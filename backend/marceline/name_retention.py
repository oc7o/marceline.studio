"""Retention for a person's name in the catalogue, and for prospect research (ADR 0048, #104).

The house pattern of `index/retention.py` (ADR 0029), `billing/retention.py` (ADR 0032) and
`marceline/deletion/retention.py` (ADR 0045): a dry run by default, counts and ids only, protected
rows reported as kept rather than failing the run, idempotent, scheduled last and off until
`RESEARCH_RETENTION_ENABLED`.

**Never a name in the output or in a log line.** Creator ids, page counts and reason codes — the
name is the thing being deleted, and a report that repeated it would be the copy that survives.

Two passes live here; the third, the discovery query text, is `crawler/retention.py`.

## `prospects` — research nobody acted on

A `catalog.WatchlistEntry` with `purpose=prospect` older than `PROSPECT_RESEARCH_RETENTION_DAYS` is
forgotten: the entry, the `index.ProspectSampleCheck` verdicts, the `Page.creators` links and the
`index.SiteCreatorBreadcrumb` counts. That is the list of four things the first-contact
notice promises to delete within three months, counted from the day the prospect was put on the
list. A new sample verdict does **not** extend it: a rolling clock keeps research alive exactly as
long as we keep looking at it, which is what the promise forbids. A prospect who signs leaves this
rule by becoming a rights holder or co-performer target; one still in conversation at day 90 is a
conversation, and the operator raises the period or converts them.

The forgotten name is then deleted **in the same run** if nothing else uses it, with no second
clock:
the research was the only reason we held it.

## `names` — a name is kept while it is used

`keep_reasons` is the single rule, and every code it can return is a purpose:

- `suppression`: any `catalog.CreatorSuppression` row, active **or lifted**. The marker is what
  keeps
  the name out of the index; deleting it would undo the request, and a lifted row is the record of a
  request and its lift. The database enforces this too (`PROTECT` on the creator), so this rule is
  belt and braces, not the only guard;
- `customer_original`: primary creator or cast member of a `matching.Reference`. Customer data
  (ADR 0031/0034), whose retention is ADR 0045 — including an unassigned original, which is ours but
  is not research;
- `target`: in `marceline.targets.targets()` — whoever we hunt today;
- `watchlist`: any `catalog.WatchlistEntry`. Noted interest is its own purpose; a prospect entry
  past
  its period is deleted by the pass above first, and then this rule no longer holds;
- `active_page`: linked to at least one **active** `index.Page`;
- `safety_record`: named by `index.Page.safety_release_creator` or
  `index.SafetyRuleDecision.creator`.
  Both are `SET_NULL`, so deleting the name would quietly empty a field of a safety record;
- `source_list`: the name is in `sources.md`. `seed_sources` re-creates every name on that list, so
  expiring one would have the two fight each other nightly. **The pass refuses to run when the file
  cannot be read** — an unreadable keep rule must not look like an empty one.

`needs_review` is deliberately not a keep rule: such a name is already out of everything
(`Creator.objects.usable()`), and if it is in a reference's cast `customer_original` keeps it.

A name with no reason gets `marceline.CreatorRetentionClock.unused_since`, set the first time the
pass sees it unused and cleared the moment any rule holds again. `CREATOR_NAME_RETENTION_DAYS` later
the `Creator` row is deleted; the cascade takes its breadcrumbs, prospect verdicts,
candidate-creator
pointers and `Page.creators` links — **including the links on deactivated pages** that ADR 0029
keeps
for its `linked` reactivation rule. ADR 0048 weighs that trade-off: the link is the last statement
on
a scrubbed page that names the person, and what it buys (reactivating a page instead of re-finding
it
by seeding the name) is reproducible. The page row itself is never touched here; its own clock is
`page_retention`.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from catalog.models import Creator, CreatorSuppression, WatchlistEntry
from catalog.sources import parse_sources
from core import deletion_journal
from crawler.sites.common import normalize_name
from index.models import Page, ProspectSampleCheck, SafetyRuleDecision, SiteCreatorBreadcrumb
from marceline.models import CreatorMergeLog, CreatorRetentionClock
from marceline.targets import TargetKind, targets
from matching.models import Reference

logger = logging.getLogger(__name__)

#: Days a prospect's research is kept, counted from the day the watchlist entry was added. The
#: Art. 14 first-contact draft promises three months; this is that promise.
PROSPECT_RESEARCH_RETENTION_DAYS = 90

#: Days a name stays after the pass first saw nothing using it.
CREATOR_NAME_RETENTION_DAYS = 90

# Keep reasons. A kept name is always reported with one of these, never silently skipped.
KEEP_SUPPRESSION = "suppression"
KEEP_CUSTOMER_ORIGINAL = "customer_original"
KEEP_TARGET = "target"
KEEP_WATCHLIST = "watchlist"
KEEP_ACTIVE_PAGE = "active_page"
KEEP_SAFETY_RECORD = "safety_record"
KEEP_SOURCE_LIST = "source_list"
#: Reported after a real run only: the database refused the deletion (a suppression marker, a
#: reference that still names it). Never allowed to fail the run.
KEEP_PROTECTED = "protected"


class RetentionRefused(Exception):
    """A keep rule could not be read, so the pass must not run (an unreadable `sources.md`)."""


def sources_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else Path(settings.BASE_DIR).parent / "sources.md"


def source_list_names(path: str | Path | None = None) -> set[str]:
    """The normalised names `seed_sources` would (re-)create, from `sources.md`.

    Raises `RetentionRefused` when the file is missing or unreadable: it is a keep rule, and a keep
    rule nobody could read must not be mistaken for an empty one.
    """
    file = sources_path(path)
    try:
        text = file.read_text()
    except OSError as exc:
        raise RetentionRefused(
            f"{file} could not be read ({type(exc).__name__}), and it is a keep rule: every name "
            "in it is re-created by `seed_sources`. Pass --sources or fix the file; nothing was "
            "changed."
        ) from exc
    return {normalize_name(name) for name in parse_sources(text).creators}


def keep_reasons(
    creator_ids: Sequence[int],
    *,
    source_names: set[str],
    forgotten_research: Iterable[int] = (),
) -> dict[int, str]:
    """The first keep reason of each of these creators; absent from the mapping means unused.

    One query per rule over the given ids, so the whole pass is a handful of statements whatever the
    catalogue's size.

    `forgotten_research` names creators whose research the prospect pass has removed in this same
    run, or would remove in this dry run: their watchlist entry, their watchlist-derived target role
    and their page links are on their way out, so those three rules are not read from rows that no
    longer decide anything. Without it a dry run and a real run would disagree about the same night.
    """
    ids = list(creator_ids)
    reasons: dict[int, str] = {}
    if not ids:
        return reasons
    known = set(ids)
    forgotten = set(forgotten_research)

    def mark(code: str, matching_ids: Iterable[int]) -> None:
        for pk in matching_ids:
            reasons.setdefault(pk, code)

    # Read in the order the report should explain them: the reasons that are also database guards
    # first, so a name kept by the database is never reported as kept by something weaker.
    mark(
        KEEP_SUPPRESSION,
        CreatorSuppression.objects.filter(creator_id__in=ids).values_list("creator_id", flat=True),
    )
    mark(
        KEEP_CUSTOMER_ORIGINAL,
        Reference.objects.filter(creator_id__in=ids).values_list("creator_id", flat=True),
    )
    mark(
        KEEP_CUSTOMER_ORIGINAL,
        Reference.creators.through.objects.filter(creator_id__in=ids).values_list(
            "creator_id", flat=True
        ),
    )
    mark(
        KEEP_TARGET,
        (
            target.creator.pk
            for target in targets()
            if target.creator.pk in known
            and not (
                target.creator.pk in forgotten
                and target.kind in (TargetKind.WATCHLIST, TargetKind.PROSPECT)
            )
        ),
    )
    mark(
        KEEP_WATCHLIST,
        (
            pk
            for pk in WatchlistEntry.objects.filter(creator_id__in=ids).values_list(
                "creator_id", flat=True
            )
            if pk not in forgotten
        ),
    )
    mark(
        KEEP_ACTIVE_PAGE,
        (
            pk
            for pk in Page.creators.through.objects.filter(
                creator_id__in=ids, page__active=True
            ).values_list("creator_id", flat=True)
            if pk not in forgotten
        ),
    )
    mark(
        KEEP_SAFETY_RECORD,
        Page.objects.filter(safety_release_creator_id__in=ids).values_list(
            "safety_release_creator_id", flat=True
        ),
    )
    mark(
        KEEP_SAFETY_RECORD,
        SafetyRuleDecision.objects.filter(creator_id__in=ids).values_list("creator_id", flat=True),
    )
    if source_names:
        mark(
            KEEP_SOURCE_LIST,
            (
                pk
                for pk, name in Creator.objects.filter(pk__in=ids).values_list("pk", "name")
                if normalize_name(name) in source_names
            ),
        )
    return reasons


@dataclass
class ProspectRetentionReport:
    dry_run: bool
    now: datetime
    retain_days: int
    forget_before: datetime
    #: Prospect watchlist entries with a clock running at all, any age.
    prospects: int
    #: Creator ids whose research is (or would be) forgotten, and what goes with each.
    forgotten_ids: list[int] = field(default_factory=list)
    #: Creator ids past the period but kept, with the reason code.
    kept: dict[int, str] = field(default_factory=dict)
    sample_checks: int = 0
    page_links: int = 0
    breadcrumbs: int = 0
    #: Due dates per creator id, so a dry run shows what is coming, not only what is due.
    due_on: dict[int, str] = field(default_factory=dict)

    @property
    def due(self) -> int:
        return len(self.forgotten_ids)


@dataclass
class NameRetentionReport:
    dry_run: bool
    now: datetime
    retain_days: int
    delete_before: datetime
    #: Every name in the catalogue, and how many of them no rule keeps.
    names: int
    unused: int
    #: Clocks this run would start / clear.
    marked: list[int] = field(default_factory=list)
    cleared: list[int] = field(default_factory=list)
    #: Names due for deletion (clock past the period, or a prospect forgotten in this run).
    due_ids: list[int] = field(default_factory=list)
    deleted_ids: list[int] = field(default_factory=list)
    #: Kept with a reason code: `protected` after a real run, the keep rules for the rest.
    kept: dict[int, str] = field(default_factory=dict)
    #: How many rows of each name-bearing table the cascade takes with the deleted names.
    cascade: dict[str, int] = field(default_factory=dict)

    @property
    def keep_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for code in self.kept.values():
            counts[code] = counts.get(code, 0) + 1
        return counts


def _cascade_counts(creator_ids: Sequence[int]) -> dict[str, int]:
    """What deleting these names takes with them. Counts only, for the report."""
    ids = list(creator_ids)
    if not ids:
        return {}
    return {
        "page_links": Page.creators.through.objects.filter(creator_id__in=ids).count(),
        "breadcrumbs": SiteCreatorBreadcrumb.objects.filter(creator_id__in=ids).count(),
        "prospect_checks": ProspectSampleCheck.objects.filter(creator_id__in=ids).count(),
        "merge_log": CreatorMergeLog.objects.filter(target_id__in=ids).count(),
    }


def apply_prospect_retention(
    *,
    now: datetime | None = None,
    dry_run: bool = True,
    retain_days: int = PROSPECT_RESEARCH_RETENTION_DAYS,
    sources: str | Path | None = None,
) -> ProspectRetentionReport:
    """Forget prospect research older than `retain_days`. Idempotent; `dry_run` only counts."""
    now = now or timezone.now()
    forget_before = now - timedelta(days=retain_days)
    source_names = source_list_names(sources)
    entries = WatchlistEntry.objects.filter(purpose=WatchlistEntry.Purpose.PROSPECT).order_by(
        "creator_id"
    )
    report = ProspectRetentionReport(
        dry_run=dry_run,
        now=now,
        retain_days=retain_days,
        forget_before=forget_before,
        prospects=entries.count(),
        due_on={
            pk: (added_at + timedelta(days=retain_days)).date().isoformat()
            for pk, added_at in entries.values_list("creator_id", "added_at")
        },
    )
    due_ids = list(entries.filter(added_at__lte=forget_before).values_list("creator_id", flat=True))
    if not due_ids:
        return report
    # A prospect who has become a rights holder or co-performer of an owned original is a customer,
    # not research: their watchlist entry is not this pass's to delete.
    reasons = keep_reasons(due_ids, source_names=source_names, forgotten_research=due_ids)
    # Only these two mean "this is not research any more": a suppression is a request we must keep
    # honouring, and a reference is customer data whose retention is ADR 0045. A prospect's own
    # watchlist entry, its target role and its page links are exactly what this pass removes, and
    # `source_list` keeps the *name* (pass 3), never the research.
    protected = {KEEP_SUPPRESSION, KEEP_CUSTOMER_ORIGINAL}
    for pk in due_ids:
        code = reasons.get(pk)
        if code in protected:
            report.kept[pk] = code
        else:
            report.forgotten_ids.append(pk)
    ids = report.forgotten_ids
    report.sample_checks = ProspectSampleCheck.objects.filter(creator_id__in=ids).count()
    report.page_links = Page.creators.through.objects.filter(creator_id__in=ids).count()
    report.breadcrumbs = SiteCreatorBreadcrumb.objects.filter(creator_id__in=ids).count()
    if dry_run or not ids:
        return report
    with transaction.atomic():
        # ADR 0057: a restore brings the verdicts, the links, the breadcrumbs and the watchlist
        # entry back. The creator id is enough to forget the same research again — the restored
        # database still holds the name, which is exactly why the journal need not.
        deletion_journal.record_many(
            "prospect_research_forgotten", ({"creator": pk} for pk in ids), now=now
        )
        ProspectSampleCheck.objects.filter(creator_id__in=ids).delete()
        Page.creators.through.objects.filter(creator_id__in=ids).delete()
        SiteCreatorBreadcrumb.objects.filter(creator_id__in=ids).delete()
        WatchlistEntry.objects.filter(creator_id__in=ids).delete()
    # Ids and counts only: the name is what this pass exists to forget.
    logger.warning(
        "PROSPECT RESEARCH RETENTION forgot %s prospect(s) added before %s "
        "(%s verdict(s), %s page link(s), %s breadcrumb(s)); %s kept",
        len(ids),
        forget_before.date().isoformat(),
        report.sample_checks,
        report.page_links,
        report.breadcrumbs,
        len(report.kept),
    )
    return report


def apply_name_retention(
    *,
    now: datetime | None = None,
    dry_run: bool = True,
    retain_days: int = CREATOR_NAME_RETENTION_DAYS,
    sources: str | Path | None = None,
    delete_now: Iterable[int] = (),
) -> NameRetentionReport:
    """Start, clear and act on the retention clock of every catalogue name.

    `delete_now` are creators whose research the prospect pass forgot in this same run (or would
    forget in this dry run): they are due immediately, with no clock, because the research was the
    only reason we held the name, and the rules that pass removed are not read for them.
    Idempotent; `dry_run` reports and writes nothing.
    """
    now = now or timezone.now()
    delete_before = now - timedelta(days=retain_days)
    source_names = source_list_names(sources)
    all_ids = list(Creator.objects.order_by("pk").values_list("pk", flat=True))
    forgotten = {pk for pk in delete_now}
    reasons = keep_reasons(all_ids, source_names=source_names, forgotten_research=forgotten)
    clocks = dict(CreatorRetentionClock.objects.values_list("creator_id", "unused_since"))
    immediate = {pk for pk in forgotten if pk in set(all_ids) and pk not in reasons}
    unused = [pk for pk in all_ids if pk not in reasons]
    report = NameRetentionReport(
        dry_run=dry_run,
        now=now,
        retain_days=retain_days,
        delete_before=delete_before,
        names=len(all_ids),
        unused=len(unused),
        marked=[pk for pk in unused if pk not in clocks],
        cleared=[pk for pk in clocks if pk in reasons],
        kept={pk: code for pk, code in reasons.items()},
    )
    expired = {pk for pk in unused if clocks.get(pk) is not None and clocks[pk] <= delete_before}
    report.due_ids = sorted(immediate | expired)
    report.cascade = _cascade_counts(report.due_ids)
    if dry_run:
        return report
    if report.cleared:
        CreatorRetentionClock.objects.filter(creator_id__in=report.cleared).delete()
    if report.marked:
        CreatorRetentionClock.objects.bulk_create(
            [
                CreatorRetentionClock(creator_id=pk, unused_since=now)
                for pk in report.marked
                if pk not in report.due_ids
            ]
        )
    for pk in report.due_ids:
        try:
            with transaction.atomic():
                # ADR 0057, inside the deletion's own transaction: a row the database protects
                # (a suppression marker, a reference) raises and leaves no journal line, because
                # the line is written on commit.
                deletion_journal.record("creator_name_deleted", creator=pk, now=now)
                Creator.objects.filter(pk=pk).delete()
            report.deleted_ids.append(pk)
        except ProtectedError:
            # A suppression marker or a reference still names it: the database is the guard, and a
            # refusal is reported as kept rather than failing the run (ADR 0045's pattern).
            report.kept[pk] = KEEP_PROTECTED
    if report.deleted_ids:
        logger.warning(
            "NAME RETENTION deleted %s catalogue name(s) unused since before %s "
            "(%s page link(s), %s breadcrumb(s) cascaded); %s kept",
            len(report.deleted_ids),
            delete_before.date().isoformat(),
            report.cascade.get("page_links", 0),
            report.cascade.get("breadcrumbs", 0),
            len(report.kept),
        )
    return report
