"""Development tells production to forget (ADR 0028, "Force invalidation from development").

Two triggers, both after the change commits (`transaction.on_commit`):

- **A safety flag** on a page (clear → review or blocked, however it was saved): a `delete` purge
  of that page. Production deletes every bundle containing it — across all organisations — and
  tombstones the page. Saves through the ORM are caught by the `Page` signals wired in
  `marceline/apps.py`; bulk `.update()` writes (the `safety_scan` command) are caught by
  `manage.py send_cache_purges --flagged-within-minutes N`, run from cron.
- **A review decision** (confirm, reject or reset of a leak/match pair): an `expire` purge of the
  organisations owning those originals. Their next request refetches; the TTL bounds a purge that
  never arrives.

Each purge is first written to `delivery.PurgeOutbox` inside the transaction that caused it, then
delivered best-effort right after commit with a few quick retries. What fails stays pending with an
error code and a log line (never a URL), and `send_cache_purges` retries it.

Only on `MARCELINE_ROLE=dev`, and only when configured:
- `CACHE_PURGE_BASE_URL` (plus the signing key) set: purges are recorded **and** sent after commit;
- `CACHE_PURGE_OUTBOX=true` without a URL: purges are only recorded, and a process that has the
  key and the URL delivers them (`send_cache_purges`, e.g. in the export container). This is how
  the development web process takes part without holding the signing key;
- neither: nothing is written and nothing is sent.
"""

import logging
import secrets
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from accounts.models import Organisation
from delivery import client
from delivery.envelope import FORMAT_PURGE, SCHEMA_VERSION, EnvelopeError, seal
from delivery.models import PurgeOutbox
from delivery.purge import MAX_IDS
from index.models import Page
from marceline.app.ownership import owned_references
from marceline.app.safety import hidden_page_filter
from marceline.export import signing_key
from matching.models import ReferenceLeak, ReferenceMatch

logger = logging.getLogger("marceline.cache")

DELIVERY_ATTEMPTS = 3
RETRY_BACKOFF_S = (0.2, 0.5)
#: Test hook, so retries do not sleep in tests.
sleep: Callable[[float], None] = time.sleep


def sending() -> bool:
    """This process delivers purges itself."""
    return settings.MARCELINE_ROLE == "dev" and bool(settings.CACHE_PURGE_BASE_URL)


def enabled() -> bool:
    """This process records purges (and maybe delivers them, `sending`)."""
    return sending() or (settings.MARCELINE_ROLE == "dev" and settings.CACHE_PURGE_OUTBOX)


def _batched(ids: list[int]) -> list[list[int]]:
    return [ids[start : start + MAX_IDS] for start in range(0, len(ids), MAX_IDS)]


def _batches(organisations: list[int], pages: list[int]) -> list[tuple[list[int], list[int]]]:
    """(organisations, pages) pairs, none naming more ids than one purge may carry.

    What fits in one purge stays one purge; only the oversized case splits, and then each half
    travels on its own, which every mode tolerates (both are idempotent on production).
    """
    if len(organisations) <= MAX_IDS and len(pages) <= MAX_IDS:
        return [(organisations, pages)]
    return [(chunk, []) for chunk in _batched(organisations)] + [
        ([], chunk) for chunk in _batched(pages)
    ]


def request_purge(
    mode: str,
    *,
    reason: str,
    organisation_ids: Iterable[int] = (),
    page_ids: Iterable[int] = (),
) -> list[PurgeOutbox]:
    """Record a purge in the current transaction and deliver it once that commits.

    Several purges when there are more ids than production accepts in one (`purge.MAX_IDS`).
    `flagged_pages_since` is unbounded — a `safety_scan` that rescores a large site can hide
    thousands of linked pages in one sweep — and an oversized purge is refused whole on arrival
    (`invalid_payload`) and then stays pending for ever; with the sweep queueing another one every
    few minutes, `deliver_pending`'s oldest-first window would fill with undeliverable rows until
    no later safety purge was delivered either.
    """
    organisations = sorted(set(organisation_ids))
    pages = sorted(set(page_ids))
    if not enabled() or not (organisations or pages):
        return []
    rows = [
        PurgeOutbox.objects.create(
            mode=mode, organisation_ids=chunk_organisations, page_ids=chunk_pages, reason=reason
        )
        for chunk_organisations, chunk_pages in _batches(organisations, pages)
    ]
    if sending():
        for row in rows:
            transaction.on_commit(partial(deliver, row.pk))
    return rows


