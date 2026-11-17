"""The case file: the process a customer can follow (docs/customer-ui-plan.md §3).

`/api/v1/app/findings`, `/api/v1/app/cases`, `/api/v1/app/cases/{original_id}` and
`/api/v1/app/removals` are one read model over `matching.LeakCase` / `CaseFinding`, which store
observations — *when* a transition was first seen — and nothing that can be derived from
`ReferenceMatch`, `ReferenceLeak` and `Page` at read time.

Four rules hold here, and the first three are the same ones the originals view is built on:

- **Scope.** Every view starts at `ownership.owned_originals`. Another organisation's case is
  answered exactly like a case that does not exist: 404, same body. A 403 would confirm it exists.
  One original is one video: the references of one file share one case, which lives on the
  representative (`LeakCase` is one-to-one with `Reference`). Originals and pages are named by
  their public ids only (ADR 0033); a malformed or integer id is one more unknown id.
- **Confirmed only, dated to the day** (operator decision, 2026-09-17). Findings come from
  `originals.confirmed_findings_for`: an unconfirmed page is no finding, no count, no case and no
  removal. Our own step, `confirmed`, is a date; the moments we first noticed a page
  (`discovered_at`, `opened_at`, the crawler's `first_seen`) never leave this module, so no pair of
  customer fields measures how fast we review. The stored stamps are unchanged — internal truth.
- **Safety.** Those findings come from `originals.findings_for`, which filters every page through
  `safety.visible_page_filter`. A flagged page is therefore never a finding, never a removal and
  never a count; if a page becomes flagged later, `sync_cases` deletes what it had written — and
  deletes the case with its last finding, because an empty case carrying `reopen_count = 2` would
  itself say that something is being hidden.
- **No imagery.** Nothing here reads `index.Media`. The evidence is site, URL and dates (#53);
  the `hosting` section names the hosts serving a page's previews, and `marceline.hosting` hands
  over their hostnames and nothing else (ADR 0027).
- **No prose.** Every value a UI branches on is a code (`marceline/app/schemas.py`). The product
  is bilingual from the start, so a German or English sentence in a payload is a bug: the UI owns
  the words, in the customer's language.

**What the timeline cannot know today is said, not invented.** Nothing in Marceline prepares a
takedown notice (#33), nothing sends one (#46), so nothing can be
acknowledged either. Those three steps are `unavailable` with a reason code, forever, until the
code that does them exists. Hoster metadata and jurisdiction are carried per finding since ADR 0027
(`hosting`); cooperation history has a table but nothing can fill it before a notice exists, so it
stays named in `not_available`.

**Reconciliation happens on read** (`sync_cases`, idempotent, one transaction per case) while
`CASE_SYNC_ON_READ` is on, the default. A case is living state that must reflect the crawler's
latest recheck; `sync_all_cases` (CLI `sync_cases`) is the same reconciliation for every
organisation, for a scheduler job. With the setting off (ADR 0028: the public cache does not write
on read) the views read the stored cases as the last sync left them (`stored_cases`), filtered
through today's visibility, and write nothing. Only originals the requesting organisation owns
are ever touched by a read.

**The case follows the representative.** When a newer reference of the same file appears (a v1
or 2 fps re-fingerprint), the next sync moves the case from the old representative to the new one
and keeps every stamp; if two references of the file had each grown a case before they were
grouped, the cases are merged into one (`_case_of`) — earliest observations win, nothing is
re-dated.

**One reconciliation per original at a time.** Two readers of the same original — two browser
tabs, or a reader and the sync job — would otherwise both see "no case yet" and both create one
(a 500 from the one-case-per-original constraint), or one would stamp a case the other had just
deleted. `_sync_case` therefore takes a transaction-scoped advisory lock on the original's file
(its sha256, so a reader that still sees the old representative queues behind one that sees the
new) *before* it reads anything, and reads the findings and the stored case under it. It is an
advisory lock and not `SELECT … FOR UPDATE` on the `Reference` row on purpose: matching holds that
row lock (and key-share locks from inserting match rows) for a whole matching run, and a customer
read must not queue behind it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from django.conf import settings
from django.db import connection, transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status

from accounts.models import Organisation
from index.models import Page
from marceline.app.auth import customer_auth
from marceline.app.hosting import hosting_by_page
from marceline.app.originals import (
    Finding,
    confirmed_findings_of,
    counts_of,
    evidence_of,
    extent_of,
    original_out,
)
from marceline.app.ownership import (
    OwnedOriginal,
    organisation_of,
    owned_original,
    owned_originals,
)
from marceline.app.schemas import (
    AppErrorOut,
    CaseState,
    CaseStep,
    CaseStepOut,
    CaseUnknownOut,
    CustomerCaseDetailOut,
    CustomerCaseFindingOut,
    CustomerCaseOut,
    CustomerFindingHostingOut,
    CustomerRemovalOut,
)
from matching.models import CaseFinding, LeakCase, Reference

router = Router(tags=["app"], auth=customer_auth)
findings_router = Router(tags=["app"], auth=customer_auth)
removals_router = Router(tags=["app"], auth=customer_auth)

# The same answer for "does not exist" and "belongs to someone else". Never make them differ.
NOT_FOUND = "Case not found"

REMOVED = Page.Availability.REMOVED
WATCHING = LeakCase.State.REMOVED_WATCHING

#: Takedown notice drafts (issue #33, ADR 0035): built now — the record's timeline reads them
#: below. Real system-side *sending* is not built (#46): the customer signs
#: and sends from their own mailbox or provider account, so `notice_delivery` stays unavailable.
NOTICE_ISSUE = 33
#: System-side transmission of a notice is tracked separately. Until it exists, nothing but
#: the customer's own "mark as sent by me" moves a notice to "sent".
DELIVERY_ISSUE = 46

#: What the case file does not know today. Named absences, never placeholder data. Since #33,
#: `cooperation_history` and `notice_preparation` are real (their own per-provider/per-notice
#: sections say "no history yet" or show the actual state, rather than the record naming a
#: capability gap); only the system's own transmission remains unavailable.
NOT_AVAILABLE: tuple[CaseUnknownOut, ...] = (
    CaseUnknownOut(
        topic="notice_delivery", reason="legal_review_pending", blocked_by_issue=DELIVERY_ISSUE
    ),
    # "No re-upload anywhere" is only ever as true as our site coverage, and no per-original
    # standing watch exists yet. Saying which sites were watched is also an open product question
    # (plan §8, question 1), so the case file says "not known" instead of implying omniscience.
    CaseUnknownOut(topic="reupload_watch_coverage", reason="capability_not_built"),
)


@dataclass(frozen=True)
class CaseView:
    """One original, its visible findings, and the stored case state reconciled with them.

    `original.public_id` is the id a customer sees; `reference` (its representative) the title.
    """

    original: OwnedOriginal
    rows: list[Finding]
    case: LeakCase | None
    entries: dict[int, CaseFinding]

    @property
    def reference(self) -> Reference:
        return self.original.representative


#: First key of the two-integer advisory lock `_sync_case` takes per original. The two-integer key
#: space does not overlap the single-bigint keys the scheduler, clustering and capture use.
CASE_SYNC_LOCK_NAMESPACE = 250025


def sync_cases(
    originals: Sequence[OwnedOriginal], *, now: datetime | None = None
) -> list[CaseView]:
    """Reconcile the stored cases of these originals with what the data says now.

    Idempotent: called twice in the same second it writes nothing the second time, and safe to
    run concurrently for the same original (see the module docstring). Callers pass only originals
    the requesting organisation owns.
    """
    now = now or timezone.now()
    return [_sync_case(original, now) for original in originals]


def sync_all_cases(*, now: datetime | None = None) -> list[CaseView]:
    """Reconcile the cases of every original any organisation owns: the scheduler-side sync.

    Ownership is asked per organisation through `owned_originals`, the single definition of it.
    """
    now = now or timezone.now()
    views: list[CaseView] = []
    for organisation in Organisation.objects.order_by("pk"):
        originals = sorted(owned_originals(organisation), key=lambda original: original.pk)
        views += sync_cases(originals, now=now)
    return views


def stored_cases(originals: Sequence[OwnedOriginal]) -> list[CaseView]:
    """The cases as the last sync left them, for reads that must not write (`CASE_SYNC_ON_READ`).

    Only findings that are visible and confirmed **today** and that the last sync recorded are
    shown: a page flagged since then is gone at once, and a page confirmed since then appears after
    the next sync. A case with no such finding left is no case, exactly as `sync_cases` would
    delete it. Case-level state (removed, reopened) is as fresh as the last sync. A case the last
    sync left on an older member of the file (a newer reference appeared since) is read from there;
    the next sync moves it.
    """
    findings = confirmed_findings_of(originals)
    cases = {
        case.reference_id: case
        for case in LeakCase.objects.filter(
            reference_id__in=[pk for original in originals for pk in original.member_ids]
        ).prefetch_related("findings")
    }
    views: list[CaseView] = []
    for original in originals:
        stored_on = [cases[pk] for pk in (original.pk, *original.member_ids) if pk in cases]
        case = stored_on[0] if stored_on else None
        stored = {entry.page_id: entry for entry in case.findings.all()} if case else {}
        rows = [row for row in findings.get(original.pk, []) if row.page.pk in stored]
        if case is None or not rows:
            views.append(CaseView(original, [], None, {}))
            continue
        entries = {row.page.pk: stored[row.page.pk] for row in rows}
        views.append(CaseView(original, rows, case, entries))
    return views


def case_views(originals: Sequence[OwnedOriginal]) -> list[CaseView]:
    """What every case endpoint reads: reconciled on read, or the stored state when that is off."""
    if settings.CASE_SYNC_ON_READ:
        return sync_cases(originals)
    return stored_cases(originals)


def file_lock_key(sha256: str) -> int:
    """The second key of an original's advisory lock: stable across representatives."""
    return int(sha256[:8], 16) % 2**31


