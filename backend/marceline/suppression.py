"""Applying and lifting an index suppression (docs/suppression.md).

The route for a performer who is **not** our customer and wants us to stop indexing them. The
marker itself is `catalog.CreatorSuppression`; what keeps it in force lives in `catalog` and
`index` (`Creator.objects.usable()`, the breadcrumb writers, the database trigger on the
page-creator link table). This module is the other half: what a suppression does to what we
already hold, and what it refuses to do.

**What suppression deletes** — everything that says *this person is on these pages*:

- `Page.creators` links for the name (the association itself);
- `index.SiteCreatorBreadcrumb` rows for the name (a site x creator count is that statement in
  aggregate);
- name-bearing parts of the stored extraction: entries of `Page.tags` and of `Page.raw_extraction`
  (the performer credits, and adapter fields such as a handle or a profile slug) that name them —
  **on safety-clear pages only**, see "Safety-flagged pages" below;
- `index.ProspectSampleCheck` verdicts and the `catalog.WatchlistEntry`, so the name can be
  neither researched nor hunted;
- the discovery trail: `crawler.CandidateCreator` links, `SourceCandidate.evidence["queries"]`
  entries naming them, and the **query text** of `crawler.SearchCall` rows that named them. Since
  ADR 0048 those rows are scrubbed rather than deleted: the row is also ADR 0013's spend ledger, so
  deleting it removed the name *and* silently moved the accounting. The name goes exactly as before
  (`crawler.retention.scrub_search_calls`, the same function the scheduled pass uses).

**What suppression keeps, on purpose** — a page credits several people, and the rest of the record
is not about the requester:

- the page itself, its URL, its site, its timestamps, its availability and its safety record:
  deleting a page would destroy the other performers' evidence and our own safety case file;
- `Page.title`: it is the site's own words, usually names the work and often other performers, and
  the next crawl of that page writes it again verbatim. Masking it would corrupt a record we did
  not author without making the page any less findable. Removing it is an operator decision
  (docs/suppression.md, "What the operator must decide"), not something this command guesses at;
- media rows and PDQ hashes: a hash is a fingerprint of a frame, not a name, and it is what lets
  another performer's copy be found on that page;
- `matching.Reference` casts of customer originals, which a customer gave us under a contract and
  may need for a legal claim. The command counts them and says so; it never edits them. An
  original whose **rights holder** would be suppressed makes the command refuse outright.

**Safety-flagged pages** (operator decision, confirmed 2026-09-17: a flagged page is off-limits
whoever is asking and whether or not the operator is present). Suppression therefore **never
rewrites the stored text of a page that is not safety-clear**: on such a page that text *is* the
safety record — it is what `safety_scan` re-judges, what `crawler.safety` decided on, and what a
hotline report is built from (docs/safety.md), so editing it could change a verdict or a case.
`_page_text_changes` only ever yields clear pages, and the count of flagged pages naming the
person is reported instead (`SuppressionPlan.flagged_pages`), so the operator decides it
rather than this command.

Links and breadcrumbs *are* removed on every page, flagged or not: deleting an association
changes no safety field, no matched term and no incident row — and `store_parse_result` stores no
creator link for a blocked page in the first place. Nothing here adds a link, activates a page,
queues a crawl, changes a safety status or releases media, on a flagged page or any other, and a
lift adds nothing back.
"""

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import Organisation
from catalog.models import Creator, CreatorSuppression, WatchlistEntry
from catalog.suppression import suppression_of
from core import deletion_journal
from crawler.models import CandidateCreator, SearchCall, SourceCandidate
from crawler.retention import scrub_search_calls
from crawler.sites.common import creator_names, name_keys, names_person
from index.models import Page, ProspectSampleCheck, SiteCreatorBreadcrumb
from marceline.app.ownership import owned_references

logger = logging.getLogger(__name__)

#: Pages are read in chunks: this runs by hand, rarely, and reads every page row once rather
#: than guessing which spellings a site might have used.
_PAGE_CHUNK = 2000
_UPDATE_BATCH = 500


class SuppressionRefused(Exception):
    """A suppression a person must decide instead of this command (a rights-holder conflict)."""


# --- Redaction (pure; the name matcher itself lives in crawler.sites.common) -------------------

_DROP = object()


def _redacted(value: Any, keys: Sequence[str], path: str, dropped: list[str]) -> Any:
    if isinstance(value, str):
        if names_person(value, keys):
            dropped.append(path)
            return _DROP
        return value
    if isinstance(value, Mapping):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            child = _redacted(item, keys, f"{path}.{key}" if path else str(key), dropped)
            if child is not _DROP:
                kept[str(key)] = child
        return kept
    if isinstance(value, list):
        items = [_redacted(item, keys, path, dropped) for item in value]
        return [item for item in items if item is not _DROP]
    return value


