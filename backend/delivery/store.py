"""The production cache: signed per-organisation bundles, served fresh, stale, or not at all.

ADR 0028, "The flow":

1. **Fresh** (rendered less than `CUSTOMER_CACHE_TTL` ago and not expired by a purge): answer.
2. **Stale but servable** (younger than `CUSTOMER_CACHE_MAX_STALE` and before its signed
   `expires_at`): whoever gets the organisation's lock refetches; everyone else answers from the
   stale bundle at once. A failed refetch answers stale too and records the failure.
3. **Missing or too old**: wait (up to `LOCK_WAIT_S`) for the organisation's lock, look again — a
   concurrent request may have filled it — and otherwise fetch. No bundle means `Unavailable`
   (the API answers 503 `data_temporarily_unavailable`), never an empty answer.

**Single flight** is a session-level Postgres advisory lock per organisation, so a burst across
gunicorn workers and threads makes one request to development, not one per request.

**Writes are serialised with purges** by a transaction-level advisory lock: a bundle whose fetch
raced a safety purge is checked against the tombstones the purge wrote, inside that lock, and
refused if it still contains a purged page. A bundle that raced an `expire` purge is stored, but
already expired (`CachedBundle.expire_requested_as_of`), so storing it cannot undo the purge.

The clock is a parameter everywhere (`now`), so the rules are tested with a fake clock.
"""

import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.conf import settings
from django.db import connection, transaction
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from core import public_ids
from delivery import client
from delivery.envelope import (
    FORMAT_BUNDLE,
    EnvelopeError,
    load_public_key,
    open_envelope,
)
from delivery.models import CachedBundle, CachedDocument, CacheTombstone, OutboxItem

logger = logging.getLogger("marceline.cache")

#: Operator decision 2026-09-17: data at most 15 minutes old while development is up.
CUSTOMER_CACHE_TTL = timedelta(minutes=15)
#: Longest a stale bundle is shown while development is unreachable (labelled `X-Data-As-Of`).
CUSTOMER_CACHE_MAX_STALE = timedelta(hours=72)
#: How far development's clock may be ahead of production's.
CLOCK_SKEW = timedelta(minutes=5)
#: How long a request without any servable bundle waits for another request's fetch.
LOCK_WAIT_S = 5.0
LOCK_POLL_S = 0.05

#: Two-integer advisory lock spaces (see core/locks.py and marceline/app/cases.py for the others).
FETCH_LOCK_SPACE = 280028
WRITE_LOCK_SPACE = 280029

#: The formats a notice draft can be downloaded in (`marceline.notices.export.FORMATS`, which
#: `delivery` may not import — it depends on `core` alone). A test asserts the two agree.
NOTICE_EXPORT_FORMATS = ("txt", "eml", "annex")

#: The customer API documents a bundle may carry: the list views, one detail per owned original
#: under its public id (ADR 0033; never an integer), `*` — the exact 404 development answers
#: for an id that is not in the bundle — `rights` (ADR 0034), `verification` (ADR 0055) and
#: `data-requests` (ADR 0056), each a single per-organisation document with no detail family, and
#: the notice documents (ADR 0046): the draft list, one detail per draft,
#: the drafting candidates per owned original, and the three download formats per draft.
_ID = public_ids.ANY_PATTERN
DOCUMENT_KEY = re.compile(
    rf"^(originals|findings|cases|removals|rights|verification|data-requests)(/({_ID}|\*))?$"
    rf"|^notices$"
    rf"|^notices/({_ID}|\*)$"
    rf"|^notices/candidates/({_ID}|\*)$"
    rf"|^notices/{_ID}/export/({'|'.join(NOTICE_EXPORT_FORMATS)})$"
)
MAX_DOCUMENTS = 20_000


class Unavailable(Exception):
    """No servable data for this organisation right now. `code` is safe to log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A customer read answers 200, its 404, or — for a notice download whose draft is not
    #: approved or already closed (ADR 0046) — the 409 that says so. Never a 5xx: a bundle carries
    #: answers, never failures (`marceline.export.DOCUMENT_STATUSES`).
    status: Literal[200, 404, 409]
    content_type: str = Field(max_length=100)
    body: str


class BundlePayload(BaseModel):
    """What development signs for one organisation (ADR 0028 envelope)."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["marceline.customer-bundle"]
    schema_version: Literal[1]
    organisation_id: int
    as_of: AwareDatetime
    expires_at: AwareDatetime
    api_version: str = Field(max_length=32)
    documents: dict[str, DocumentIn]
    page_ids: list[int]