@transaction.atomic
def _sync_case(original: OwnedOriginal, now: datetime) -> CaseView:
    reference = original.representative
    # Serialise every reconciliation of this original first, then read: a finding list or a case
    # row loaded before the lock may already be out of date when it is written back.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [CASE_SYNC_LOCK_NAMESPACE, file_lock_key(reference.sha256)],
        )
    rows = confirmed_findings_of([original]).get(original.pk, [])
    case = _case_of(original)
    if not rows:
        # Everything visible is gone: rejected by a reviewer, or safety-flagged. The case goes
        # with it — an empty case row would be a trace of what the customer may not see.
        if case is not None:
            case.delete()
        return CaseView(original, [], None, {})
    if case is None:
        case = LeakCase.objects.create(
            reference=reference,
            state=LeakCase.State.OPEN,
            opened_at=min(row.detected_at for row in rows),
        )
        entries: dict[int, CaseFinding] = {}
    else:
        entries = {entry.page_id: entry for entry in case.findings.all()}
    visible = {row.page.pk for row in rows}
    stale = [entry.pk for page_id, entry in entries.items() if page_id not in visible]
    if stale:
        CaseFinding.objects.filter(pk__in=stale).delete()
        entries = {page_id: entry for page_id, entry in entries.items() if page_id in visible}
    for row in rows:
        entries[row.page.pk] = _sync_finding(case, row, entries.get(row.page.pk), now)
    _sync_state(case, rows, list(entries.values()), now)
    return CaseView(original, rows, case, entries)