def purge_payload(row: PurgeOutbox, *, now: datetime) -> dict[str, Any]:
    """A fresh issue time and nonce per attempt: production refuses replays and old purges."""
    return {
        "format": FORMAT_PURGE,
        "schema_version": SCHEMA_VERSION,
        "mode": row.mode,
        "organisation_ids": list(row.organisation_ids),
        "page_ids": list(row.page_ids),
        "issued_at": now,
        "nonce": secrets.token_urlsafe(24),
    }


def deliver(outbox_id: int) -> bool:
    """Send one pending purge, retrying briefly. True once production acknowledged it."""
    row = PurgeOutbox.objects.filter(pk=outbox_id, sent_at__isnull=True).first()
    if row is None:
        return True
    error = ""
    attempts = 0
    for attempt in range(DELIVERY_ATTEMPTS):
        if attempt:
            sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S)) - 1])
        attempts += 1
        try:
            client.post_purge(seal(purge_payload(row, now=timezone.now()), signing_key()))
        except EnvelopeError as exc:
            error = exc.code  # a key problem does not fix itself between attempts
            break
        except client.DeliveryError as exc:
            error = exc.code
            continue
        PurgeOutbox.objects.filter(pk=row.pk).update(
            sent_at=timezone.now(),
            attempts=row.attempts + attempts,
            last_attempt_at=timezone.now(),
            last_error="",
        )
        logger.info("cache purge %s (%s) delivered, outbox row %s", row.mode, row.reason, row.pk)
        return True
    PurgeOutbox.objects.filter(pk=row.pk).update(
        attempts=row.attempts + attempts, last_attempt_at=timezone.now(), last_error=error[:64]
    )
    logger.warning(
        "cache purge %s (%s) not delivered after %s attempt(s): %s; outbox row %s stays pending",
        row.mode,
        row.reason,
        attempts,
        error,
        row.pk,
    )
    return False


def deliver_pending(*, limit: int = 100) -> tuple[int, int]:
    """Retry pending purges, oldest first. Returns (delivered, still pending)."""
    ids = list(
        PurgeOutbox.objects.filter(sent_at__isnull=True)
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    delivered = sum(deliver(pk) for pk in ids)
    return delivered, len(ids) - delivered


def organisations_owning(reference_ids: Iterable[int]) -> list[int]:
    """The organisations that own any of these originals, through `ownership` only.

    One query per organisation: there are a handful, and asking `owned_references` keeps the
    ownership rule single-sourced whatever it becomes.
    """
    ids = sorted(set(reference_ids))
    if not ids:
        return []
    return [
        organisation.pk
        for organisation in Organisation.objects.order_by("pk")
        if owned_references(organisation).filter(pk__in=ids).exists()
    ]


def purge_decided_references(reference_ids: Iterable[int]) -> list[PurgeOutbox]:
    """After confirm/reject/reset: expire the bundles of the organisations owning the originals."""
    if not enabled():
        return []
    return request_purge(
        PurgeOutbox.Mode.EXPIRE,
        reason="decision",
        organisation_ids=organisations_owning(reference_ids),
    )


def purge_flagged_page(page_id: int) -> list[PurgeOutbox]:
    """A page stopped being safety-clear: delete every bundle that contains it."""
    return request_purge(PurgeOutbox.Mode.DELETE, reason="safety_flag", page_ids=[page_id])


def flagged_pages_since(since: datetime) -> list[int]:
    """Pages a customer may no longer see whose safety state moved since `since`, and that could
    be in a bundle at all (a page no original was ever linked to is in none).

    The condition is `marceline.app.safety.hidden_page_filter`, the exact negation of what the
    customer views show — **not** "not safety-clear". A page can leave a customer's view while
    staying `clear`: `safety_scan` and a re-crawl rescore it, and a score that drops below
    `SAFETY_GATE_MIN_SCORE` (a near miss, thinner text, or the page's site having collected recent
    blocks) hides it exactly as a flag does (ADR 0018). Those writes are bulk `.update()`s or
    leave `safety_status` untouched, so no model signal sees them either; without this the
    production cache would keep serving that page's URL until the bundle expired.
    """
    linked = Exists(ReferenceLeak.objects.filter(page_id=OuterRef("pk"))) | Exists(
        ReferenceMatch.objects.filter(page_id=OuterRef("pk"))
    )
    return list(
        Page.objects.filter(hidden_page_filter())
        .filter(
            Q(safety_flagged_at__gte=since)
            | Q(safety_reviewed_at__gte=since)
            | Q(safety_scored_at__gte=since)
        )
        .filter(linked)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def sweep_flagged_pages(*, within: timedelta, now: datetime) -> list[PurgeOutbox]:
    """Catch safety flags written without model signals (bulk updates): `delete` purges."""
    with transaction.atomic():
        return request_purge(
            PurgeOutbox.Mode.DELETE,
            reason="safety_sweep",
            page_ids=flagged_pages_since(now - within),
        )
