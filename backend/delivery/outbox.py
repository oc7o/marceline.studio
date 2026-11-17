"""Customer writes on production: accept into an outbox, hand out signed batches, settle (ADR 0028
amendment "The production write path").

Production has no originals, matches or rights records to write to — it is a cache. A customer
write is therefore **accepted** here (the endpoint has already checked the session, CSRF, the rate
limit and the document), stored as an `OutboxItem` and answered 202; development **pulls** pending
items over Tailscale, applies each through the code its own endpoint uses, and **acknowledges** the
outcome. Nothing in this module knows what a write means; it moves and stores validated bytes.

Who signs what:

- **Development signs its requests** (`marceline.outbox-pull`, `marceline.outbox-ack`) with the
  key it already signs bundles and purges with; production verifies them with development's public
  key (`CACHE_VERIFY_KEY_PATH`), inside ±`CLOCK_SKEW`, with a single-use nonce. A tailnet node that
  is not development can neither read the outbox nor settle items.
- **Production signs the batches** (`marceline.outbox-batch`) with a key of its own
  (`OUTBOX_SIGNING_KEY_PATH`, held by the Tailscale-only `internal` process only); development
  verifies them with production's public key. The batch echoes the pull's nonce, so an old batch
  cannot be replayed into a later pull. A spoofed production node cannot inject writes.
- Neither machine holds the other's private key. A fully compromised production can still invent
  customer writes — it holds the customer sessions, so it could anyway; development re-validates
  every write and the writes it applies are the ones a signed-in customer could make.

Everything takes `now`; nothing here reads the clock.
"""

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings
from django.db import transaction
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from core import public_ids
from delivery.envelope import (
    FORMAT_OUTBOX_ACK,
    FORMAT_OUTBOX_BATCH,
    FORMAT_OUTBOX_PULL,
    SCHEMA_VERSION,
    EnvelopeError,
    load_private_key,
    open_envelope,
    seal,
)
from delivery.models import CachedBundle, OutboxItem, OutboxPullState
from delivery.purge import claim_nonce
from delivery.store import CLOCK_SKEW, serialise_cache_writes

logger = logging.getLogger("marceline.outbox")

ITEM_ID_PREFIX = "sub"
ITEM_ID_PATTERN = "^" + public_ids.pattern(ITEM_ID_PREFIX) + "$"
#: Payload bytes one batch carries; the first item always goes, however large (an original's
#: document is at most 16 MiB, which still fits an envelope once encoded).
BATCH_BYTES = 8 * 1024 * 1024
MAX_BATCH_ITEMS = 200
#: Outcomes one acknowledgement carries. Bounded by what production's edge accepts on
#: `/internal/v1/outbox/ack` — 256 KB (`infra/prod/Caddyfile`) — because a 413 there never heals:
#: `inbox.acknowledge` always sends the *oldest* settled, unacknowledged outcomes, so an
#: acknowledgement too large is too large on every run for ever, and nothing on production would
#: settle again. A result costs up to ~375 bytes sealed (longest ids, a 64-character code, a
#: timestamp, base64), so this leaves the wire under 200 KB at its worst; what does not fit goes
#: in the next run's, a minute later. Kept at twice `MAX_BATCH_ITEMS`, so an ordinary run's
#: outcomes never have to split. `test_prod_edge_config.py` measures it against the Caddyfile.
MAX_ACK_RESULTS = 2 * MAX_BATCH_ITEMS + 100
#: Settled items (payload already emptied) stay this long, so the customer sees the outcome.
RETENTION = timedelta(days=7)


# --- accepting ----------------------------------------------------------------------------------


def writes_enabled() -> bool:
    """This production machine accepts customer writes into the outbox."""
    return settings.MARCELINE_ROLE == "prod" and settings.OUTBOX_ENABLED


def paused(now: datetime) -> bool:
    """Development has not pulled for `OUTBOX_PAUSE_AFTER_HOURS` (or never): refuse new writes, so
    a customer hears it at once instead of watching a write wait for days."""
    pulled = OutboxPullState.objects.values_list("last_pull_at", flat=True).first()
    limit = timedelta(hours=settings.OUTBOX_PAUSE_AFTER_HOURS)
    return pulled is None or now - pulled > limit