def _case_of(original: OwnedOriginal) -> LeakCase | None:
    """The original's one case, on its representative — moved or merged there if need be.

    A case left on an older member (a newer reference of the file became the representative) is
    moved, stamps and all. Several cases on members of one file — grown before references were
    grouped — are merged into one: the representative's if it has one, else the earliest opened.
    """
    cases = list(
        LeakCase.objects.filter(reference_id__in=original.member_ids)
        .prefetch_related("findings")
        .order_by("opened_at", "pk")
    )
    if not cases:
        return None
    target = next((case for case in cases if case.reference_id == original.pk), cases[0])
    for other in cases:
        if other.pk != target.pk:
            _merge_case(target, other)
    if target.reference_id != original.pk:
        target.reference = original.representative
        target.save(update_fields=["reference", "updated_at"])
    return LeakCase.objects.prefetch_related("findings").get(pk=target.pk)


def _merge_case(target: LeakCase, other: LeakCase) -> None:
    """Fold `other` into `target`: every page once, the earliest observation of each step kept."""
    entries = {entry.page_id: entry for entry in target.findings.all()}
    for entry in other.findings.all():
        kept = entries.get(entry.page_id)
        if kept is None:
            entry.case = target
            entry.save(update_fields=["case", "updated_at"])
            continue
        earlier, later = sorted((kept, entry), key=lambda row: (row.discovered_at, row.pk))
        verified = [row.verified_at for row in (kept, entry) if row.verified_at is not None]
        removal = earlier if earlier.removed_at is not None else later
        kept.discovered_at = earlier.discovered_at
        kept.verified_at = min(verified) if verified else None
        kept.removed_at, kept.reappeared_at = removal.removed_at, removal.reappeared_at
        kept.save(
            update_fields=[
                "discovered_at",
                "verified_at",
                "removed_at",
                "reappeared_at",
                "updated_at",
            ]
        )
        entry.delete()
    target.opened_at = min(target.opened_at, other.opened_at)
    target.reopen_count = max(target.reopen_count, other.reopen_count)
    reopened = [case.reopened_at for case in (target, other) if case.reopened_at is not None]
    target.reopened_at = max(reopened) if reopened else None
    removed = [case.removed_at for case in (target, other) if case.removed_at is not None]
    target.removed_at = max(removed) if removed else None
    # `other` goes first: `target` may be about to take over its reference.
    other.delete()
    target.save(
        update_fields=["opened_at", "reopen_count", "reopened_at", "removed_at", "updated_at"]
    )


