"""Development pulls customer writes from production and applies each one once (ADR 0028 amendment
"The production write path").

One run (`manage.py pull_customer_writes`, cron every minute in the `export` container):

1. **Pull.** A request signed with development's key (fresh nonce, issue time) goes to production's
   Tailscale-only listener; the answer is a batch signed with *production's* key that must echo the
   nonce. Anything that does not verify is refused as a whole.
2. **Apply, in order, once.** Items arrive in acceptance order. Each is applied by the caller's
   `apply` function — the project package wires the same code the development endpoints use — in
   one transaction together with its `InboxItem` row, so an item is applied at most once however
   often it is pulled: a settled row is only acknowledged again.
   - `apply` returns an `Outcome`: `done` (with a result code, e.g. `created`) or `refused` (the
     code development's own endpoint would have answered, e.g. `claim_needs_review`).
   - `apply` raising is a **failure**: the item stays pending on production, and the rest of that
     organisation's items wait for the next run (per-organisation order; other organisations go
     on). After `MAX_APPLY_ATTEMPTS` failures the item is **quarantined**: settled with the
     exception's class name as reason and its payload kept here for `--retry`, so one poison item
     cannot hold an organisation back for ever.
3. **Acknowledge.** Every settled row production has not confirmed yet is sent in one signed
   acknowledgement; production settles its items, empties their payloads and expires the
   organisations' cached bundles. A lost acknowledgement is simply sent again by the next run.
   Production parses that one message as a unit, so an outcome it could not parse would hold back
   every organisation for ever: `sendable` keeps each row's outcome inside the shape production
   accepts, and drops from the *wire* what it cannot (never from the row).

Keys: `signing_key` is development's (the one bundles and purges use); `OUTBOX_VERIFY_KEY_PATH` is
production's public key. Development never holds production's private key.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings
from django.db import transaction
from pydantic import ValidationError

from delivery import client
from delivery.envelope import (
    FORMAT_OUTBOX_ACK,
    FORMAT_OUTBOX_BATCH,
    FORMAT_OUTBOX_PULL,
    SCHEMA_VERSION,
    EnvelopeError,
    load_public_key,
    open_envelope,
    seal,
)
from delivery.models import InboxItem
from delivery.outbox import (
    MAX_ACK_RESULTS,
    RETENTION,
    BatchPayload,
    ItemOut,
    ResultIn,
    new_nonce,
    within_window,
)

logger = logging.getLogger("marceline.outbox")

MAX_APPLY_ATTEMPTS = 3
#: Single-instance lock for the pull command (see core/locks.py for the other key spaces).
PULL_LOCK_SPACE = 73428310


@dataclass(frozen=True)
class PulledItem:
    """One customer write as production accepted it. `payload` is JSON text, not yet trusted."""

    id: str
    organisation_id: int
    user_id: int
    kind: str
    received_at: datetime
    payload: str


@dataclass(frozen=True)
class Outcome:
    outcome: Literal["done", "refused"]
    code: str
    original_id: str = ""


Apply = Callable[[PulledItem], Outcome]
PostPull = Callable[[bytes], bytes]
PostAck = Callable[[bytes], None]


@dataclass(frozen=True)
class RunReport:
    pulled: int
    settled: int
    failed: int
    waiting: int
    acknowledged: int
    more: bool


@lru_cache(maxsize=4)
def _public_key(path: str) -> Ed25519PublicKey:
    return load_public_key(path)


def verification_key() -> Ed25519PublicKey:
    """Production's public outbox key (`OUTBOX_VERIFY_KEY_PATH`)."""
    path = settings.OUTBOX_VERIFY_KEY_PATH
    if not path:
        raise EnvelopeError("outbox_verify_key_not_configured")
    try:
        return _public_key(path)
    except (OSError, ValueError) as exc:  # missing, or not a PEM key (e.g. /dev/null)
        raise EnvelopeError("outbox_verify_key_unreadable") from exc


def pull_payload(*, nonce: str, now: datetime) -> dict[str, Any]:
    return {
        "format": FORMAT_OUTBOX_PULL,
        "schema_version": SCHEMA_VERSION,
        "issued_at": now,
        "nonce": nonce,
    }


