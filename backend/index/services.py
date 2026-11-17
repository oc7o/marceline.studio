"""Index workflows shared by the crawler, the media worker and the admin: content-safety state.

Flagged pages never have their media fetched (docs/safety.md). Blocked pages lose their media
queue entries and frame hashes; media of pages under review wait until a reviewer clears them.

On top of that hard deny, ADR 0018 adds the media gate: a media row may only be requested once
the *detail page it belongs to* has been fetched, parsed and scored clearly safe. Eligibility is
carried by `Media.eligible_at` (NULL = never fetch this), so media discovered on a listing card
is recorded but stays inert until its own page's text has been judged.
"""

import logging
from collections.abc import Iterable
from datetime import datetime

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from index.models import FrameHash, Media, Page, SafetyRuleDecision

logger = logging.getLogger(__name__)


def media_hosts_by_site() -> list[tuple[int, str]]:
    """Every distinct (site id, lower-case host) media URLs point to, capture rows excluded.

    Input for the site host roles (`catalog.request_accounting.record_observed_hosts`, issue #74);
    one scan of the media table, run daily by the scheduler.
    """
    sql = (
        "SELECT DISTINCT p.site_id, lower(substring(m.url from '^[A-Za-z][A-Za-z0-9+.-]*://"
        "(?:[^/@?#]*@)?([^/:?#]+)')) "
        f"FROM {Media._meta.db_table} m JOIN {Page._meta.db_table} p ON p.id = m.page_id "
        "WHERE m.kind <> %s"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [Media.Kind.CAPTURE])
        return [(int(site_id), host) for site_id, host in cursor.fetchall() if host]


# Media skip reasons. Released media are recognised by them, so keep them stable.
SAFETY_BLOCK_REASON = "safety block"
SAFETY_REVIEW_REASON = "safety review"
# Held back by the score gate (ADR 0018): the page is not flagged, but its text is not clearly
# safe enough to fetch pictures from. Released again when a later score clears it.
SAFETY_GATE_REASON = "safety gate"
SAFETY_REASONS = (SAFETY_BLOCK_REASON, SAFETY_REVIEW_REASON, SAFETY_GATE_REASON)


def gate_min_score() -> float:
    return float(getattr(settings, "SAFETY_GATE_MIN_SCORE", 0.80))


def page_media_allowed(safety_status: str | None, safety_score: float | None) -> bool:
    """Whether this page's media may be fetched at all: not flagged, and scored clearly safe.

    A page that was never scored (`safety_score is None`) is not allowed: nothing has judged its
    own text yet. This is the single rule the queue, the fetch guard and the admin all use.
    """
    if safety_status != Page.SafetyStatus.CLEAR:
        return False
    return safety_score is not None and safety_score >= gate_min_score()


def media_denial_reason(safety_status: str | None, safety_score: float | None) -> str:
    """Why this page's media must not be fetched (only call when `page_media_allowed` is False)."""
    if safety_status == Page.SafetyStatus.BLOCKED:
        return SAFETY_BLOCK_REASON
    if safety_status == Page.SafetyStatus.REVIEW:
        return SAFETY_REVIEW_REASON
    return SAFETY_GATE_REASON


def cancel_page_media(page_ids: Iterable[int]) -> tuple[int, int]:
    """Skip every not-yet-skipped media row of these pages and delete their frame hashes.

    Returns (media cancelled, frame hashes deleted). Only hashes are deleted; no media is stored.
    """
    ids = list(page_ids)
    with transaction.atomic():
        hashes, _ = FrameHash.objects.filter(media__page_id__in=ids).delete()
        Media.objects.filter(page_id__in=ids).exclude(eligible_at__isnull=True).update(
            eligible_at=None
        )
        cancelled = (
            Media.objects.filter(page_id__in=ids)
            .exclude(status=Media.Status.SKIPPED)
            .update(
                status=Media.Status.SKIPPED,
                last_error=SAFETY_BLOCK_REASON,
                locked_at=None,
                next_attempt_at=None,
            )
        )
    return cancelled, hashes


def release_page_media(page_ids: Iterable[int], *, now: datetime | None = None) -> int:
    """Put media skipped for safety reasons back into the fetch queue and make it eligible.

    Only called for pages that may have media again; the caller has already checked the gate.
    """
    now = now or timezone.now()
    return Media.objects.filter(
        page_id__in=list(page_ids), status=Media.Status.SKIPPED, last_error__in=SAFETY_REASONS
    ).update(
        status=Media.Status.PENDING,
        attempts=0,
        next_attempt_at=None,
        last_error="",
        eligible_at=now,
    )


