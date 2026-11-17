"""Receiving a purge on production (ADR 0028, "Force invalidation from development").

A purge is a signed envelope naming organisations and/or pages, with an issue time and a nonce:

- `expire` — after decisions: the bundles are marked expired and refetched on the next request;
  while development is down the old bundle is still served. The issue time is kept on each bundle
  (`expire_requested_as_of`), so a bundle rendered before the purge and arriving after it is
  stored already expired rather than clearing the flag.
- `delete` — on a safety block: the bundles are deleted, never merely expired, and every named page
  gets a tombstone, so a bundle rendered before the purge (a fetch that raced it) is refused when it
  arrives. If development is down the customer gets a 503 rather than the flagged page.

A purge older or newer than `PURGE_MAX_SKEW`, or with a nonce already seen, is refused: a replayed
purge that deletes every entry would be a denial of service.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.db import IntegrityError, transaction
from django.db.models import Q, Value
from django.db.models.functions import Coalesce, Greatest
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from delivery.envelope import FORMAT_PURGE, EnvelopeError, open_envelope
from delivery.models import CachedBundle, CacheTombstone, PurgeNonce
from delivery.store import CLOCK_SKEW, CUSTOMER_CACHE_MAX_STALE, serialise_cache_writes

logger = logging.getLogger("marceline.cache")

PURGE_MAX_SKEW = CLOCK_SKEW
MAX_IDS = 10_000


class PurgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["marceline.cache-purge"]
    schema_version: Literal[1]
    mode: Literal["expire", "delete"]
    organisation_ids: list[int] = Field(max_length=MAX_IDS)
    page_ids: list[int] = Field(max_length=MAX_IDS)
    issued_at: AwareDatetime
    nonce: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class PurgeResult:
    mode: str
    bundles: int


def verify_purge(wire: bytes, key: Ed25519PublicKey, *, now: datetime) -> PurgePayload:
    raw = open_envelope(wire, key, expected_format=FORMAT_PURGE)
    try:
        payload = PurgePayload.model_validate(raw)
    except ValidationError as exc:
        raise EnvelopeError("invalid_payload") from exc
    if abs(now - payload.issued_at) > PURGE_MAX_SKEW:
        raise EnvelopeError("outside_time_window")
    if not payload.organisation_ids and not payload.page_ids:
        raise EnvelopeError("empty_purge")
    return payload


def claim_nonce(nonce: str, *, issued_at: datetime, now: datetime) -> None:
    """Record a nonce of a request development signed; `EnvelopeError("replayed")` if seen before.

    One namespace for every signed request production receives from development (purges, outbox
    pulls and acknowledgements): nonces are random, so they never collide across formats. Kept a
    little longer than the clock-skew window, after which the time check refuses a replay anyway.
    """
    try:
        with transaction.atomic():
            PurgeNonce.objects.create(nonce=nonce, issued_at=issued_at)
    except IntegrityError as exc:
        raise EnvelopeError("replayed") from exc
    PurgeNonce.objects.filter(issued_at__lt=now - 2 * PURGE_MAX_SKEW).delete()


def apply_purge(payload: PurgePayload, *, now: datetime) -> PurgeResult:
    """Apply a verified purge. Raises `EnvelopeError("replayed")` for a nonce seen before."""
    with transaction.atomic():
        serialise_cache_writes()
        claim_nonce(payload.nonce, issued_at=payload.issued_at, now=now)
        targets = CachedBundle.objects.filter(
            Q(organisation_id__in=payload.organisation_ids) | Q(page_ids__overlap=payload.page_ids)
        )
        if payload.mode == "delete":
            for page_id in set(payload.page_ids):
                tombstone, created = CacheTombstone.objects.get_or_create(
                    page_id=page_id, defaults={"purged_as_of": payload.issued_at}
                )
                if not created and tombstone.purged_as_of < payload.issued_at:
                    tombstone.purged_as_of = payload.issued_at
                    tombstone.save(update_fields=("purged_as_of",))
            # A tombstone older than any bundle that could still be stored has done its job.
            CacheTombstone.objects.filter(
                purged_as_of__lt=now - CUSTOMER_CACHE_MAX_STALE - 2 * CLOCK_SKEW
            ).delete()
            count = targets.count()
            targets.delete()
        else:
            # `expire_requested_as_of` is this purge's mark: `store_bundle` reads it so that a
            # fetch that raced the purge (a bundle rendered before it, arriving after) is stored
            # already expired instead of clearing the flag. Only ever moved forwards, so an out of
            # order purge cannot weaken a newer one.
            count = targets.update(
                expired=True,
                expire_requested_as_of=Greatest(
                    Coalesce("expire_requested_as_of", Value(payload.issued_at)),
                    Value(payload.issued_at),
                ),
            )
    logger.info(
        "cache purge applied: %s, %s organisation(s), %s page(s), %s bundle(s)",
        payload.mode,
        len(payload.organisation_ids),
        len(payload.page_ids),
        count,
    )
    return PurgeResult(mode=payload.mode, bundles=count)


def receive_purge(wire: bytes, key: Ed25519PublicKey, *, now: datetime) -> PurgeResult:
    return apply_purge(verify_purge(wire, key, now=now), now=now)