def verify_batch(wire: bytes, key: Ed25519PublicKey, *, nonce: str, now: datetime) -> BatchPayload:
    """Production's signature, the echoed nonce and the time window; raises `EnvelopeError`."""
    raw = open_envelope(wire, key, expected_format=FORMAT_OUTBOX_BATCH)
    try:
        payload = BatchPayload.model_validate(raw)
    except ValidationError as exc:
        raise EnvelopeError("invalid_payload") from exc
    if payload.request_nonce != nonce:
        raise EnvelopeError("nonce_mismatch")
    within_window(payload.issued_at, now)
    return payload


def _pulled(item: ItemOut) -> PulledItem:
    return PulledItem(
        id=item.id,
        organisation_id=item.organisation_id,
        user_id=item.user_id,
        kind=item.kind,
        received_at=item.received_at,
        payload=item.payload,
    )


def _settle(row: InboxItem, outcome: Outcome, *, now: datetime) -> None:
    row.status = InboxItem.Status.APPLIED if outcome.outcome == "done" else InboxItem.Status.REFUSED
    row.result_code = outcome.code[:64]
    row.original_id = outcome.original_id[:64]
    row.attempts += 1
    row.reason = ""
    row.payload = ""
    row.settled_at = now
    row.acknowledged_at = None
    row.save()


def _record_failure(item: PulledItem, error: Exception, *, now: datetime) -> InboxItem:
    code = type(error).__name__[:128]
    with transaction.atomic():
        row, _ = InboxItem.objects.select_for_update().get_or_create(
            item_id=item.id,
            defaults={
                "organisation_id": item.organisation_id,
                "user_id": item.user_id,
                "kind": item.kind,
                "received_at": item.received_at,
                "status": InboxItem.Status.FAILED,
                "first_pulled_at": now,
            },
        )
        row.attempts += 1
        row.reason = code
        if row.attempts >= MAX_APPLY_ATTEMPTS:
            row.status = InboxItem.Status.QUARANTINED
            row.result_code = "processing_failed"
            row.payload = item.payload
            row.settled_at = now
            row.acknowledged_at = None
        else:
            row.status = InboxItem.Status.FAILED
        row.save()
    # The traceback stays in development's own log (a warning is no Sentry event); the error line
    # Sentry receives names the item and the exception class, never the payload.
    logger.warning("customer write %s failed: %s", item.id, code, exc_info=error)
    if row.status == InboxItem.Status.QUARANTINED:
        logger.error(
            "customer write %s (%s, organisation %s) quarantined after %s attempts: %s",
            item.id,
            item.kind,
            item.organisation_id,
            row.attempts,
            code,
        )
    return row


def apply_item(item: PulledItem, apply: Apply, *, now: datetime) -> InboxItem:
    """Apply one pulled item at most once; returns its row (status `failed` = try again later)."""
    try:
        with transaction.atomic():
            row = InboxItem.objects.select_for_update().filter(item_id=item.id).first()
            if row is not None and row.status != InboxItem.Status.FAILED:
                return row
            outcome = apply(item)
            if row is None:
                row = InboxItem(
                    item_id=item.id,
                    organisation_id=item.organisation_id,
                    user_id=item.user_id,
                    kind=item.kind,
                    received_at=item.received_at,
                    first_pulled_at=now,
                )
            _settle(row, outcome, now=now)
            return row
    except Exception as error:
        return _record_failure(item, error, now=now)


#: What an outcome is worth on the wire. A row whose status cannot be expressed is not sent at all
#: rather than crashing the acknowledgement it travels in.
OUTCOMES = {
    InboxItem.Status.APPLIED: "done",
    InboxItem.Status.REFUSED: "refused",
    InboxItem.Status.QUARANTINED: "quarantined",
}
#: Fields an outcome can do without, tried in this order when production could not parse it.
DROPPABLE: tuple[tuple[str, ...], ...] = ((), ("original_id",), ("code",), ("code", "original_id"))


def sendable(row: InboxItem) -> dict[str, Any] | None:
    """One outcome as `outbox.ResultIn` accepts it, or None when it cannot be carried at all.

    Production parses the whole acknowledgement as a unit and refuses it as a unit, and every run
    re-sends *every* settled, unacknowledged outcome — so one value outside `ResultIn`'s shape (a
    handler answering `Claim-Needs-Review` rather than `claim_needs_review`) would make every
    acknowledgement unparseable for good: nothing would settle on production again, its writes
    would sit pending until they tripped the stale alert and paused the outbox, and no customer
    could add anything. The shape is therefore checked here, against `ResultIn` itself so the two
    cannot drift, and what production cannot parse is left out of the wire — never out of this
    row, which keeps the real code for staff.
    """
    result: dict[str, Any] = {
        "id": row.item_id,
        "organisation_id": row.organisation_id,
        "outcome": OUTCOMES.get(InboxItem.Status(row.status)),
        "code": row.result_code,
        "original_id": row.original_id,
        "applied_at": row.settled_at,
    }
    for fields in DROPPABLE:
        candidate = result | {field: "" for field in fields}
        try:
            ResultIn.model_validate(candidate)
        except ValidationError:
            continue
        if fields:
            logger.error(
                "customer write %s: production cannot parse its %s; acknowledged without it",
                row.item_id,
                " and ".join(fields),
            )
        return candidate
    logger.error(
        "customer write %s cannot be acknowledged at all (status %s); it stays pending on "
        "production",
        row.item_id,
        row.status,
    )
    return None