def redact(value: Any, keys: Sequence[str]) -> tuple[Any, list[str]]:
    """`value` without the parts that name this person, and the paths that were dropped. Pure.

    A string that names them is removed: from a list its entry goes, from a mapping its whole key
    goes (a `{"handle": "janedoe"}` field is about nobody else). Numbers, booleans and everything
    that names somebody else are untouched, so a page crediting four performers keeps three.
    """
    dropped: list[str] = []
    result = _redacted(value, keys, "", dropped)
    return ({} if result is _DROP else result), dropped


def redact_page(tags: Any, raw_extraction: Any, keys: Sequence[str]) -> tuple[Any, Any, list[str]]:
    """One page's `tags` and `raw_extraction` without the person, and the fields that changed."""
    new_tags, tag_paths = redact(tags, keys)
    new_raw, raw_paths = redact(raw_extraction, keys)
    fields = ["tags" for _ in tag_paths]
    fields += [f"raw_extraction.{path}" if path else "raw_extraction" for path in raw_paths]
    return new_tags, new_raw, fields


# --- The plan ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRedaction:
    """One page whose stored extraction names the person, and what it would look like after."""

    page_id: int
    tags: Any
    raw_extraction: Any
    fields: list[str]


@dataclass(frozen=True)
class SuppressionPlan:
    """Exactly what applying a suppression would change. Read-only; `--dry-run` prints it."""

    creator: Creator
    names: list[str]
    existing: CreatorSuppression | None
    page_links: int
    link_sites: int
    breadcrumbs: int
    redactions: list[PageRedaction]
    #: Pages that name the person but are **not** safety-clear: their stored text is the safety
    #: record, so suppression leaves it exactly as it is and only counts them (see the module
    #: docstring, "Safety-flagged pages"). A count, never an id or a URL.
    flagged_pages: int
    watchlist: WatchlistEntry | None
    sample_checks: int
    candidate_links: int
    candidate_queries: int
    #: Search calls whose query text named them: **scrubbed**, never deleted (ADR 0048) — the row is
    #: also the search spend ledger of ADR 0013.
    search_calls: int
    owned_as_rights_holder: int
    owned_as_co_performer: int

    @property
    def redacted_pages(self) -> int:
        return len(self.redactions)

    @property
    def fields(self) -> dict[str, int]:
        """Pages affected per stored field, e.g. `{"raw_extraction.performers": 39, "tags": 12}`."""
        counted = Counter(
            path for redaction in self.redactions for path in dict.fromkeys(redaction.fields)
        )
        return dict(sorted(counted.items()))

    @property
    def changes(self) -> int:
        return (
            self.page_links
            + self.breadcrumbs
            + self.redacted_pages
            + self.sample_checks
            + self.candidate_links
            + self.candidate_queries
            + self.search_calls
            + (1 if self.watchlist is not None else 0)
        )


def _page_text_changes(keys: Sequence[str]) -> tuple[list[PageRedaction], int]:
    """(redactions on safety-clear pages, how many flagged pages name the person and are left).

    The single place that decides which pages' text suppression may rewrite. A page that is not
    safety-clear is counted and skipped: its stored text is the safety record `safety_scan`
    re-judges and a hotline report is built from, and a flagged page is off-limits whoever is
    asking (docs/safety.md, and the module docstring above).
    """
    redactions: list[PageRedaction] = []
    flagged = 0
    rows = Page.objects.values("id", "safety_status", "tags", "raw_extraction")
    for row in rows.iterator(chunk_size=_PAGE_CHUNK):
        tags, raw, fields = redact_page(row["tags"], row["raw_extraction"], keys)
        if not fields:
            continue
        if row["safety_status"] != Page.SafetyStatus.CLEAR:
            flagged += 1
            continue
        redactions.append(PageRedaction(row["id"], tags, raw, fields))
    return redactions, flagged


def _candidate_evidence(keys: Sequence[str]) -> list[tuple[SourceCandidate, dict[str, Any], int]]:
    """Source candidates whose recorded search queries named the person, with the cleaned
    evidence and how many query samples would go."""
    changed: list[tuple[SourceCandidate, dict[str, Any], int]] = []
    for candidate in SourceCandidate.objects.exclude(evidence={}).order_by("pk"):
        evidence = candidate.evidence
        if not isinstance(evidence, Mapping):
            continue
        samples = evidence.get("queries")
        if not isinstance(samples, list):
            continue
        kept = [
            sample
            for sample in samples
            if not (
                isinstance(sample, Mapping) and names_person(str(sample.get("query", "")), keys)
            )
        ]
        if len(kept) != len(samples):
            changed.append((candidate, {**evidence, "queries": kept}, len(samples) - len(kept)))
    return changed