def enqueue(
    *,
    kind: str,
    organisation_id: int,
    user_id: int,
    payload: dict[str, Any],
    now: datetime,
    title: str = "",
    dedupe_key: str = "",
) -> tuple[OutboxItem, bool]:
    """Store a validated write; returns (item, created). A still-pending item of the same kind and
    key for the same organisation is returned instead of a second one (a double submit)."""
    with transaction.atomic():
        if dedupe_key:
            existing = (
                OutboxItem.objects.select_for_update()
                .filter(
                    organisation_id=organisation_id,
                    kind=kind,
                    dedupe_key=dedupe_key,
                    status=OutboxItem.Status.PENDING,
                )
                .order_by("pk")
                .first()
            )
            if existing is not None:
                return existing, False
        text = json.dumps(payload, separators=(",", ":"))
        item = OutboxItem.objects.create(
            kind=kind,
            organisation_id=organisation_id,
            user_id=user_id,
            payload=text,
            payload_bytes=len(text.encode()),
            title=title[:300],
            dedupe_key=dedupe_key[:128],
            received_at=now,
        )
    logger.info("outbox accepted %s %s of organisation %s", kind, item.public_id, organisation_id)
    return item, True


# --- keys ---------------------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _private_key(path: str) -> Ed25519PrivateKey:
    return load_private_key(path)


def signing_key() -> Ed25519PrivateKey:
    """Production's own outbox key (`OUTBOX_SIGNING_KEY_PATH`); only `internal` holds it."""
    path = settings.OUTBOX_SIGNING_KEY_PATH
    if not path:
        raise EnvelopeError("outbox_signing_key_not_configured")
    try:
        return _private_key(path)
    except (OSError, ValueError) as exc:  # missing, or not a PEM key (e.g. /dev/null)
        raise EnvelopeError("outbox_signing_key_unreadable") from exc


# --- the pull -----------------------------------------------------------------------------------

NonceStr = Annotated[str, Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class PullPayload(BaseModel):
    """What development signs to ask for pending writes."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["marceline.outbox-pull"]
    schema_version: Literal[1]
    issued_at: AwareDatetime
    nonce: NonceStr


class ItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ITEM_ID_PATTERN)
    organisation_id: int
    user_id: int
    kind: str = Field(max_length=32)
    received_at: AwareDatetime
    payload: str


class BatchPayload(BaseModel):
    """What production signs in answer: pending items in acceptance order."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["marceline.outbox-batch"]
    schema_version: Literal[1]
    issued_at: AwareDatetime
    request_nonce: NonceStr
    items: list[ItemOut] = Field(max_length=MAX_BATCH_ITEMS)
    more: bool


class ResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ITEM_ID_PATTERN)
    organisation_id: int
    outcome: Literal["done", "refused", "quarantined"]
    code: str = Field(max_length=64, pattern=r"^[a-z0-9_]*$")
    original_id: str = Field(default="", max_length=64, pattern=r"^([a-z]{2,8}_[0-9a-f]{32})?$")
    applied_at: AwareDatetime


class AckPayload(BaseModel):
    """What development signs to settle items."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["marceline.outbox-ack"]
    schema_version: Literal[1]
    issued_at: AwareDatetime
    nonce: NonceStr
    results: list[ResultIn] = Field(max_length=MAX_ACK_RESULTS)


def within_window(issued_at: datetime, now: datetime) -> None:
    if abs(now - issued_at) > CLOCK_SKEW:
        raise EnvelopeError("outside_time_window")


def verify_pull(wire: bytes, key: Ed25519PublicKey, *, now: datetime) -> PullPayload:
    """Development's signature, time window and a single-use nonce; raises `EnvelopeError`."""
    raw = open_envelope(wire, key, expected_format=FORMAT_OUTBOX_PULL)
    try:
        payload = PullPayload.model_validate(raw)
    except ValidationError as exc:
        raise EnvelopeError("invalid_payload") from exc
    within_window(payload.issued_at, now)
    claim_nonce(payload.nonce, issued_at=payload.issued_at, now=now)
    return payload