@dataclass(frozen=True)
class Document:
    status: int
    content_type: str
    body: str
    as_of: datetime


Fetch = Callable[[int], bytes]


@lru_cache(maxsize=4)
def _public_key(path: str) -> Ed25519PublicKey:
    return load_public_key(path)


def verification_key() -> Ed25519PublicKey:
    """Development's public key (`CACHE_VERIFY_KEY_PATH`). Missing means nothing verifies."""
    path = settings.CACHE_VERIFY_KEY_PATH
    if not path:
        raise EnvelopeError("verify_key_not_configured")
    try:
        return _public_key(path)
    except (OSError, ValueError) as exc:  # missing, or not a PEM key (e.g. /dev/null)
        raise EnvelopeError("verify_key_unreadable") from exc


def verify_bundle(
    wire: bytes, key: Ed25519PublicKey, *, organisation_id: int, now: datetime
) -> BundlePayload:
    """Signature, format, schema version, organisation and time window; raises `EnvelopeError`."""
    raw = open_envelope(wire, key, expected_format=FORMAT_BUNDLE)
    try:
        payload = BundlePayload.model_validate(raw)
    except ValidationError as exc:
        raise EnvelopeError("invalid_payload") from exc
    # The organisation is inside the signature: a bundle can never be filed under another one.
    if payload.organisation_id != organisation_id:
        raise EnvelopeError("wrong_organisation")
    if payload.as_of > now + CLOCK_SKEW:
        raise EnvelopeError("from_the_future")
    if payload.expires_at <= now:
        raise EnvelopeError("expired")
    if payload.expires_at - payload.as_of > CUSTOMER_CACHE_MAX_STALE + CLOCK_SKEW:
        raise EnvelopeError("validity_too_long")
    if len(payload.documents) > MAX_DOCUMENTS or not all(
        DOCUMENT_KEY.match(k) for k in payload.documents
    ):
        raise EnvelopeError("invalid_document_key")
    return payload