def _search_call_ids(keys: Sequence[str]) -> list[int]:
    return [
        call.pk
        for call in SearchCall.objects.only("id", "query").order_by("pk")
        if names_person(call.query, keys)
    ]


def _owned_counts(creator: Creator) -> tuple[int, int]:
    """(as rights holder, as co-performer) organisation-owned originals crediting this name."""
    as_rights_holder = 0
    as_co_performer = 0
    for organisation in Organisation.objects.order_by("pk"):
        owned = owned_references(organisation)
        as_rights_holder += owned.filter(creator_id=creator.pk).count()
        as_co_performer += (
            owned.filter(creators=creator.pk).exclude(creator_id=creator.pk).distinct().count()
        )
    return as_rights_holder, as_co_performer


def plan_suppression(creator: Creator) -> SuppressionPlan:
    """Read-only: everything applying a suppression for this creator would change."""
    names = creator_names(creator)
    keys = name_keys(names)
    evidence = _candidate_evidence(keys)
    redactions, flagged_pages = _page_text_changes(keys)
    as_rights_holder, as_co_performer = _owned_counts(creator)
    return SuppressionPlan(
        creator=creator,
        names=names,
        existing=suppression_of(creator),
        page_links=Page.creators.through.objects.filter(creator_id=creator.pk).count(),
        link_sites=Page.objects.filter(creators=creator).values("site_id").distinct().count(),
        breadcrumbs=SiteCreatorBreadcrumb.objects.filter(creator_id=creator.pk).count(),
        redactions=redactions,
        flagged_pages=flagged_pages,
        watchlist=WatchlistEntry.objects.filter(creator_id=creator.pk).first(),
        sample_checks=ProspectSampleCheck.objects.filter(creator_id=creator.pk).count(),
        candidate_links=CandidateCreator.objects.filter(creator_id=creator.pk).count(),
        candidate_queries=sum(dropped for _, _, dropped in evidence),
        search_calls=len(_search_call_ids(keys)),
        owned_as_rights_holder=as_rights_holder,
        owned_as_co_performer=as_co_performer,
    )


# --- Applying and lifting ---------------------------------------------------------------------