def _sync_finding(
    case: LeakCase, row: Finding, entry: CaseFinding | None, now: datetime
) -> CaseFinding:
    """Stamp what is newly observed about one page. A stamp is written once and never moves."""
    if entry is None:
        entry = CaseFinding(case=case, page=row.page, discovered_at=row.detected_at)
    changed: list[str] = []
    if row.confirmed and entry.verified_at is None:
        entry.verified_at = row.confirmed_at or now
        changed.append("verified_at")
    gone = row.page.availability == REMOVED
    checked = row.page.availability_checked_at
    if gone and (entry.removed_at is None or entry.reappeared_at is not None):
        # First removal, or removed again after having come back: re-arm the pair.
        entry.removed_at = checked or now
        entry.reappeared_at = None
        changed += ["removed_at", "reappeared_at"]
    elif not gone and entry.removed_at is not None and entry.reappeared_at is None:
        entry.reappeared_at = checked or now
        changed.append("reappeared_at")
    if entry.pk is None:
        entry.save()
    elif changed:
        entry.save(update_fields=[*changed, "updated_at"])
    return entry


def _sync_state(
    case: LeakCase, rows: list[Finding], entries: list[CaseFinding], now: datetime
) -> None:
    """Case state follows the pages: gone only when every finding is gone ("unknown" is not gone).

    A case that was fully removed and has something up again **reopens** — it is never closed and
    never archived, because the operator's definition of handled is "removed and not re-uploaded
    anywhere" (plan §8).
    """
    state = (
        WATCHING if all(row.page.availability == REMOVED for row in rows) else LeakCase.State.OPEN
    )
    opened_at = min(entry.discovered_at for entry in entries)
    changed: list[str] = []
    if case.opened_at != opened_at:
        case.opened_at = opened_at
        changed.append("opened_at")
    if case.state != state:
        if state == WATCHING:
            stamps = [entry.removed_at for entry in entries if entry.removed_at is not None]
            case.removed_at = max(stamps) if stamps else now
            changed.append("removed_at")
        else:
            case.reopened_at = now
            case.reopen_count += 1
            changed += ["reopened_at", "reopen_count"]
        case.state = state
        changed.append("state")
    if changed:
        case.save(update_fields=[*changed, "updated_at"])


def case_state_of(case: LeakCase | None) -> CaseState:
    return "removed_watching" if case is not None and case.state == WATCHING else "open"


def online_of(page: Page) -> bool | None:
    """Online, gone, or not knowable from the last check — three answers, not two."""
    if page.availability == Page.Availability.PLAYABLE:
        return True
    if page.availability == REMOVED:
        return False
    return None


def case_finding_out(
    view: CaseView, row: Finding, hosting: dict[int, CustomerFindingHostingOut]
) -> CustomerCaseFindingOut:
    entry = view.entries[row.page.pk]
    page = row.page
    return CustomerCaseFindingOut(
        original_id=view.original.public_id,
        page_id=page.public_id,
        site=page.site.domain,
        url=page.url,
        case_state=case_state_of(view.case),
        availability=page.availability,
        online=online_of(page),
        last_checked=page.availability_checked_at,
        confirmed_on=confirmed_on(entry),
        removed_at=entry.removed_at,
        reappeared_at=entry.reappeared_at,
        evidence=evidence_of(row),
        extent=extent_of(row),
        hosting=hosting[page.pk],
    )