def allow_page_media(page_ids: Iterable[int], *, now: datetime | None = None) -> int:
    """Grant eligibility to the media of pages that may be fetched (ADR 0018).

    Only a detail-page parse grants eligibility, so media stored before the gate existed — or
    attached while its page was still gated — keeps `eligible_at = NULL` and would never be
    requested again, however safe the page turns out to be. `scan_pages` is the backfill and
    grants here; `release_page_media` decides separately which *skipped* rows come back.

    Browser-capture rows (ADR 0016) are never granted: they hold hashes, not a fetchable URL.

    Returns the number of rows that became eligible.
    """
    return (
        Media.objects.filter(page_id__in=list(page_ids), eligible_at__isnull=True)
        .exclude(status=Media.Status.SKIPPED)
        .exclude(kind=Media.Kind.CAPTURE)
        .update(eligible_at=now or timezone.now())
    )


def gate_page_media(
    page_ids: Iterable[int], *, reason: str | None = SAFETY_GATE_REASON, now: datetime | None = None
) -> int:
    """Hold back the media of pages that are not clearly safe (ADR 0018).

    The rows and their URLs are kept — they are what a later, better-informed crawl updates — but
    they lose their eligibility, so no request can be made for them. Frame hashes are left alone:
    a gated page is not a flagged page, and `cancel_page_media` handles blocking.

    `reason` names the skip written on pending rows. Pages under review pass `None`: their media
    keeps waiting as PENDING exactly as before (docs/safety.md), it is simply no longer eligible.
    Returns the number of pending rows that were skipped.
    """
    ids = list(page_ids)
    if not ids:
        return 0
    with transaction.atomic():
        Media.objects.filter(page_id__in=ids).exclude(eligible_at__isnull=True).update(
            eligible_at=None
        )
        if reason is None:
            return 0
        return Media.objects.filter(page_id__in=ids, status=Media.Status.PENDING).update(
            status=Media.Status.SKIPPED,
            last_error=reason,
            locked_at=None,
            next_attempt_at=None,
        )


def log_flag(page: Page, categories: Iterable[str]) -> None:
    """One WARNING line per newly flagged page: page id, site id and categories only.

    Never the URL or any content: WARNING lines leave the machine (Sentry, log shipping), and a
    flagged page's address is exactly what must not travel. The admin shows it to those who may.
    """
    if page.safety_status == Page.SafetyStatus.CLEAR:
        return
    label = "SAFETY BLOCK" if page.safety_status == Page.SafetyStatus.BLOCKED else "SAFETY REVIEW"
    logger.warning(
        "%s page=%s site=%s categories=%s",
        label,
        page.pk,
        page.site_id,
        ",".join(sorted(set(categories))) or "manual",
    )


def set_safety_status(
    page: Page,
    status: Page.SafetyStatus,
    *,
    note: str = "",
    now: datetime | None = None,
    by: str = "",
    log_kind: str | None = SafetyRuleDecision.Kind.STAFF_STATUS,
) -> None:
    """A reviewer's decision: record it, then cancel (blocked) or release (clear) the media.

    Logged as a `SafetyRuleDecision` (kind `log_kind`, `None` when the caller logs its own), so
    reversing an auto-block is as auditable as the block itself.
    """
    now = now or timezone.now()
    with transaction.atomic():
        if log_kind is not None:
            SafetyRuleDecision.objects.create(
                page=page,
                kind=log_kind,
                status=status,
                terms=page.safety_matches if isinstance(page.safety_matches, list) else [],
                rule_version=page.safety_rule_version,
                decided_by=by,
                note=note,
            )
        page.safety_status = status
        page.safety_reviewed_at = now
        if note:
            page.safety_note = note
        if status != Page.SafetyStatus.CLEAR and page.safety_flagged_at is None:
            page.safety_flagged_at = now
        page.save(
            update_fields=(
                "safety_status",
                "safety_reviewed_at",
                "safety_note",
                "safety_flagged_at",
            )
        )
        if status == Page.SafetyStatus.BLOCKED:
            cancel_page_media([page.pk])
        elif status == Page.SafetyStatus.CLEAR:
            # Clearing a page answers the term match, not the score: a page a reviewer cleared
            # may still be gated (ADR 0018), and then its media stays unfetched.
            if page_media_allowed(page.safety_status, page.safety_score):
                release_page_media([page.pk], now=now)
            else:
                gate_page_media([page.pk])