def serialise_cache_writes() -> None:
    """Inside a transaction: wait for any purge or other store to finish, and hold them off."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [WRITE_LOCK_SPACE, 0])


def store_bundle(payload: BundlePayload, *, now: datetime) -> CachedBundle:
    """Replace the organisation's bundle. Refuses one older than what is stored, or one that
    still contains a page a safety purge removed after it was rendered; stores one rendered
    before an applied write or an `expire` purge already expired, so it is refetched at once."""
    with transaction.atomic():
        serialise_cache_writes()
        existing = (
            CachedBundle.objects.select_for_update()
            .filter(organisation_id=payload.organisation_id)
            .first()
        )
        if existing is not None and payload.as_of < existing.as_of:
            raise EnvelopeError("as_of_went_backwards")
        if CacheTombstone.objects.filter(
            page_id__in=payload.page_ids, purged_as_of__gte=payload.as_of
        ).exists():
            raise EnvelopeError("contains_purged_page")
        # A customer write development applied after this bundle was rendered (ADR 0028 write
        # path): serve it, but refetch on the next request, so the write shows up at once.
        behind_writes = OutboxItem.objects.filter(
            organisation_id=payload.organisation_id,
            status=OutboxItem.Status.DONE,
            applied_at__gt=payload.as_of,
        ).exists()
        # The same for an `expire` purge this fetch raced: a bundle rendered at or before the
        # purge's issue time is behind the decision the purge announced, so storing it must not
        # clear the flag the purge set. (`delete` is guarded by the tombstones above instead: a
        # flagged page may not come back at all, not even as stale.)
        behind_purge = (
            existing is not None
            and existing.expire_requested_as_of is not None
            and payload.as_of <= existing.expire_requested_as_of
        )
        fields = {
            "as_of": payload.as_of,
            "expires_at": payload.expires_at,
            "fetched_at": now,
            "api_version": payload.api_version,
            "expired": behind_writes or behind_purge,
            "page_ids": sorted(set(payload.page_ids)),
            "last_failure_at": None,
            "last_failure": "",
        }
        bundle, _ = CachedBundle.objects.update_or_create(
            organisation_id=payload.organisation_id, defaults=fields
        )
        CachedDocument.objects.filter(bundle=bundle).delete()
        CachedDocument.objects.bulk_create(
            CachedDocument(
                bundle=bundle,
                key=key,
                status=document.status,
                content_type=document.content_type,
                body=document.body,
            )
            for key, document in payload.documents.items()
        )
    return bundle


def is_fresh(bundle: CachedBundle, now: datetime) -> bool:
    return (
        not bundle.expired and now - bundle.as_of < CUSTOMER_CACHE_TTL and is_servable(bundle, now)
    )


def is_servable(bundle: CachedBundle, now: datetime) -> bool:
    return now - bundle.as_of <= CUSTOMER_CACHE_MAX_STALE and now < bundle.expires_at


def _bundle(organisation_id: int) -> CachedBundle | None:
    return CachedBundle.objects.filter(organisation_id=organisation_id).first()


def _read(organisation_id: int, key: str, fallback_key: str | None) -> Document | None:
    keys = [key] if fallback_key is None else [key, fallback_key]
    rows = {
        row.key: row
        for row in CachedDocument.objects.select_related("bundle").filter(
            bundle__organisation_id=organisation_id, key__in=keys
        )
    }
    row = rows.get(key) or (rows.get(fallback_key) if fallback_key else None)
    if row is None:
        return None
    return Document(
        status=row.status, content_type=row.content_type, body=row.body, as_of=row.bundle.as_of
    )


def _lock_key(organisation_id: int) -> list[int]:
    return [FETCH_LOCK_SPACE, organisation_id % 2**31]


@contextmanager
def _fetch_lock(organisation_id: int, *, wait_s: float) -> Iterator[bool]:
    """Session-level lock per organisation; yields whether it is held here."""
    deadline = time.monotonic() + wait_s
    held = False
    with connection.cursor() as cursor:
        while True:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", _lock_key(organisation_id))
            row = cursor.fetchone()
            held = bool(row and row[0])
            if held or time.monotonic() >= deadline:
                break
            time.sleep(LOCK_POLL_S)
    try:
        yield held
    finally:
        if held:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", _lock_key(organisation_id))


def refresh(organisation_id: int, *, now: datetime, fetch: Fetch) -> CachedBundle | None:
    """Fetch, verify and store one organisation's bundle; None (failure recorded) if that fails."""
    try:
        wire = fetch(organisation_id)
        payload = verify_bundle(wire, verification_key(), organisation_id=organisation_id, now=now)
        return store_bundle(payload, now=now)
    except (client.DeliveryError, EnvelopeError) as exc:
        code = exc.code
    CachedBundle.objects.filter(organisation_id=organisation_id).update(
        last_failure_at=now, last_failure=code[:64]
    )
    logger.warning("cache refresh failed for organisation %s: %s", organisation_id, code)
    return None


def document_for(
    organisation_id: int,
    key: str,
    *,
    fallback_key: str | None = None,
    now: datetime,
    fetch: Fetch | None = None,
) -> Document:
    """The cached response for `key` (or `fallback_key`, the bundle's 404), refreshing as needed.

    Raises `Unavailable` when there is nothing that may be shown.
    """
    fetch = fetch or client.fetch_bundle
    bundle = _bundle(organisation_id)
    if bundle is not None and is_fresh(bundle, now):
        found = _read(organisation_id, key, fallback_key)
        if found is not None:
            return found
    if bundle is not None and is_servable(bundle, now):
        with _fetch_lock(organisation_id, wait_s=0) as held:
            if held:
                current = _bundle(organisation_id)
                if current is None or not is_fresh(current, now):
                    refresh(organisation_id, now=now, fetch=fetch)
        found = _read(organisation_id, key, fallback_key)
        if found is not None and now - found.as_of <= CUSTOMER_CACHE_MAX_STALE:
            return found
        raise Unavailable("no_servable_bundle")
    with _fetch_lock(organisation_id, wait_s=LOCK_WAIT_S) as held:
        if not held:
            raise Unavailable("fetch_in_progress")
        current = _bundle(organisation_id)
        if current is None or not is_fresh(current, now):
            current = refresh(organisation_id, now=now, fetch=fetch) or current
        if current is None or not is_servable(current, now):
            raise Unavailable("no_servable_bundle")
    found = _read(organisation_id, key, fallback_key)
    if found is None:
        raise Unavailable("document_missing")
    return found
