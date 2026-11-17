"""Safety incidents: one case per genuinely new finding, reported once, kept only as long as needed.

An incident is the record of a content-safety case (docs/safety.md, issue #66): what was flagged,
where, when, whether it was reported to a hotline and what the hotline answered. It replaces the
free-text `Page.safety_note` field.

**Deduplication is the point.** An automated feed must never flood a hotline with the same finding.
Three distinct cases are handled here, and each one is named in `SafetyIncidentPage.Evidence`:

(a) `same_url` — the same page seen again on a re-crawl. Its active incident absorbs the sighting
    (`last_seen_at` moves); no new case, no second report.
(b) `mirror` / `sha256` / `cluster` — the same content at a different URL or on another site. The
    caller supplies the evidence as `Related` rows, because deciding what "the same content" means
    is the matching app's job (`marceline/safety_incidents.py`; `index` must not import `matching`).
    The mirrored page is attached to the existing case as another page of it.
(c) `term_change` — the same page flagged again after the term list changed. While the case is
    still open or reported the new term is merged into it. Once a human has settled the case,
    a term it was already judged for never reopens it; a term it was *not* judged for does open a
    new case, which is exactly the rule docs/safety.md already states for `safety_reviewed_at`.
(d) `pdq` — the same material recognised by a hash the case kept (ADR 0011 amendment 2026-09-17).
    When a page is blocked, the PDQ hashes it already had (computed while it was clear, before the
    gate closed) are moved from `index.FrameHash` onto its case as `SafetyIncidentHash` rows —
    deleted from `FrameHash` as before, so no matcher ever sees them. A later blocked page whose
    own retained hash is within `HASH_MAX_DISTANCE` of one of them is the same material and joins
    that case. Nothing is fetched for this, and a page that never had a hash simply has none.

**Never any page content** enters an incident: URL, timestamps, category, term and reporting
metadata only. Nothing here reads `page.title`, `page.tags` or `page.raw_extraction`.

Retention (operator decision, 2026-09-16): keep the record while the case is open, then twelve
months after it closes, then delete it — URLs, terms and the hashes it kept, together. Long enough
to prove to a hotline or a prosecutor what was found and when, short enough to defend under GDPR
Art. 5. `apply_retention` implements it and is idempotent; the `safety_retention` command is the
cron entry point.
"""

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, QuerySet
from django.utils import timezone

from core import deletion_journal
from index.models import (
    FrameHash,
    Media,
    Page,
    SafetyIncident,
    SafetyIncidentHash,
    SafetyIncidentPage,
)

logger = logging.getLogger(__name__)

Evidence = SafetyIncidentPage.Evidence
Status = SafetyIncident.Status

#: Cases that still absorb new sightings instead of opening a second case.
ACTIVE_STATUSES = (Status.OPEN, Status.REPORTED)
#: Cases a human has settled. They neither absorb nor block a genuinely new term.
SETTLED_STATUSES = (Status.CLOSED, Status.FALSE_POSITIVE)

#: A reported case is closed automatically after this long without news from the hotline. Without
#: it an open-ended case would keep its record forever, which is what retention exists to prevent.
#: A necessity review is due at least every 90 days, so that is the default.
AUTO_CLOSE_AFTER_DAYS = 90
#: Months a closed case is kept before the whole record is deleted.
RETAIN_MONTHS_AFTER_CLOSE = 12

#: PDQ distance at which two retained hashes count as the same material (deduplication case (d)).
#: Far tighter than matching's own 31: a wrong link here would fold two distinct findings into one
#: case and silence a report, which is the one failure direction docs/safety.md refuses. Only
#: near-identical pictures — a re-encode or a re-upload of the same file — get under this.
HASH_MAX_DISTANCE = 8
#: Hashes kept per case. A poster plus a clip's frames can be dozens; a handful of the sharpest
#: ones answers "is this the same material?" just as well, and this record stays as small as its
#: purpose allows.
MAX_RETAINED_HASHES = 16


