"""Replaying the deletion journal against a restored database (ADR 0057, DPIA R24, issue #137).

A `pg_restore` puts the database back as it was on the night of the dump. Everything deleted since
is therefore **back**: a suppressed performer's page links and breadcrumbs (ADR 0038), a deactivated
page's title, tags and performer credits (ADR 0029), a forgotten prospect's research (ADR 0048), a
former customer's originals, rights record and notice drafts (ADR 0045), a settled safety case, an
expired page. And one thing is *missing* rather than resurrected: every do-not-contact record given
after the dump (ADR 0047), which is the worse direction — we would write to someone who said no.

`core.deletion_journal` holds one line per such action, outside the dump. This module walks those
lines oldest first and puts the database back into the state the deletions left it in, using **the
same functions that performed the original deletion** wherever one exists, so there is no second
implementation of what a suppression removes.

**Why a replay can be trusted to delete.** The restored database is a strictly *older* state of the
same database. Nothing in it can have become customer data, a crawl target or a live case after the
journal line was written, because the journal line is newer than every row in it. So "delete this
again" cannot destroy something that has since acquired a reason to be kept. Where that argument is
not quite enough — a safety case that is `open` again in the restored state, a page a still-open
case is attached to — the entry is **listed for a human** instead.

**Idempotent, and it says which of the two it did.** Every kind first asks whether the deletion is
already in effect, so a second run reports `already` and writes nothing. A dry run is the default
and reports exactly what a real run would do.

**Kinds this cannot re-apply from the journal alone** (each reported with its key, never silently):

- `creator_suppression_lifted` — a lift needs a reason, and free text never enters the journal. A
  restore that loses a lift leaves the name suppressed, which is the safe side, so the operator
  lifts it again by hand with `suppress_creator --lift` if it was genuinely withdrawn.
- `creator_suppression` whose `catalog.Creator` row is not in the restored database (the name was
  first recorded after the dump): there is nothing to attach the marker to, and the journal holds
  no name to create one from — by design.
- `organisation_data_deleted` / `original_data_deleted` with no `OrganisationEnding` /
  `OriginalRemoval` mark in the restored database **while the data is still there**. ADR 0045 is
  explicit that both scopes run off a date **a human set**; re-inventing that mark here would be
  inferring it. When the mark is gone *and* so is the data (the organisation row cascaded away with
  a completed deletion), that is the deletion in effect, not a gap — `already`, so the weekly
  restore check does not complain for ever about a pilot that ended cleanly.
- `page_deleted` whose page is attached to an `open` or `reported` safety incident in the restored
  state, and `safety_incident_deleted` whose case is `open` again: a human settles a case, a replay
  does not.

Run it through `manage.py reapply_deletions` — always before the restored database serves anything
(docs/runbooks/backup-restore.md § C, docs/runbooks/restore-drill.md).

**Why it is not inside `marceline/deletion/`.** That package deliberately imports nothing from
`catalog` (ADR 0047, `test_the_deletion_pass_imports_nothing_from_catalog`): the do-not-contact
record, the creators and the sites are not a customer's data and a customer-data pass must not be
able to reach them. A replay spans every deletion path in the product, `catalog` included, so it
lives beside that package rather than in it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from django.db import connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from accounts.models import Organisation
from catalog.models import Creator, DoNotContactRecord, WatchlistEntry
from catalog.suppression import is_suppressed, suppression_of
from core import deletion_journal
from core.deletion_journal import Entry, Journal
from crawler.models import SearchCall, SourceCandidate
from crawler.retention import scrub_search_calls, scrubbed_evidence
from index.models import Page, ProspectSampleCheck, SafetyIncident, SiteCreatorBreadcrumb
from index.retention import (
    OPEN_SAFETY_STATUSES,
    holds_scrubbable_text,
    scrub_deactivated_pages,
    stays_scrubbed,
)
from marceline.app.ownership import owned_references
from marceline.deletion.models import OrganisationEnding, OriginalRemoval
from marceline.deletion.retention import delete_organisation, delete_original
from marceline.suppression import SuppressionRefused, apply_suppression, plan_suppression
from matching.models import LeakCase, Reference

logger = logging.getLogger(__name__)

#: What one entry's replay concluded.
ALREADY = "already"
REAPPLIED = "reapplied"
FAILED = "failed"
HUMAN = "human"

#: The order kinds are replayed in. Deliberately not journal order across kinds: the text scrubs
#: run before the page deletions (so a page that is deleted later is not scrubbed pointlessly), the
#: prospect pass before the name deletions it feeds, and the customer scopes last because they are
#: the only ones that touch billing-adjacent rows. Within one kind the order is the journal's.
REPLAY_ORDER: tuple[str, ...] = (
    "creator_suppression",
    "prospect_research_forgotten",
    "creator_name_deleted",
    "do_not_contact_recorded",
    "search_query_scrubbed",
    "candidate_queries_scrubbed",
    "page_text_scrub",
    "page_deleted",
    "safety_incident_deleted",
    "case_file_reset",
    "original_data_deleted",
    "organisation_data_deleted",
    "creator_suppression_lifted",
)

#: The note a replayed do-not-contact record carries. The original note is free text and is in no
#: backup outside the database, so the row says where it came from rather than pretending.
REPLAY_NOTE = "Re-inserted from the deletion journal after a database restore (ADR 0057)."
#: `recorded_by` of a replayed row: the replay, never a staff name read out of a file.
REPLAY_ACTOR = "deletion-journal"
#: The note a replayed suppression marker carries, for the same reason.
REPLAY_SUPPRESSION_NOTE = (
    "Re-applied from the deletion journal after a database restore (ADR 0057). The original note, "
    "identity note and decided_by are in no backup outside the database."
)


@dataclass
class KindOutcome:
    """What the replay did with every entry of one kind."""

    kind: str
    entries: int = 0
    already: int = 0
    reapplied: int = 0
    #: `(key, reason)` — never the deleted content, and no free text from the database.
    failed: list[tuple[str, str]] = field(default_factory=list)
    needs_human: list[tuple[str, str]] = field(default_factory=list)

    def record(self, verdict: str, key: str, reason: str) -> None:
        if verdict == ALREADY:
            self.already += 1
        elif verdict == REAPPLIED:
            self.reapplied += 1
        elif verdict == FAILED:
            self.failed.append((key, reason))
        else:
            self.needs_human.append((key, reason))


@dataclass
class ReplayReport:
    dry_run: bool
    now: datetime
    database: str
    journal_dir: str
    since: datetime | None = None
    #: `file:line reason` for lines that could not be parsed. A journal nobody can read is itself
    #: the failure this feature exists against, so it is reported, never skipped.
    unreadable: list[str] = field(default_factory=list)
    outcomes: list[KindOutcome] = field(default_factory=list)

    @property
    def entries(self) -> int:
        return sum(outcome.entries for outcome in self.outcomes)

    @property
    def already(self) -> int:
        return sum(outcome.already for outcome in self.outcomes)

    @property
    def reapplied(self) -> int:
        return sum(outcome.reapplied for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(len(outcome.failed) for outcome in self.outcomes)

    @property
    def needs_human(self) -> int:
        return sum(len(outcome.needs_human) for outcome in self.outcomes)

    @property
    def summary(self) -> str:
        """One machine-readable line; `infra/backup/pg-restore-check.sh` reads it."""
        return (
            f"reapply-deletions entries={self.entries} already={self.already} "
            f"reapplied={self.reapplied} failed={self.failed} "
            f"needs_human={self.needs_human} unreadable={len(self.unreadable)}"
        )


# --- One handler per kind ---------------------------------------------------------------------
# Each returns (verdict, reason). A handler may write only when `dry_run` is False, and every one
# of them asks about the current state first so a second run is a no-op.


def _creator_suppression(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    creator = Creator.objects.filter(pk=entry.get("creator")).first()
    if creator is None:
        return FAILED, "no creator row in the restored database, and the journal holds no name"
    if dry_run:
        plan = plan_suppression(creator)
        if is_suppressed(creator) and not plan.changes:
            return ALREADY, ""
        return REAPPLIED, f"{plan.changes} row(s) to remove again"
    in_force = is_suppressed(creator)
    try:
        plan, _ = apply_suppression(
            creator,
            basis=str(entry.get("basis")),
            requested_on=datetime.fromisoformat(str(entry.get("requested_on"))).date(),
            identity_check=str(entry.get("identity")),
            note=REPLAY_SUPPRESSION_NOTE,
            decided_by=REPLAY_ACTOR,
            now=now,
        )
    except SuppressionRefused:
        # The message names the creator, so it is not repeated here (CLAUDE.md: identifiers, never
        # content). The only refusal `apply_suppression` raises is the rights-holder conflict.
        return FAILED, "refused: the name is the rights holder of a customer original"
    if in_force and not plan.changes:
        return ALREADY, ""
    return REAPPLIED, f"{plan.changes} row(s) removed again"


def _creator_suppression_lifted(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    suppression = suppression_of(entry.get("creator"))
    if suppression is None:
        return ALREADY, "no marker in the restored database, so nothing is suppressed"
    if suppression.lifted_at is not None:
        return ALREADY, ""
    return HUMAN, "lift it by hand with `suppress_creator --lift --reason …` if it still holds"


def _prospect_research_forgotten(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    creator_id = entry.get("creator")
    if not Creator.objects.filter(pk=creator_id).exists():
        return ALREADY, "the name itself is gone"
    checks = ProspectSampleCheck.objects.filter(creator_id=creator_id)
    links = Page.creators.through.objects.filter(creator_id=creator_id)
    crumbs = SiteCreatorBreadcrumb.objects.filter(creator_id=creator_id)
    entries = WatchlistEntry.objects.filter(creator_id=creator_id)
    rows = checks.count() + links.count() + crumbs.count() + entries.count()
    if not rows:
        return ALREADY, ""
    if dry_run:
        return REAPPLIED, f"{rows} row(s) to forget again"
    with transaction.atomic():
        checks.delete()
        links.delete()
        crumbs.delete()
        entries.delete()
    return REAPPLIED, f"{rows} row(s) forgotten again"


def _creator_name_deleted(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    creator_id = entry.get("creator")
    if not Creator.objects.filter(pk=creator_id).exists():
        return ALREADY, ""
    if dry_run:
        return REAPPLIED, ""
    try:
        with transaction.atomic():
            Creator.objects.filter(pk=creator_id).delete()
    except ProtectedError:
        return FAILED, "protected: a suppression marker or a reference still names it"
    return REAPPLIED, ""


def _do_not_contact_recorded(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    digest = str(entry.get("identifier_hash"))
    if DoNotContactRecord.objects.filter(identifier_hash=digest).exists():
        return ALREADY, ""
    if dry_run:
        return REAPPLIED, ""
    creator_id = entry.get("creator")
    if creator_id is not None and not Creator.objects.filter(pk=creator_id).exists():
        creator_id = None  # the prospect row went; the "no" stays (the model's own rule)
    DoNotContactRecord.objects.create(
        identifier_hash=digest,
        identifier_kind=str(entry.get("identifier_kind")),
        received_on=datetime.fromisoformat(str(entry.get("received_on"))).date(),
        channel=str(entry.get("channel")),
        recorded_by=REPLAY_ACTOR,
        creator_id=creator_id,
        note=REPLAY_NOTE,
    )
    return REAPPLIED, ""


def _search_query_scrubbed(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    call = SearchCall.objects.filter(pk=entry.get("call")).only("id", "query").first()
    if call is None:
        return ALREADY, "the ledger row is not in the restored database"
    if not call.query:
        return ALREADY, ""
    if dry_run:
        return REAPPLIED, ""
    scrub_search_calls([call.pk], now=now)
    return REAPPLIED, ""


def _candidate_queries_scrubbed(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    candidate = SourceCandidate.objects.filter(pk=entry.get("candidate")).first()
    if candidate is None:
        return ALREADY, "the candidate is not in the restored database"
    cleaned, changed = scrubbed_evidence(candidate.evidence)
    if not changed:
        return ALREADY, ""
    if dry_run:
        return REAPPLIED, f"{changed} sample(s) to scrub again"
    SourceCandidate.objects.filter(pk=candidate.pk).update(evidence=cleaned)
    return REAPPLIED, f"{changed} sample(s) scrubbed again"


def _page_text_scrub(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    page = Page.objects.filter(pk=entry.get("page")).first()
    if page is None:
        return ALREADY, "the page row is not in the restored database"
    if not Page.objects.filter(pk=page.pk).filter(holds_scrubbable_text()).exists():
        return ALREADY, ""
    if page.safety_status != Page.SafetyStatus.CLEAR:
        return HUMAN, "the page is not safety-clear: its stored text is the safety record"
    if not stays_scrubbed(page):
        return HUMAN, "the page is active again in the restored state"
    if dry_run:
        return REAPPLIED, ""
    scrub_deactivated_pages([page.pk])
    return REAPPLIED, ""


def _page_deleted(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    page = Page.objects.filter(pk=entry.get("page")).only("id", "url").first()
    if page is None:
        return ALREADY, ""
    if sha256(page.url.encode()).hexdigest() != str(entry.get("url_sha256")):
        return FAILED, "the page at that id is a different URL; refusing to delete it"
    if Page.objects.filter(
        pk=page.pk, safety_incidents__incident__status__in=OPEN_SAFETY_STATUSES
    ).exists():
        return HUMAN, "an open or reported safety case is attached to it in the restored state"
    if dry_run:
        return REAPPLIED, ""
    try:
        with transaction.atomic():
            Page.objects.filter(pk=page.pk).delete()
    except ProtectedError:
        return FAILED, "protected: a customer has drafted a notice about it"
    return REAPPLIED, ""


def _safety_incident_deleted(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    incident = SafetyIncident.objects.filter(pk=entry.get("incident")).only("id", "status").first()
    if incident is None:
        return ALREADY, ""
    if incident.status not in (SafetyIncident.Status.CLOSED, SafetyIncident.Status.FALSE_POSITIVE):
        return HUMAN, f"the case is {incident.status} again: a human settles a case, not a replay"
    if dry_run:
        return REAPPLIED, ""
    SafetyIncident.objects.filter(pk=incident.pk).delete()
    return REAPPLIED, ""


def _case_file_reset(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    references = list(
        Reference.objects.filter(sha256=str(entry.get("sha256"))).values_list("pk", flat=True)
    )
    cases = LeakCase.objects.filter(reference_id__in=references)
    count = cases.count()
    if not count:
        return ALREADY, ""
    if dry_run:
        return REAPPLIED, f"{count} case row(s) to reset again"
    cases.delete()
    return REAPPLIED, f"{count} case row(s) reset again"


def _original_data_deleted(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    removal = (
        OriginalRemoval.objects.select_related("organisation")
        .filter(organisation_id=entry.get("organisation"), sha256=str(entry.get("sha256")))
        .order_by("pk")
        .first()
    )
    if removal is None:
        # The mark is stamped, never deleted, so its absence means the restore predates it — unless
        # the organisation itself is gone, in which case the deletion is simply in effect and there
        # is nothing to re-apply. Asking for a human there would make the weekly restore check
        # complain for ever about a pilot that ended cleanly months ago.
        if not owned_references_exist(int(entry.get("organisation")), str(entry.get("sha256"))):
            return ALREADY, "the organisation no longer owns that file"
        return (
            HUMAN,
            "no OriginalRemoval mark in the restored database: ADR 0045 runs off a date a human "
            "set, so re-mark it with `remove_original`",
        )
    report = delete_original(removal, now=now, dry_run=dry_run, by=None)
    if not report.counts:
        return ALREADY, ""
    return REAPPLIED, f"{report.rows} row(s)"


def _organisation_data_deleted(entry: Entry, *, dry_run: bool, now: datetime) -> tuple[str, str]:
    ending = (
        OrganisationEnding.objects.select_related("organisation")
        .filter(organisation_id=entry.get("organisation"))
        .order_by("pk")
        .first()
    )
    if ending is None:
        # As above: the mark cascades away with the organisation row, so "no mark and no
        # organisation" is the completed deletion, not a gap a human has to close.
        if not Organisation.objects.filter(pk=entry.get("organisation")).exists():
            return ALREADY, "the organisation row is gone"
        return (
            HUMAN,
            "no OrganisationEnding mark in the restored database: ADR 0045 runs off a date a "
            "human set, so re-mark the pilot as ended with `end_pilot`",
        )
    report = delete_organisation(ending, now=now, dry_run=dry_run, by=None)
    if not report.counts:
        return ALREADY, ""
    return REAPPLIED, f"{report.rows} row(s)"


def owned_references_exist(organisation_pk: int, sha256: str) -> bool:
    """Does this organisation still own a reference of that file? (ADR 0031's ownership table.)"""
    organisation = Organisation.objects.filter(pk=organisation_pk).first()
    if organisation is None:
        return False
    return owned_references(organisation).filter(sha256=sha256).exists()


Handler = Callable[..., tuple[str, str]]

HANDLERS: dict[str, Handler] = {
    "creator_suppression": _creator_suppression,
    "creator_suppression_lifted": _creator_suppression_lifted,
    "prospect_research_forgotten": _prospect_research_forgotten,
    "creator_name_deleted": _creator_name_deleted,
    "do_not_contact_recorded": _do_not_contact_recorded,
    "search_query_scrubbed": _search_query_scrubbed,
    "candidate_queries_scrubbed": _candidate_queries_scrubbed,
    "page_text_scrub": _page_text_scrub,
    "page_deleted": _page_deleted,
    "safety_incident_deleted": _safety_incident_deleted,
    "case_file_reset": _case_file_reset,
    "original_data_deleted": _original_data_deleted,
    "organisation_data_deleted": _organisation_data_deleted,
}

#: Every kind the journal can write has a handler and a place in the replay order. A new kind with
#: no handler would be silently ignored, which is the one failure this whole feature cannot afford.
assert set(HANDLERS) == set(deletion_journal.KIND_FIELDS), "a journal kind has no replay handler"
assert set(REPLAY_ORDER) == set(HANDLERS), "a replay handler is not in REPLAY_ORDER"


class JournalUnavailable(RuntimeError):
    """There is no journal to replay, so nothing can be said about what a restore undid."""


def replay(
    *,
    dry_run: bool = True,
    since: datetime | None = None,
    directory: Path | None = None,
    now: datetime | None = None,
    journal: Journal | None = None,
) -> ReplayReport:
    """Re-apply every journalled deletion to this database. Dry run by default.

    Raises `JournalUnavailable` when no journal directory is configured: answering "nothing to do"
    there would be the most dangerous possible answer.
    """
    moment = now or timezone.now()
    where = directory if directory is not None else deletion_journal.journal_dir()
    if where is None:
        raise JournalUnavailable(
            "DELETION_JOURNAL_DIR is not set: this database's deletions were never journalled, so "
            "a restore cannot be repaired from here (ADR 0057, docs/runbooks/backup-restore.md)"
        )
    read = journal if journal is not None else deletion_journal.read_journal(where, since=since)
    report = ReplayReport(
        dry_run=dry_run,
        now=moment,
        database=connection.settings_dict.get("NAME", "?"),
        journal_dir=str(where),
        since=since,
        unreadable=list(read.unreadable),
    )
    by_kind: dict[str, list[Entry]] = {kind: [] for kind in REPLAY_ORDER}
    for entry in read.entries:
        by_kind.setdefault(entry.kind, []).append(entry)
    for kind in REPLAY_ORDER:
        outcome = KindOutcome(kind=kind)
        handler = HANDLERS[kind]
        for entry in by_kind.get(kind, ()):
            outcome.entries += 1
            verdict, reason = handler(entry, dry_run=dry_run, now=moment)
            outcome.record(verdict, entry.key, reason)
        report.outcomes.append(outcome)
    if not dry_run:
        logger.warning(
            "DELETION JOURNAL replay on %s: %s entr(ies), %s already in effect, %s re-applied, "
            "%s could not be applied, %s need a human",
            report.database,
            report.entries,
            report.already,
            report.reapplied,
            report.failed,
            report.needs_human,
        )
    return report