def answer_due(requested_on: date) -> date:
    """One month from the request (Art. 12(3) GDPR), clamped to the end of a shorter month."""
    year = requested_on.year + (requested_on.month // 12)
    month = requested_on.month % 12 + 1
    day = requested_on.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def _refusal(plan: SuppressionPlan) -> str:
    if plan.owned_as_rights_holder:
        return (
            f"{plan.creator.name!r} is the rights holder of {plan.owned_as_rights_holder} "
            "original(s) an organisation owns. Suppressing a customer's own rights holder would "
            "delete the evidence of their own case: this is a conflict for the operator "
            "to decide, not for this command (docs/suppression.md)."
        )
    return ""


@transaction.atomic
def apply_suppression(
    creator: Creator,
    *,
    basis: str = CreatorSuppression.Basis.OBJECTION,
    requested_on: date | None = None,
    identity_check: str = CreatorSuppression.IdentityCheck.NOT_VERIFIED,
    identity_note: str = "",
    note: str = "",
    decided_by: str = "",
    now: datetime | None = None,
) -> tuple[SuppressionPlan, CreatorSuppression]:
    """Suppress this name and drop what we hold about them. Idempotent; logs what it removed.

    Re-running it changes nothing new: the marker keeps its dates, and everything it deletes is
    already gone. Running it after a lift suppresses the name again.
    """
    now = now or timezone.now()
    requested_on = requested_on or now.date()
    plan = plan_suppression(creator)
    if refusal := _refusal(plan):
        raise SuppressionRefused(refusal)

    suppression, created = CreatorSuppression.objects.get_or_create(
        creator=creator,
        defaults={
            "basis": basis,
            "requested_on": requested_on,
            "answer_due_on": answer_due(requested_on),
            "identity_check": identity_check,
            "identity_note": identity_note,
            "note": note,
            "decided_by": decided_by,
        },
    )
    if not created and suppression.lifted_at is not None:
        # Suppressed again after a lift: the new request replaces the old record's dates.
        suppression.basis = basis
        suppression.requested_on = requested_on
        suppression.answer_due_on = answer_due(requested_on)
        suppression.identity_check = identity_check
        suppression.identity_note = identity_note
        suppression.note = note
        suppression.decided_by = decided_by
        suppression.lifted_at = None
        suppression.lift_reason = ""

    # Links and breadcrumbs go on every page, flagged or not: removing an association touches no
    # safety field, matched term or incident row (and a blocked page never had a link).
    Page.creators.through.objects.filter(creator_id=creator.pk).delete()
    SiteCreatorBreadcrumb.objects.filter(creator_id=creator.pk).delete()
    ProspectSampleCheck.objects.filter(creator_id=creator.pk).delete()
    WatchlistEntry.objects.filter(creator_id=creator.pk).delete()

    candidate_ids = list(
        CandidateCreator.objects.filter(creator_id=creator.pk).values_list(
            "candidate_id", flat=True
        )
    )
    CandidateCreator.objects.filter(creator_id=creator.pk).delete()
    for candidate in SourceCandidate.objects.filter(pk__in=candidate_ids):
        candidate.distinct_creators = CandidateCreator.objects.filter(
            candidate_id=candidate.pk
        ).count()
        candidate.save(update_fields=("distinct_creators",))
    keys = name_keys(plan.names)
    for candidate, evidence, _ in _candidate_evidence(keys):
        candidate.evidence = evidence
        candidate.save(update_fields=("evidence",))
    # Scrubbed, not deleted (ADR 0048): the name goes, the row's cost stays in the ledger.
    scrub_search_calls(_search_call_ids(keys), now=now)

    # Text is rewritten on safety-clear pages only (`_page_text_changes`), and only these two
    # JSON fields: never a safety status, a matched term, a score, `active` or a link.
    Page.objects.bulk_update(
        [
            Page(pk=item.page_id, tags=item.tags, raw_extraction=item.raw_extraction)
            for item in plan.redactions
        ],
        ("tags", "raw_extraction"),
        batch_size=_UPDATE_BATCH,
    )

    # ADR 0057, DPIA R24: a restore undoes every deletion above. A snapshot taken after the
    # request brings the links and breadcrumbs back (the marker comes with it, so no *new* link is
    # written, but the old ones stand again); one taken before it loses the marker too and the
    # suppression is simply gone. The journal is outside the dump, so `reapply_deletions` can
    # suppress the name again. Nothing but the creator id, the basis, the request date and the
    # identity-check code goes in — never the name, never the note, never who decided.
    was_in_force = plan.existing is not None and plan.existing.lifted_at is None
    if not was_in_force or plan.changes:
        deletion_journal.record(
            "creator_suppression",
            creator=creator.pk,
            basis=str(suppression.basis),
            requested_on=suppression.requested_on,
            identity=str(suppression.identity_check),
            now=now,
        )

    suppression.applied_at = now
    suppression.decided_by = decided_by or suppression.decided_by
    suppression.removed_page_links = F("removed_page_links") + plan.page_links
    suppression.removed_breadcrumbs = F("removed_breadcrumbs") + plan.breadcrumbs
    suppression.redacted_pages = F("redacted_pages") + plan.redacted_pages
    suppression.save()
    suppression.refresh_from_db()

    # The creator id, never the name: the row above is the record, the log is for operations.
    logger.info(
        "index suppression applied: creator=%s links=%d breadcrumbs=%d pages_redacted=%d "
        "flagged_pages_left=%d sample_checks=%d candidate_links=%d candidate_queries=%d "
        "search_calls=%d watchlist=%s",
        creator.pk,
        plan.page_links,
        plan.breadcrumbs,
        plan.redacted_pages,
        plan.flagged_pages,
        plan.sample_checks,
        plan.candidate_links,
        plan.candidate_queries,
        plan.search_calls,
        plan.watchlist is not None,
    )
    return plan, suppression


@transaction.atomic
def lift_suppression(
    creator: Creator, *, reason: str, decided_by: str = "", now: datetime | None = None
) -> CreatorSuppression:
    """Lift a suppression: the name may be hunted, linked and searched for again from now on.

    Nothing is restored. Links and breadcrumbs come back only as the next crawl of a page finds
    the name again, and the stored extraction this suppression redacted is rewritten by that same
    crawl — we do not keep a copy to put back. The row stays, with its reason, as the record.
    """
    reason = reason.strip()
    if not reason:
        raise SuppressionRefused("say why the suppression is lifted (--reason)")
    suppression = suppression_of(creator)
    if suppression is None:
        raise SuppressionRefused(f"{creator.name!r} has no suppression to lift")
    if suppression.lifted_at is not None:
        return suppression
    suppression.lifted_at = now or timezone.now()
    suppression.lift_reason = reason
    if decided_by:
        suppression.decided_by = decided_by
    suppression.save(update_fields=("lifted_at", "lift_reason", "decided_by", "updated_at"))
    # Journalled for completeness, not for replay (ADR 0057): the reason is free text, which never
    # enters the journal, and a restore that loses a lift leaves the name suppressed — the safe
    # direction. `reapply_deletions` lists these for a human instead of lifting anything itself.
    deletion_journal.record("creator_suppression_lifted", creator=creator.pk, now=now)
    logger.info("index suppression lifted: creator=%s", creator.pk)
    return suppression
