"""Retention for deactivated pages (ADR 0029 amendment 2026-09-17, issue #71).

`deactivate_untargeted` (`marceline.targets`) deactivates pages of no target creator, never
deletes them: re-crawling one is expensive, and reactivating a page whose creator later signs is
a genuine purpose. Left as it was, that made the retention period "forever", so a bounded
retention period was added. Two separate things now happen, at two separate times:

- **At deactivation** (`marceline.targets.apply_activity` calls `scrub_deactivated_pages`), drop
  what is about the people the page named: `title`, `tags`, and the performer credits inside
  `raw_extraction` (the parser's own `"performers"` key and `crawler.sites.base
  .DROPPED_PERFORMERS_KEY`, `"dropped_performers"` — repeated here literally, since `index` may
  not import `crawler`). **Keep** the URL, every timestamp, the site, every `FrameHash`/`Media`
  and — unlike the fields above — `Page.creators`: `marceline.targets.apply_activity`'s own
  `linked` reactivation rule needs that link to notice a page again once its creator becomes a
  target, so clearing it would make deactivation one-way for exactly the pages this feature exists
  to let back in. Losing it from human view is instead handled at the aggregate, not the source:
  `index.breadcrumbs` counts only active pages, so a deactivated page's credit stops showing up
  in the site x creator breadcrumbs the moment it deactivates, without touching the page's own
  link. A page can credit several people; scrubbing only ever clears fields on the page itself,
  never the page row, so removing a name never removes the page.

  Two things make that scrub hold rather than be a one-off statement (issue #148):
  **`stays_scrubbed`**, which `crawler.services.store_parse_result` asks before its upsert, so a
  re-crawl of a page that is still deactivated does not write the three fields back (it used to,
  unconditionally); and **`backfill_scrub`** (CLI `scrub_inactive_pages`, dry run by default), the
  catch-up pass for everything deactivated before the scrub existed, which this ADR amendment
  explicitly left unscrubbed. `scrub_gap` is the invariant both are measured by — `make status`
  prints it, and it must read 0.
- **`PAGE_RETENTION_MONTHS` (24) after that** (`apply_retention`, cron `page_retention`), the page
  row itself is deleted outright — `Media`/`FrameHash` cascade with it. `Page.deactivated_at`
  is the clock: set when `apply_activity` deactivates a page, cleared the moment it reactivates,
  so a page whose target status changed inside the window never expires. Two things guard against
  deleting a page a safety case still needs, one of them structural and one an explicit check:
  - **`index.SafetyIncident.Status.OPEN`/`REPORTED`.** `keep_rules.safety` already keeps any
    non-`clear` page active forever, so a page currently blocked or under review is never even a
    *candidate* here. But a page can be `clear` again (a staff reversal) while it is still
    attached to an incident that is open or reported — nobody has closed the case yet, even
    though this particular page no longer trips a term. `apply_retention` excludes such pages
    from deletion explicitly (`Page.safety_incidents__incident__status`), unlike the notice
    protection below, this is checked up front, so a dry run sees it too.
  - A page a customer has drafted a real notice about is protected from deletion at the database
    level (`marceline.notices.models.NoticeFinding.page`, `on_delete=PROTECT`); `apply_retention`
    reports it as kept rather than failing the whole run, though a dry run cannot see that in
    advance (there is no query for it — the constraint is the only source of truth).

  `index.SafetyIncidentPage.page` is `on_delete=SET_NULL` by design (its URL is copied precisely
  so the incident survives the page row), and `index.SafetyIncidentHash` has no relationship to
  `Page` at all (it is copied off a page's hashes at the moment it is blocked, onto the incident,
  in the same step that deletes them from `FrameHash`) — so **nothing about deleting a page's row
  can corrupt a safety incident's own record**; the guard above is about not deleting evidence a
  still-open case might need, not about protecting the incident table itself.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.db.models.deletion import ProtectedError
from django.db.models.expressions import RawSQL
from django.db.models.functions import TruncDate
from django.utils import timezone

from core import deletion_journal
from index.incidents import months_ago
from index.models import Page, SafetyIncident

logger = logging.getLogger(__name__)

#: A page is never deleted while it is still attached to a safety case nobody has settled.
OPEN_SAFETY_STATUSES = (SafetyIncident.Status.OPEN, SafetyIncident.Status.REPORTED)

#: Keys inside `Page.raw_extraction` that hold performer credits (`crawler/services.py`
#: `store_parse_result`; `crawler.sites.base.DROPPED_PERFORMERS_KEY`). Literal strings: `index`
#: may not import `crawler` (module boundaries). A new performer-shaped extraction key needs an
#: entry here too.
PERFORMER_CREDIT_KEYS: tuple[str, ...] = ("performers", "dropped_performers")

#: How long a page stays deactivated before its row is deleted outright.
PAGE_RETENTION_MONTHS = 24


def scrub_deactivated_pages(page_ids: Sequence[int]) -> int:
    """Drop what is about people from pages just deactivated; keep what a reactivation needs.

    Clears `title`, `tags` and the performer-credit keys in `raw_extraction`. Deliberately never
    touches `Page.creators` (kept for the `linked` reactivation rule) or
    `active`/`inactive_reason`/`deactivated_at` (the caller,
    `marceline.targets.apply_activity`, decides those). Idempotent: a page with nothing left to
    clear is a no-op. Returns the number of pages updated.

    **A page that is not safety-clear is skipped, whatever the caller passes** (root `CLAUDE.md`,
    docs/suppression.md "Safety-flagged pages are off-limits here too"): on a flagged page the text
    *is* the safety record — what `safety_scan` re-judges, what the block was decided on and what a
    hotline report is built from. `marceline.targets.keep_rules.safety` keeps such a page active, so
    the deactivation path never hands one in; the filter here makes that hold for every caller,
    including the backfill below, rather than depending on each one (issue #148).
    """
    ids = list(page_ids)
    if not ids:
        return 0
    # One journal line per page that actually still holds something to clear (ADR 0057): a restore
    # from before the scrub brings the title, the tags and the performer credits back, and only the
    # journal can say which rows to scrub again. Read before the update, and not folded into it:
    # the returned count is "clear pages in `ids`", which stays what every caller expects.
    deletion_journal.record_many(
        "page_text_scrub",
        (
            {"page": pk}
            for pk in Page.objects.filter(pk__in=ids, safety_status=Page.SafetyStatus.CLEAR)
            .filter(holds_scrubbable_text())
            .order_by("pk")
            .values_list("pk", flat=True)
        ),
    )
    # jsonb `-` removes a top-level key, a no-op if it is already absent (idempotent); one
    # operator per key, built from PERFORMER_CREDIT_KEYS so the two always stay in step.
    strip_keys = " ".join("- %s" for _ in PERFORMER_CREDIT_KEYS)
    return Page.objects.filter(pk__in=ids, safety_status=Page.SafetyStatus.CLEAR).update(
        title="",
        tags=[],
        raw_extraction=RawSQL(f"raw_extraction {strip_keys}", list(PERFORMER_CREDIT_KEYS)),
    )


def stays_scrubbed(page: Page | None) -> bool:
    """Whether this **stored** page was scrubbed by a deactivation and must stay that way.

    The scrub is a deletion, not a display rule, so a later parse of the same URL may not write
    `title`, `tags` and the performer credits back: what the row may hold is decided by the
    deactivation, not by the crawl. `crawler.services.store_parse_result` asks this before its
    upsert — until issue #148 it rewrote all three unconditionally, so every inactive page a
    listing, a search or the media-link recovery re-crawled silently got its names back.

    A page that has since been reactivated (its creator became a target) answers False and is
    filled normally again by the next parse, and so does `None` — a page is born active.
    """
    return (
        page is not None
        and not page.active
        and page.inactive_reason == Page.InactiveReason.UNTARGETED
    )


def without_performer_credits(raw_extraction: Mapping[str, Any]) -> dict[str, Any]:
    """`raw_extraction` without the performer-credit keys, for a page that stays scrubbed.

    The other keys (`duration_s`, `external_links`, an adapter's flags) are not about people, and a
    re-crawl keeps them current — exactly the ones `scrub_deactivated_pages` leaves in place.
    """
    return {key: value for key, value in raw_extraction.items() if key not in PERFORMER_CREDIT_KEYS}


def holds_scrubbable_text() -> Q:
    """Pages still holding any of the three things a deactivation scrub removes."""
    return ~Q(title="") | ~Q(tags=[]) | Q(raw_extraction__has_any_keys=list(PERFORMER_CREDIT_KEYS))


def scrub_candidates() -> QuerySet[Page]:
    """What `backfill_scrub` would scrub: deactivated as untargeted, safety-clear, and still
    holding title, tags or performer credits. Never an active page, never a flagged one, never a
    page inactive for some other reason."""
    return Page.objects.filter(
        active=False,
        inactive_reason=Page.InactiveReason.UNTARGETED,
        safety_status=Page.SafetyStatus.CLEAR,
    ).filter(holds_scrubbable_text())


@dataclass(frozen=True)
class ScrubGap:
    """The cheap invariant behind issue #148, reported by `make status`.

    `holding_text` must be 0. Anything else means a deactivated page still carries the title, tags
    or performer credits that the Art. 30 record (`docs/legal/verzeichnis-verarbeitungstaetigkeiten
    .md`, V06) and `docs/legal/crawl-position.md` § 2 both state are deleted at deactivation —
    either because it was deactivated before the scrub existed (`scrub_inactive_pages` clears that
    stock) or because something wrote them back, which is the regression `stays_scrubbed` prevents.
    """

    #: Pages the targeted-indexing rule has deactivated at all.
    inactive: int
    #: Of those, safety-clear ones still holding scrubbable text: the number that must be 0.
    holding_text: int
    #: Of those, ones deliberately left untouched because the page is not safety-clear.
    safety_flagged: int

    @property
    def clean(self) -> bool:
        return self.holding_text == 0


def scrub_gap() -> ScrubGap:
    """One aggregate over the deactivated pages; writes nothing (`make status` is read-only)."""
    holds = holds_scrubbable_text()
    clear = Q(safety_status=Page.SafetyStatus.CLEAR)
    row = Page.objects.filter(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED
    ).aggregate(
        inactive=Count("pk"),
        holding=Count("pk", filter=clear & holds),
        flagged=Count("pk", filter=~clear & holds),
    )
    return ScrubGap(
        inactive=row["inactive"] or 0,
        holding_text=row["holding"] or 0,
        safety_flagged=row["flagged"] or 0,
    )


#: How many pages one statement of the backfill scrubs. Small enough that a run can be interrupted
#: at any point and resumed — the candidate query *is* the cursor, there is no stored position.
SCRUB_BATCH_SIZE = 2_000


@dataclass
class ScrubBackfillReport:
    dry_run: bool
    now: datetime
    batch_size: int
    limit: int | None
    #: Pages the run could scrub when it started.
    candidates: int
    #: Candidates per deactivation day (`YYYY-MM-DD`; `unknown` for a page with no clock).
    by_day: dict[str, int] = field(default_factory=dict)
    #: Inactive pages still holding text per `inactive_reason` — a reason this backfill does not
    #: handle would show up here and nowhere else.
    by_reason: dict[str, int] = field(default_factory=dict)
    #: Inactive pages still holding text that are **not** safety-clear: left untouched, reported.
    safety_flagged: int = 0
    #: Pages actually scrubbed (0 on a dry run).
    scrubbed: int = 0
    #: Candidates left when the run ended — non-zero after a `--limit`ed run, so the operator knows
    #: to run it again.
    remaining: int = 0


def backfill_scrub(
    *,
    dry_run: bool = True,
    batch_size: int = SCRUB_BATCH_SIZE,
    limit: int | None = None,
    now: datetime | None = None,
) -> ScrubBackfillReport:
    """Scrub the pages a past deactivation left unscrubbed (issue #148). Idempotent, resumable.

    `scrub_deactivated_pages` only ever ran for the pages one `apply_activity` call deactivated, so
    everything deactivated before it shipped kept its title, tags and credits. This is the catch-up
    pass: the same three fields through the same `scrub_deactivated_pages`, in batches that each
    commit on their own, so an interrupted run keeps what it did and a re-run continues where it
    stopped.

    It never touches an active page, never a page inactive for another reason, and never a page
    that is not safety-clear (twice over: the candidate query and `scrub_deactivated_pages`
    itself). A dry run writes nothing and only counts.
    """
    now = now or timezone.now()
    report = ScrubBackfillReport(
        dry_run=dry_run,
        now=now,
        batch_size=batch_size,
        limit=limit,
        candidates=scrub_candidates().count(),
        by_day=_scrub_candidates_by_day(),
        by_reason=_inactive_holding_by_reason(),
        safety_flagged=scrub_gap().safety_flagged,
    )
    if not dry_run:
        while limit is None or report.scrubbed < limit:
            size = batch_size if limit is None else min(batch_size, limit - report.scrubbed)
            ids = list(scrub_candidates().order_by("pk").values_list("pk", flat=True)[:size])
            if not ids:
                break
            with transaction.atomic():
                done = scrub_deactivated_pages(ids)
            if not done:  # unreachable: the update filters on the candidate query's own terms
                break
            report.scrubbed += done
        if report.scrubbed:
            logger.warning(
                "PAGE SCRUB BACKFILL cleared title, tags and performer credits on %s "
                "deactivated page(s) (issue #148)",
                report.scrubbed,
            )
    report.remaining = scrub_candidates().count()
    return report


def _scrub_candidates_by_day() -> dict[str, int]:
    rows = (
        scrub_candidates()
        .annotate(day=TruncDate("deactivated_at"))
        .values("day")
        .annotate(n=Count("pk"))
        .order_by("day")
    )
    return {(row["day"].isoformat() if row["day"] else "unknown"): row["n"] for row in rows}


def _inactive_holding_by_reason() -> dict[str, int]:
    rows = (
        Page.objects.filter(active=False)
        .filter(holds_scrubbable_text())
        .values("inactive_reason")
        .annotate(n=Count("pk"))
        .order_by("inactive_reason")
    )
    return {row["inactive_reason"] or "unknown": row["n"] for row in rows}


@dataclass
class PageRetentionReport:
    dry_run: bool
    now: datetime
    retain_months: int
    delete_before: datetime
    #: Deactivated pages with a retention clock running at all, any age.
    deactivated: int
    #: Due-for-deletion counts per site domain, excluding pages an open safety case still needs.
    by_site: dict[str, int] = field(default_factory=dict)
    #: Due, but kept for an open/reported safety incident. Known ahead of time, dry run included.
    safety_protected_ids: list[int] = field(default_factory=list)
    #: Populated on a real run only: ids actually deleted, and ids a `NoticeFinding` protected
    #: (unlike the safety case above, that one is a database constraint with no query for it).
    deleted_ids: list[int] = field(default_factory=list)
    protected_ids: list[int] = field(default_factory=list)

    @property
    def due(self) -> int:
        return sum(self.by_site.values())


def apply_retention(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    retain_months: int = PAGE_RETENTION_MONTHS,
) -> PageRetentionReport:
    """Delete pages deactivated longer than `retain_months`. Idempotent; `dry_run` only reports.

    Only pages with `active=False` and `deactivated_at` set are ever candidates — a page a target
    keeps active, or one reactivated since, has no clock running and is never touched here,
    whatever its age. A page still attached to an open or reported safety incident is excluded
    before anything is attempted (see the module docstring); a page a customer has drafted a real
    notice about is protected from deletion at the database level
    (`marceline.notices.models.NoticeFinding`, `on_delete=PROTECT`) and reported as kept rather
    than failing the whole run.
    """
    now = now or timezone.now()
    delete_before = months_ago(now, retain_months)
    clocked = Page.objects.filter(active=False, deactivated_at__isnull=False)
    deactivated = clocked.count()
    due = clocked.filter(deactivated_at__lte=delete_before)
    safety_protected_ids = sorted(
        due.filter(safety_incidents__incident__status__in=OPEN_SAFETY_STATUSES)
        .values_list("pk", flat=True)
        .distinct()
    )
    deletable = due.exclude(pk__in=safety_protected_ids)
    by_site = {
        row["site__domain"]: row["n"]
        for row in deletable.values("site__domain").annotate(n=Count("pk")).order_by("site__domain")
    }
    report = PageRetentionReport(
        dry_run=dry_run,
        now=now,
        retain_months=retain_months,
        delete_before=delete_before,
        deactivated=deactivated,
        by_site=by_site,
        safety_protected_ids=safety_protected_ids,
    )
    if dry_run:
        return report
    for pk, url in deletable.order_by("pk").values_list("pk", "url"):
        try:
            with transaction.atomic():
                # Journalled inside the deletion's own transaction (ADR 0057): the line is written
                # on commit, so a page a `NoticeFinding` protects leaves no entry. The URL's digest
                # travels with the id so a replay can prove it is deleting the row the journal
                # means before it deletes anything — the URL itself never goes into the file.
                deletion_journal.record(
                    "page_deleted", page=pk, url_sha256=sha256(url.encode()).hexdigest()
                )
                Page.objects.filter(pk=pk).delete()
            report.deleted_ids.append(pk)
        except ProtectedError:
            report.protected_ids.append(pk)
    if report.deleted_ids:
        # Its Page.creators links cascade away with the row; nothing recomputes because a
        # deactivated page never counted towards a breadcrumb in the first place
        # (index.breadcrumbs counts active pages only).
        logger.warning(
            "PAGE RETENTION deleted %s page(s) deactivated before %s "
            "(%s protected by a notice, %s protected by an open safety case)",
            len(report.deleted_ids),
            delete_before.date().isoformat(),
            len(report.protected_ids),
            len(report.safety_protected_ids),
        )
    return report