def hosting_of(views: Sequence[CaseView]) -> dict[int, CustomerFindingHostingOut]:
    """The hosting section of every finding of these cases, looked up in one go."""
    return hosting_by_page((row.page for view in views for row in view.rows), timezone.now())


def case_out(view: CaseView) -> CustomerCaseOut | None:
    """None while nothing has been found: there is no case to follow yet."""
    if view.case is None:
        return None
    return CustomerCaseOut(
        original_id=view.original.public_id,
        title=view.reference.title,
        state=case_state_of(view.case),
        opened_on=min(confirmed_on(entry) for entry in view.entries.values()),
        removed_at=view.case.removed_at,
        reopened_on=day(view.case.reopened_at) if view.case.reopened_at is not None else None,
        reopen_count=view.case.reopen_count,
        counts=counts_of(view.rows),
    )


def day(moment: datetime) -> date:
    """A moment as the day it fell on, in the project's time zone."""
    return timezone.localdate(moment)


def confirmed_on(entry: CaseFinding) -> date:
    """The day a copy was confirmed, from the stored observation — never the decision row.

    Every customer finding is confirmed, so `verified_at` is set; the fallback only keeps the type
    honest and says no more than the day either way. A decision row's `updated_at` moves whenever
    matching saves the row again, so reading it here would let the date drift (see
    `originals.Finding` for what that leaves before the first sync).
    """
    return day(entry.verified_at if entry.verified_at is not None else entry.discovered_at)


def _step(step: CaseStep, at: datetime | None) -> CaseStepOut:
    return CaseStepOut(step=step, status="done" if at is not None else "pending", at=at)


@dataclass(frozen=True)
class NoticeStamps:
    """The earliest moment any of this original's notice drafts reached each stage — a
    historical fact, so a later withdrawal never un-happens it (issue #33, ADR 0035)."""

    prepared_at: datetime | None
    sent_at: datetime | None
    acknowledged_at: datetime | None


def notice_stamps_of(original: OwnedOriginal) -> NoticeStamps:
    """Read-only: every notice draft ever created for this original, any status.

    Scoped by the original's own public id, which is enough on its own: it is a random id unique
    across every organisation (ADR 0033), never reused, so there is no risk of reading another
    organisation's drafts even without an explicit organisation filter here.

    `marceline.notices` is imported here, not at module level, to keep the case file's own
    import graph the shallow one it has always been; both modules live in the project package, so
    there is no boundary to cross, only a habit to keep.
    """
    from marceline.notices.models import NoticeDraft

    rows = NoticeDraft.objects.filter(original_public_id=original.public_id).values_list(
        "approved_at", "marked_sent_at", "responded_at"
    )
    prepared = [row[0] for row in rows if row[0] is not None]
    sent = [row[1] for row in rows if row[1] is not None]
    acknowledged = [row[2] for row in rows if row[2] is not None]
    return NoticeStamps(
        prepared_at=min(prepared) if prepared else None,
        sent_at=min(sent) if sent else None,
        acknowledged_at=min(acknowledged) if acknowledged else None,
    )


def timeline_of(view: CaseView) -> list[CaseStepOut]:
    """The six customer steps of plan §3, always all of them, derived or honestly unavailable.

    - `confirmed` — the day the first copy was confirmed as this original (a person, or an auto
      decision a reviewer can reverse). Found and verified are one step for a customer, dated to
      the day and no more precisely: how long review takes is internal (2026-09-17);
    - `notice_prepared` — done once any notice draft for this original was approved
      (issue #33): the customer confirmed every declaration and the exact text was frozen;
    - `notice_sent` — done once the customer marked one as sent (by themselves, or through the
      sandbox path for the operator's test organisation) — attributed to "you", never "us"
      (plan §4: Marceline does not transmit notices itself, #46);
    - `notice_acknowledged` — done once a provider response was recorded for one of them;
    - `removed` — the case is fully removed. One finding of several coming down is a count, not
      this step: it would be a promise the other URLs have not kept;
    - `rechecked` — the latest availability check of any finding. It is what keeps "removed" from
      going stale, and it is also how a re-upload is noticed.
    """
    case = view.case
    confirmed = [confirmed_on(entry) for entry in view.entries.values()]
    checked = [
        row.page.availability_checked_at
        for row in view.rows
        if row.page.availability_checked_at is not None
    ]
    notices = notice_stamps_of(view.original)
    return [
        CaseStepOut(
            step="confirmed",
            status="done" if confirmed else "pending",
            at=None,
            on=min(confirmed) if confirmed else None,
        ),
        _step("notice_prepared", notices.prepared_at),
        _step("notice_sent", notices.sent_at),
        _step("notice_acknowledged", notices.acknowledged_at),
        _step("removed", case.removed_at if case is not None and case.state == WATCHING else None),
        _step("rechecked", max(checked) if checked else None),
    ]