def batch_payload(pull: PullPayload, *, now: datetime) -> dict[str, Any]:
    """Pending items, oldest first, up to `BATCH_BYTES` of payload (always at least one), and
    stamps the pull. Items stay pending until an acknowledgement settles them."""
    candidates = list(
        OutboxItem.objects.filter(status=OutboxItem.Status.PENDING)
        .order_by("pk")
        .values_list("pk", "payload_bytes")[: MAX_BATCH_ITEMS + 1]
    )
    chosen: list[int] = []
    total = 0
    for pk, size in candidates[:MAX_BATCH_ITEMS]:
        if chosen and total + size > BATCH_BYTES:
            break
        chosen.append(pk)
        total += size
    more = len(chosen) < len(candidates)
    rows = {row.pk: row for row in OutboxItem.objects.filter(pk__in=chosen)}
    with transaction.atomic():
        OutboxPullState.objects.update_or_create(singleton=True, defaults={"last_pull_at": now})
        if chosen:
            for row in rows.values():
                OutboxItem.objects.filter(pk=row.pk).update(pulls=row.pulls + 1, last_pulled_at=now)
    return {
        "format": FORMAT_OUTBOX_BATCH,
        "schema_version": SCHEMA_VERSION,
        "issued_at": now,
        "request_nonce": pull.nonce,
        "items": [
            {
                "id": rows[pk].public_id,
                "organisation_id": rows[pk].organisation_id,
                "user_id": rows[pk].user_id,
                "kind": rows[pk].kind,
                "received_at": rows[pk].received_at,
                "payload": rows[pk].payload,
            }
            for pk in chosen
        ],
        "more": more,
    }


def signed_batch(pull: PullPayload, key: Ed25519PrivateKey, *, now: datetime) -> bytes:
    return seal(batch_payload(pull, now=now), key)


# --- settling -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AckResult:
    settled: int
    ignored: int


def verify_ack(wire: bytes, key: Ed25519PublicKey, *, now: datetime) -> AckPayload:
    raw = open_envelope(wire, key, expected_format=FORMAT_OUTBOX_ACK)
    try:
        payload = AckPayload.model_validate(raw)
    except ValidationError as exc:
        raise EnvelopeError("invalid_payload") from exc
    within_window(payload.issued_at, now)
    claim_nonce(payload.nonce, issued_at=payload.issued_at, now=now)
    return payload


def apply_ack(payload: AckPayload, *, now: datetime) -> AckResult:
    """Settle items: status, code, the original's public id; the payload is emptied at once.

    A pending item takes any outcome; a quarantined one may still become done or refused (staff
    applied it again on development); done and refused are final. The organisation in the result
    must be the item's own. Organisations with a `done` write get their cached bundle expired in the
    same transaction, so the change is fetched on the customer's next request.
    """
    settled = 0
    expire: set[int] = set()
    with transaction.atomic():
        for result in payload.results:
            allowed = [OutboxItem.Status.PENDING]
            if result.outcome != "quarantined":
                allowed.append(OutboxItem.Status.QUARANTINED)
            count = OutboxItem.objects.filter(
                public_id=result.id, organisation_id=result.organisation_id, status__in=allowed
            ).update(
                status=result.outcome,
                result_code=result.code,
                original_id=result.original_id,
                applied_at=result.applied_at,
                settled_at=now,
                payload="",
            )
            settled += count
            if count and result.outcome == "done":
                expire.add(result.organisation_id)
        if expire:
            serialise_cache_writes()
            CachedBundle.objects.filter(organisation_id__in=expire).update(expired=True)
    ignored = len(payload.results) - settled
    logger.info("outbox acknowledgement: %s settled, %s ignored", settled, ignored)
    return AckResult(settled=settled, ignored=ignored)


# --- housekeeping -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Housekeeping:
    stale_reported: int
    deleted: int
    paused: bool


def housekeeping(*, now: datetime) -> Housekeeping:
    """Report writes pending longer than `OUTBOX_STALE_ALERT_HOURS` (an ERROR log line, which
    Sentry turns into an event; once per item), report a paused outbox, and delete items settled
    longer than `RETENTION` ago. Cron-safe and idempotent."""
    stale = OutboxItem.objects.filter(
        status=OutboxItem.Status.PENDING,
        alerted_at__isnull=True,
        received_at__lt=now - timedelta(hours=settings.OUTBOX_STALE_ALERT_HOURS),
    )
    oldest = stale.order_by("received_at").values_list("received_at", flat=True).first()
    reported = stale.update(alerted_at=now)
    if reported and oldest is not None:
        logger.error(
            "customer write outbox: %s write(s) pending longer than %s h (oldest received %s)",
            reported,
            settings.OUTBOX_STALE_ALERT_HOURS,
            f"{oldest:%Y-%m-%d %H:%M} UTC",
        )
    is_paused = writes_enabled() and paused(now)
    if is_paused:
        logger.error(
            "customer write outbox: development has not pulled for %s h or more; new writes are "
            "refused",
            settings.OUTBOX_PAUSE_AFTER_HOURS,
        )
    deleted, _ = (
        OutboxItem.objects.exclude(status=OutboxItem.Status.PENDING)
        .filter(settled_at__lt=now - RETENTION)
        .delete()
    )
    return Housekeeping(stale_reported=reported, deleted=deleted, paused=is_paused)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)
