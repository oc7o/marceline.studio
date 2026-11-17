"""The notice gate (issue #33, ADR 0035): why a finding may not go into a notice draft.

Composes existing helpers, exactly as the operator's build spec asks — nothing here re-implements
the rights model, copy-extent classification or the provider directory:

- match band `high` + human content-identity confirmation (`matching.ReferenceMatch`, a `confirmed`
  status *is* the human review step in this codebase — the review queue is how a match gets there);
- a recent availability check with no media download (`index.Page.availability`);
- rights blockers (`marceline.rights.gate.notice_gate_blockers`);
- a licensed-partner check for the URL's host (`marceline.rights.partners.partner_for_url`);
- the copy-extent exception review (`matching.copy_extent.requires_exception_review`) — satisfied
  once the customer has ticked the per-finding exception confirmation;
- a provider role with a supported v1 channel (`catalog.hosting.directory.provider_roles`).

`finding_blockers` is pure; `finding_gate_facts` reads a snapshot (read-only). An empty list means
this finding is eligible for a notice draft; `review`/`low` band and rejected matches are never
eligible — they simply never reach `high` + `confirmed`, and a row the second signal proposed
(ADR 0037) is excluded from the file's verdict altogether.

**`file_matches` is the one match lookup all three call sites share** (`candidates`,
`create_draft`, `approve`), so the screen, the draft and its approval can never judge the same
copy differently. It reads every reference of the file **this organisation owns** (ADR 0022: a
re-fingerprint is a second `Reference`, and the customer sees one original), needs one confirmed
`high` row among them, and lets any rejected row veto.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.conf import settings
from django.utils import timezone

from accounts.models import Organisation
from catalog.hosting.directory import provider_roles
from catalog.hosting.services import normalize_hostname
from index.models import Page
from marceline.app.ownership import owned_references
from marceline.rights.gate import Work, notice_gate_blockers
from marceline.rights.partners import partner_for_url
from matching.copy_extent import (
    CopyExtent,
    combine_extents,
    current_extent,
    extent_requires_exception_review,
)
from matching.models import Reference, ReferenceMatch


class FindingBlocker(StrEnum):
    NO_HIGH_CONFIRMED_MATCH = "no_high_confirmed_match"
    PAGE_NOT_AVAILABLE = "page_not_available"
    AVAILABILITY_NOT_RECENT = "availability_not_recent"
    LICENSED_PARTNER_MATCH = "licensed_partner_match"
    EXCEPTION_REVIEW_REQUIRED = "exception_review_required"
    NO_SUPPORTED_CHANNEL = "no_supported_channel"


def notice_availability_max_age() -> timedelta:
    return timedelta(hours=settings.NOTICE_AVAILABILITY_MAX_AGE_HOURS)


@dataclass(frozen=True)
class FileMatch:
    """What *every* match row of one file says about one page, reduced to one answer.

    Not a row: a verdict over all of them. An original is a file, and a re-fingerprint under a
    newer pipeline or another sampling rate is a second `Reference` of it (ADR 0022, issue #70),
    so several rows can describe the same copy. Two rules decide, and neither depends on which
    row a query returns first:

    - `confirmed_high` — **any** of them confirmed at band `high`. A human's confirmation lands on
      the row that was reviewed, which need not belong to the representative the customer sees,
      and the customer's own view already counts a page confirmed on any reference of the file as
      one confirmed copy (`marceline.app.originals.confirmed_findings_of`). The gate has to agree,
      or it refuses a notice for a copy the customer was shown as confirmed.
    - `rejected` — **any** of them rejected. An explicit human "no, this is not the same work"
      vetoes, whatever another sampling grid says about the same page: `no_high_confirmed_match`
      is this gate's only human review step, and a second row must not stand in for a decision
      somebody took against it. So `eligible` needs the confirmation *and* the absence of a
      rejection.

    `extent` is the most cautious of the rows (`copy_extent.combine_extents`), which is what the
    customer's own extent already shows for a file's twins on one page.
    """

    confirmed_high: bool
    rejected: bool
    extent: CopyExtent
    #: When a human confirmed this copy at band `high` — the **earliest** such decision where
    #: several references of the file confirm it (issue #142). It is our own record of the review
    #: step this gate insists on, it is snapshotted into a draft at drafting time, and the rendered
    #: notice names it: so it is the earliest, never "the latest write to the row".
    confirmed_at: datetime | None = None

    @property
    def eligible(self) -> bool:
        """A standing human confirmation: one confirmed `high` row and no rejected one."""
        return self.confirmed_high and not self.rejected

    @property
    def requires_exception_review(self) -> bool:
        return extent_requires_exception_review(self.extent)


def file_matches(
    organisation: Organisation, sha256: str, page_ids: Collection[int]
) -> dict[int, FileMatch]:
    """Per page, what this organisation's references of this file say about it.

    The one lookup `candidates`, `create_draft` and `approve` share, so the screen, the draft and
    its approval can never judge the same copy differently.

    Scoped through `ownership.owned_references`: only references this organisation owns count.
    A twin owned by somebody else (or one this organisation no longer owns) is not evidence for
    its notice — and an organisation that owns none of the file's references gets no entry at all,
    which the gate reads as `no_high_confirmed_match`.

    A row a candidate generator proposed — the embedding (ADR 0037) or the metadata scorer
    (ADR 0040) — carries no frame evidence and cannot reach the `high` band at all, so it says
    nothing about the file and is excluded here: left in, its `unknown` extent would be the most
    cautious one and would block a notice the real match rows allow, and a human rejecting a
    candidate the machine only guessed at would veto them too.
    """
    ids = list(page_ids)
    if not ids:
        return {}
    facts: dict[int, FileMatch] = {}
    rows = ReferenceMatch.objects.filter(
        reference__in=owned_references(organisation).filter(sha256=sha256), page_id__in=ids
    ).exclude(candidate_source__in=ReferenceMatch.CANDIDATE_ONLY_SOURCES)
    for row in rows:
        seen = facts.get(row.page_id)
        extent = current_extent(row)
        this_confirms = (
            row.status == ReferenceMatch.Status.CONFIRMED and row.band == ReferenceMatch.Band.HIGH
        )
        facts[row.page_id] = FileMatch(
            confirmed_high=(seen.confirmed_high if seen else False) or this_confirms,
            rejected=(seen.rejected if seen else False)
            or row.status == ReferenceMatch.Status.REJECTED,
            extent=combine_extents([seen.extent, extent]) if seen else extent,
            confirmed_at=_earliest(
                seen.confirmed_at if seen else None,
                row.decided_at if this_confirms else None,
            ),
        )
    return facts


def _earliest(left: datetime | None, right: datetime | None) -> datetime | None:
    """The earlier of two moments, ignoring a missing one. Pure."""
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


@dataclass(frozen=True)
class FindingFacts:
    """What the pure gate needs to know about one (reference, page) pair."""

    now: datetime
    match_confirmed_high: bool
    page_availability: str
    availability_checked_at: datetime | None
    licensed_partner: bool
    extent_requires_review: bool
    exception_confirmed: bool
    has_supported_channel: bool
    max_availability_age: timedelta


def finding_blockers(facts: FindingFacts) -> list[str]:
    """Every reason this finding may not go into a notice draft, in a fixed order. Pure."""
    found: list[str] = []
    if not facts.match_confirmed_high:
        found.append(FindingBlocker.NO_HIGH_CONFIRMED_MATCH)
    if facts.page_availability != Page.Availability.PLAYABLE:
        found.append(FindingBlocker.PAGE_NOT_AVAILABLE)
    elif (
        facts.availability_checked_at is None
        or facts.now - facts.availability_checked_at > facts.max_availability_age
    ):
        found.append(FindingBlocker.AVAILABILITY_NOT_RECENT)
    if facts.licensed_partner:
        found.append(FindingBlocker.LICENSED_PARTNER_MATCH)
    if facts.extent_requires_review and not facts.exception_confirmed:
        found.append(FindingBlocker.EXCEPTION_REVIEW_REQUIRED)
    if not facts.has_supported_channel:
        found.append(FindingBlocker.NO_SUPPORTED_CHANNEL)
    return [str(blocker) for blocker in found]


def _has_supported_channel(hostname: str, now: datetime) -> bool:
    roles = provider_roles([hostname], now).get(hostname, [])
    return any(role.suggested is not None for role in roles)


def finding_gate_facts(
    organisation: Organisation,
    reference: Reference,
    page: Page,
    match: FileMatch | None,
    *,
    exception_confirmed: bool = False,
    now: datetime | None = None,
) -> FindingFacts:
    """The real entry point: facts for one (reference, page) finding. Read-only.

    `match` is the file's verdict for this page (`file_matches`), never a single row: a rejection
    on one of the file's references vetoes a confirmation on another, and the extent is the most
    cautious of them.
    """
    now = now or timezone.now()
    hostname = normalize_hostname(page.site.domain)
    partner = partner_for_url(organisation, page.url)
    return FindingFacts(
        now=now,
        match_confirmed_high=match is not None and match.eligible,
        page_availability=page.availability,
        availability_checked_at=page.availability_checked_at,
        licensed_partner=partner is not None,
        extent_requires_review=match.requires_exception_review if match is not None else True,
        exception_confirmed=exception_confirmed,
        has_supported_channel=_has_supported_channel(hostname, now),
        max_availability_age=notice_availability_max_age(),
    )


def finding_gate_blockers(
    organisation: Organisation,
    reference: Reference,
    page: Page,
    match: FileMatch | None,
    *,
    exception_confirmed: bool = False,
    now: datetime | None = None,
) -> list[str]:
    """Rights blockers (ADR 0034) plus this notice feature's own match/availability/partner/
    exception/channel blockers, for one finding. Rights codes come through unchanged."""
    rights = notice_gate_blockers(
        Work(organisation, reference.sha256), today=(now or timezone.now()).date()
    )
    own = finding_blockers(
        finding_gate_facts(
            organisation, reference, page, match, exception_confirmed=exception_confirmed, now=now
        )
    )
    return [*rights, *own]


def eligible(blockers: Iterable[str]) -> bool:
    return not list(blockers)