def acknowledge(signing_key: Ed25519PrivateKey, *, now: datetime, post: PostAck) -> int:
    """Send every settled, unacknowledged outcome; returns how many production confirmed."""
    rows = list(
        InboxItem.objects.filter(settled_at__isnull=False, acknowledged_at__isnull=True).order_by(
            "settled_at", "pk"
        )[:MAX_ACK_RESULTS]
    )
    settled = [(row, result) for row in rows if (result := sendable(row)) is not None]
    if not settled:
        return 0
    payload = {
        "format": FORMAT_OUTBOX_ACK,
        "schema_version": SCHEMA_VERSION,
        "issued_at": now,
        "nonce": new_nonce(),
        "results": [result for _, result in settled],
    }
    post(seal(payload, signing_key))
    InboxItem.objects.filter(pk__in=[row.pk for row, _ in settled]).update(acknowledged_at=now)
    return len(settled)


def run_once(
    apply: Apply,
    *,
    signing_key: Ed25519PrivateKey,
    verify_key: Ed25519PublicKey,
    now: Callable[[], datetime],
    post_pull: PostPull | None = None,
    post_ack: PostAck | None = None,
) -> RunReport:
    """One pull, the items in order, one acknowledgement. Raises `DeliveryError`/`EnvelopeError`
    when production cannot be reached or does not verify (nothing is applied then)."""
    post_pull = post_pull or client.post_outbox_pull
    post_ack = post_ack or client.post_outbox_ack
    nonce = new_nonce()
    wire = post_pull(seal(pull_payload(nonce=nonce, now=now()), signing_key))
    batch = verify_batch(wire, verify_key, nonce=nonce, now=now())
    blocked: set[int] = set()
    settled = failed = waiting = 0
    for item in batch.items:
        if item.organisation_id in blocked:
            waiting += 1
            continue
        row = apply_item(_pulled(item), apply, now=now())
        if row.status == InboxItem.Status.FAILED:
            failed += 1
            blocked.add(item.organisation_id)
        else:
            settled += 1
    acknowledged = acknowledge(signing_key, now=now(), post=post_ack)
    return RunReport(
        pulled=len(batch.items),
        settled=settled,
        failed=failed,
        waiting=waiting,
        acknowledged=acknowledged,
        more=batch.more and not failed and not waiting,
    )


def retry(item_id: str, apply: Apply, *, now: datetime) -> InboxItem:
    """Apply a quarantined item again from its kept payload (after a fix). A success settles it
    anew, and the next acknowledgement tells production; another failure keeps it quarantined."""
    row = InboxItem.objects.get(item_id=item_id, status=InboxItem.Status.QUARANTINED)
    item = PulledItem(
        id=row.item_id,
        organisation_id=row.organisation_id,
        user_id=row.user_id,
        kind=row.kind,
        received_at=row.received_at,
        payload=row.payload,
    )
    try:
        with transaction.atomic():
            locked = InboxItem.objects.select_for_update().get(pk=row.pk)
            _settle(locked, apply(item), now=now)
            return locked
    except Exception as error:
        InboxItem.objects.filter(pk=row.pk).update(
            attempts=row.attempts + 1, reason=type(error).__name__[:128]
        )
        logger.warning("retry of customer write %s failed", item_id, exc_info=error)
        row.refresh_from_db()
        return row


def housekeeping(*, now: datetime) -> int:
    """Delete acknowledged, applied or refused rows older than `RETENTION`. Quarantined rows stay
    until staff retried them; failing rows are still pending on production."""
    deleted, _ = InboxItem.objects.filter(
        status__in=(InboxItem.Status.APPLIED, InboxItem.Status.REFUSED),
        acknowledged_at__lt=now - RETENTION,
    ).delete()
    return deleted