def removal_out(view: CaseView, row: Finding) -> CustomerRemovalOut | None:
    """One removals-log entry, or None when this finding is not one.

    Only a **confirmed** finding that was observed gone counts. An unconfirmed page that vanished
    may have vanished for any reason; calling it a removal would claim work we cannot prove.
    """
    entry = view.entries[row.page.pk]
    if entry.removed_at is None or entry.verified_at is None:
        return None
    state = case_state_of(view.case)
    page = row.page
    return CustomerRemovalOut(
        original_id=view.original.public_id,
        title=view.reference.title,
        page_id=page.public_id,
        site=page.site.domain,
        url=page.url,
        removed_at=entry.removed_at,
        confirmed_on=confirmed_on(entry),
        reappeared_at=entry.reappeared_at,
        last_checked=page.availability_checked_at,
        state="reopened" if entry.reappeared_at is not None else "removed",
        case_state=state,
        # The operator's definition: removed **and** not re-uploaded anywhere we look. Both halves
        # are required, so one entry of a reopened case is not "handled" even though its own URL
        # is still gone.
        handled=entry.reappeared_at is None and state == "removed_watching",
    )


def owned_cases(request: HttpRequest) -> list[CaseView]:
    """Every case of the requesting organisation, reconciled. Someone else's is not in here."""
    return case_views(owned_originals(organisation_of(request.user)))


@findings_router.get("", response=list[CustomerCaseFindingOut])
def findings(request: HttpRequest) -> list[CustomerCaseFindingOut]:
    """Every page with a human-reviewed match of this organisation's originals, and its record."""
    views = owned_cases(request)
    hosting = hosting_of(views)
    return [case_finding_out(view, row, hosting) for view in views for row in view.rows]


@router.get("", response=list[CustomerCaseOut])
def cases(request: HttpRequest) -> list[CustomerCaseOut]:
    """One record per original something was found of. An original with nothing found has none."""
    rows = (case_out(view) for view in owned_cases(request))
    return [row for row in rows if row is not None]


@router.get("/{original_id}", response={200: CustomerCaseDetailOut, 404: AppErrorOut})
def case_detail(
    request: HttpRequest, original_id: str
) -> Status[CustomerCaseDetailOut | AppErrorOut]:
    """One record by the original's public id: the original, its matching copies, the timeline,
    and what is not known yet.

    404 unless this organisation owns the original — byte-identical for a malformed, unknown or
    foreign id.
    An owned original with nothing found answers 200 with `case: null` and a timeline whose first
    step is still pending: "nothing found" is an answer, not a missing resource.
    """
    original = owned_original(organisation_of(request.user), original_id)
    if original is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    [view] = case_views([original])
    hosting = hosting_of([view])
    return Status(
        200,
        CustomerCaseDetailOut(
            original=original_out(original, view.rows),
            case=case_out(view),
            findings=[case_finding_out(view, row, hosting) for row in view.rows],
            timeline=timeline_of(view),
            not_available=list(NOT_AVAILABLE),
        ),
    )


@removals_router.get("", response=list[CustomerRemovalOut])
def removals(request: HttpRequest) -> list[CustomerRemovalOut]:
    """What came down and when, newest first — living state, not an archive.

    An entry can reopen: `state` is `reopened` when that URL came back, and `handled` ("no copy
    online") is false whenever anything else of the same original is up again.
    """
    entries = [
        entry
        for view in owned_cases(request)
        for row in view.rows
        if (entry := removal_out(view, row)) is not None
    ]
    return sorted(entries, key=lambda entry: (entry.removed_at, entry.page_id), reverse=True)