@dataclass(frozen=True)
class Related:
    """Another page that is known to carry the same content (deduplication case (b))."""

    page_id: int
    evidence: str


@dataclass(frozen=True)
class RetainedHash:
    """One PDQ hash of a blocked page, on its way from `FrameHash` onto the case.

    It carries no URL, no bytes and nothing about the page beyond which media kind it came from.
    """

    pdq: bytes
    quality: int
    source_kind: str = ""


@dataclass(frozen=True)
class Decision:
    """What happened (or, in a dry run, what would happen) for one flagged page."""

    action: str  # created | linked | settled
    evidence: str
    incident_id: int | None = None
    incident: SafetyIncident | None = None


def page_terms(page: Page) -> list[dict[str, str]]:
    """The page's matched terms as {category, term}, deduplicated and ordered. No page content."""
    raw = page.safety_matches if isinstance(page.safety_matches, list) else []
    seen = {
        (str(match.get("category", "")), str(match.get("term", "")))
        for match in raw
        if isinstance(match, dict)
    }
    return [{"category": category, "term": term} for category, term in sorted(seen)]


def _term_keys(terms: Iterable[object]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for term in terms:
        if isinstance(term, dict):
            keys.add((str(term.get("category", "")), str(term.get("term", ""))))
    return keys


def sha256_related(page: Page) -> list[Related]:
    """Pages whose media has bytes identical to this page's (deduplication case (b)).

    The only same-content evidence `index` can produce on its own: it needs no hashes, no cluster
    and no request. `marceline.safety_incidents.related_pages` adds mirror keys and clusters.
    """
    digests = [
        sha
        for sha in Media.objects.filter(page=page).values_list("sha256", flat=True).distinct()
        if sha
    ]
    if not digests:
        return []
    others = (
        Media.objects.filter(sha256__in=digests)
        .exclude(page_id=page.pk)
        .values_list("page_id", flat=True)
        .distinct()
    )
    return [Related(page_id=other, evidence=Evidence.SHA256) for other in sorted(set(others))]


def collect_page_hashes(
    page_ids: Sequence[int], *, limit: int = MAX_RETAINED_HASHES
) -> dict[int, list[RetainedHash]]:
    """The PDQ hashes these pages have *right now*, read so a block can move them onto the case.

    Call this immediately before `index.services.cancel_page_media` deletes them, and only for
    pages a rule blocks with an incident. Nothing is fetched: these hashes were computed while the
    page was still clear, and after the block they exist only on the case (or nowhere).

    The best-quality hashes come first and at most `limit` per page are kept.
    """
    if not page_ids:
        return {}
    rows = (
        FrameHash.objects.filter(media__page_id__in=list(page_ids))
        .order_by("media__page_id", "-quality", "media_id", "frame_index")
        .values_list("media__page_id", "pdq", "quality", "media__kind")
    )
    out: dict[int, list[RetainedHash]] = {}
    for page_id, pdq, quality, kind in rows:
        kept = out.setdefault(int(page_id), [])
        if len(kept) >= limit:
            continue
        kept.append(RetainedHash(pdq=bytes(pdq), quality=int(quality), source_kind=str(kind)))
    return out


def pdq_distance(first: bytes, second: bytes) -> int:
    """Hamming distance of two 256-bit PDQ hashes.

    Deliberately local arithmetic: `index` may not import `matching` (module boundaries), and
    these hashes must never travel through the matcher's code in either direction.
    """
    return (int.from_bytes(first, "big") ^ int.from_bytes(second, "big")).bit_count()


def hash_related_incident(
    hashes: Sequence[RetainedHash], *, max_distance: int = HASH_MAX_DISTANCE
) -> SafetyIncident | None:
    """The still-running case whose retained hash is nearest to one of `hashes`, if any.

    Deduplication case (d). A plain scan in Python: this table holds a handful of hashes per case
    and only for cases nobody has settled yet, and it must never be joined to anything the
    matcher queries.
    """
    if not hashes:
        return None
    rows = SafetyIncidentHash.objects.filter(incident__status__in=ACTIVE_STATUSES).values_list(
        "incident_id", "pdq"
    )
    best: tuple[int, int] | None = None
    for incident_id, stored in rows:
        stored_bytes = bytes(stored)
        distance = min(pdq_distance(candidate.pdq, stored_bytes) for candidate in hashes)
        if distance > max_distance:
            continue
        if best is None or distance < best[1]:
            best = (int(incident_id), distance)
    if best is None:
        return None
    return SafetyIncident.objects.filter(pk=best[0]).first()


def retain_hashes(
    incident: SafetyIncident, hashes: Sequence[RetainedHash], *, limit: int = MAX_RETAINED_HASHES
) -> int:
    """Keep these hashes on the case, up to `limit` per case. Returns how many rows were added.

    Idempotent (a hash a case already holds is not stored twice) and purpose-bound: the rows exist
    to recognise the same material again, they go when the case goes, and nothing else reads them.
    """
    if not hashes:
        return 0
    held = {
        bytes(value)
        for value in SafetyIncidentHash.objects.filter(incident=incident).values_list(
            "pdq", flat=True
        )
    }
    room = limit - len(held)
    if room <= 0:
        return 0
    new: list[SafetyIncidentHash] = []
    for candidate in sorted(hashes, key=lambda item: -item.quality):
        if len(new) >= room or candidate.pdq in held:
            continue
        held.add(candidate.pdq)
        new.append(
            SafetyIncidentHash(
                incident=incident,
                pdq=candidate.pdq,
                quality=candidate.quality,
                source_kind=candidate.source_kind,
            )
        )
    if not new:
        return 0
    SafetyIncidentHash.objects.bulk_create(new, ignore_conflicts=True)
    return len(new)


def _incidents_for(page_ids: Sequence[int]) -> QuerySet[SafetyIncidentPage]:
    return SafetyIncidentPage.objects.select_related("incident").filter(page_id__in=page_ids)


def _attach(
    incident: SafetyIncident, page: Page, *, evidence: str, now: datetime
) -> SafetyIncidentPage:
    link, created = SafetyIncidentPage.objects.get_or_create(
        incident=incident,
        url=page.url,
        defaults={
            "page": page,
            "site_domain": page.site.domain,
            "evidence": evidence,
            "first_seen_at": page.safety_flagged_at or now,
            "last_seen_at": now,
        },
    )
    if not created:
        link.page = page
        link.last_seen_at = now
        link.save(update_fields=("page", "last_seen_at"))
    return link


def _absorb(
    incident: SafetyIncident,
    page: Page,
    *,
    evidence: str,
    terms: list[dict[str, str]],
    now: datetime,
) -> None:
    """Record another sighting on an existing case: newer `last_seen_at`, any new term merged."""
    merged = sorted(
        _term_keys(incident.terms if isinstance(incident.terms, list) else []) | _term_keys(terms)
    )
    incident.terms = [{"category": category, "term": term} for category, term in merged]
    incident.last_seen_at = now
    incident.save(update_fields=("terms", "last_seen_at", "updated_at"))
    _attach(incident, page, evidence=evidence, now=now)


def open_or_link(
    page: Page,
    *,
    related: Sequence[Related] = (),
    retained: Sequence[RetainedHash] = (),
    cluster_ref: int | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> Decision:
    """Open a case for this flagged page, or attach it to the case it already belongs to.

    `related` carries deduplication case (b): other pages known to hold the same content, most
    trustworthy evidence first. `retained` carries case (d): the PDQ hashes this page had when it
    was blocked, moved out of `FrameHash` by the caller. They are compared against the hashes
    running cases already keep, and then kept on whichever case this page ends up in — so the next
    re-encoded mirror is recognised instead of reported again. Returns what happened; nothing is
    written when `dry_run`.
    """
    now = now or timezone.now()
    terms = page_terms(page)
    keys = _term_keys(terms)

    with transaction.atomic():
        links = list(_incidents_for([page.pk]).order_by("-incident__detected_at", "-incident_id"))
        # (a) and (c): this page already has a case that is still running.
        active = next(
            (link.incident for link in links if link.incident.status in ACTIVE_STATUSES), None
        )
        if active is not None:
            new_terms = keys - _term_keys(active.terms if isinstance(active.terms, list) else [])
            evidence = Evidence.TERM_CHANGE if new_terms else Evidence.SAME_URL
            if not dry_run:
                _absorb(active, page, evidence=evidence, terms=terms, now=now)
                retain_hashes(active, retained)
            return Decision("linked", evidence, active.pk, active)

        # (c) settled: a human's decision stands for the terms it was made about (docs/safety.md).
        settled = next(
            (link.incident for link in links if link.incident.status in SETTLED_STATUSES), None
        )
        if settled is not None:
            judged = _term_keys(settled.terms if isinstance(settled.terms, list) else [])
            if keys <= judged:
                return Decision("settled", Evidence.TERM_CHANGE, settled.pk, settled)

        # (b) the same content is already a case somewhere else.
        related_ids = [rel.page_id for rel in related]
        if related_ids:
            by_page = {
                link.page_id: link.incident
                for link in _incidents_for(related_ids)
                if link.incident.status in ACTIVE_STATUSES
            }
            for rel in related:
                other = by_page.get(rel.page_id)
                if other is None:
                    continue
                if not dry_run:
                    _absorb(other, page, evidence=rel.evidence, terms=terms, now=now)
                    retain_hashes(other, retained)
                return Decision("linked", rel.evidence, other.pk, other)

        # (d) the same material, recognised by a hash a running case kept. The last net, because
        # the evidence above identifies a page and this one identifies a picture.
        by_hash = hash_related_incident(retained)
        if by_hash is not None:
            if not dry_run:
                _absorb(by_hash, page, evidence=Evidence.PDQ, terms=terms, now=now)
                retain_hashes(by_hash, retained)
            return Decision("linked", Evidence.PDQ, by_hash.pk, by_hash)

        if dry_run:
            return Decision("created", Evidence.ORIGIN, None, None)
        incident = SafetyIncident.objects.create(
            status=Status.OPEN,
            category=terms[0]["category"] if terms else "",
            term=terms[0]["term"] if terms else "",
            terms=terms,
            detected_at=page.safety_flagged_at or now,
            last_seen_at=now,
            cluster_ref=cluster_ref,
        )
        _attach(incident, page, evidence=Evidence.ORIGIN, now=now)
        retain_hashes(incident, retained)
    logger.warning(
        "SAFETY INCIDENT opened incident=%s page=%s site=%s category=%s",
        incident.pk,
        page.pk,
        page.site.domain,
        incident.category or "manual",
    )
    return Decision("created", Evidence.ORIGIN, incident.pk, incident)


def _append_note(incident: SafetyIncident, line: str) -> None:
    incident.note = f"{incident.note.rstrip()}\n{line}".strip() if incident.note else line


def mark_reported(
    incident: SafetyIncident,
    *,
    reference: str = "",
    via: str = "",
    report_category: str = "",
    by: str = "",
    note: str = "",
    reported_at: datetime | None = None,
) -> SafetyIncident:
    """A human filed the report. `reference` stays blank when the hotline issues no number."""
    incident.status = Status.REPORTED
    incident.reported_at = reported_at or incident.reported_at or timezone.now()
    if via:
        incident.reported_via = via
    if reference:
        incident.reference = reference
    if report_category:
        incident.report_category = report_category
    if by:
        incident.decided_by = by
    if note:
        _append_note(incident, note)
    incident.save()
    return incident


def mark_closed(
    incident: SafetyIncident, *, by: str = "", note: str = "", now: datetime | None = None
) -> SafetyIncident:
    """The case is finished. This starts the twelve-month retention clock."""
    incident.status = Status.CLOSED
    incident.closed_at = incident.closed_at or (now or timezone.now())
    if by:
        incident.decided_by = by
    if note:
        _append_note(incident, note)
    incident.save()
    return incident


def mark_false_positive(
    incident: SafetyIncident, *, by: str = "", note: str = "", now: datetime | None = None
) -> SafetyIncident:
    """The flag was wrong (judged from text only). Also a closed case for retention purposes."""
    incident.status = Status.FALSE_POSITIVE
    incident.closed_at = incident.closed_at or (now or timezone.now())
    if by:
        incident.decided_by = by
    if note:
        _append_note(incident, note)
    incident.save()
    return incident


def months_ago(now: datetime, months: int) -> datetime:
    """`now` minus whole calendar months, clamping the day (31 March - 1 month = 28/29 February)."""
    month_index = (now.year * 12 + now.month - 1) - months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(now.day, _days_in_month(year, month))
    return now.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


@dataclass
class RetentionReport:
    dry_run: bool
    now: datetime
    close_after_days: int
    retain_months: int
    close_before: datetime
    delete_before: datetime
    total: int = 0
    by_status: Counter[str] = field(default_factory=Counter)
    closed: int = 0
    deleted: int = 0
    deleted_ids: list[int] = field(default_factory=list)
    open_unreported: int = 0
    kept: int = 0


def apply_retention(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    close_after_days: int = AUTO_CLOSE_AFTER_DAYS,
    retain_months: int = RETAIN_MONTHS_AFTER_CLOSE,
) -> RetentionReport:
    """Close stale reported cases, then delete cases closed longer ago than the retention period.

    Idempotent and safe to run from cron: a case closed by this run is not deleted by it, and a
    second run in the same minute changes nothing. Open cases are never closed and never deleted —
    an open case is work waiting for a human, and the report counts it separately.
    """
    now = now or timezone.now()
    close_before = now - timedelta(days=close_after_days)
    delete_before = months_ago(now, retain_months)
    report = RetentionReport(
        dry_run=dry_run,
        now=now,
        close_after_days=close_after_days,
        retain_months=retain_months,
        close_before=close_before,
        delete_before=delete_before,
    )
    report.total = SafetyIncident.objects.count()
    for row in SafetyIncident.objects.values("status").annotate(n=Count("pk")):
        report.by_status[str(row["status"])] = int(row["n"])
    report.open_unreported = SafetyIncident.objects.filter(status=Status.OPEN).count()

    stale = SafetyIncident.objects.filter(status=Status.REPORTED, reported_at__lte=close_before)
    report.closed = stale.count()
    if not dry_run and report.closed:
        for incident in stale:
            mark_closed(
                incident,
                by="safety_retention",
                note=(
                    f"Closed automatically {now.date().isoformat()}: reported "
                    f"{close_after_days} days ago and no further news. Retention clock starts now."
                ),
                now=now,
            )

    expired = SafetyIncident.objects.filter(
        status__in=SETTLED_STATUSES, closed_at__lte=delete_before
    )
    report.deleted_ids = sorted(expired.values_list("pk", flat=True))
    report.deleted = len(report.deleted_ids)
    if not dry_run and report.deleted:
        # ADR 0057: a restore from before this pass brings a settled case, its copied URL and its
        # retained hashes back. The journal carries the incident id and nothing else — no URL, no
        # matched term, no page content — so `reapply_deletions` can delete it again.
        deletion_journal.record_many(
            "safety_incident_deleted", ({"incident": pk} for pk in report.deleted_ids)
        )
        SafetyIncident.objects.filter(pk__in=report.deleted_ids).delete()
        logger.warning(
            "SAFETY INCIDENT retention deleted %s record(s) closed before %s",
            report.deleted,
            delete_before.date().isoformat(),
        )
    report.kept = report.total - report.deleted
    return report
