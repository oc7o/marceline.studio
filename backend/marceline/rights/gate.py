"""Why a notice draft may not be prepared for a work: the rights half of the gate (ADR 0034).

`gate_blockers(facts)` is pure: it takes a snapshot of what is known and returns machine-readable
blocker codes, in a fixed order. `notice_gate_blockers(work)` reads that snapshot from the database
(read-only — it never changes a status) and evaluates it. An empty list means the rights side has
no objection; the match side (human-confirmed, rechecked, customer-adopted) is a separate gate the
future notice feature adds on top.

Codes, not words: the customer UI and the admin own the language. **A code is a fact about our own
record**, never a reading of a customer's document (`docs/legal/answers/03-rdg-operating-model.md`
Q3) — `documents_not_complete` means the documents have not passed the formal-completeness check,
`authority_not_declared` means the customer has not yet stated that those documents give them the
right or authority they declared, and `mandate_notice_clause_not_recorded` means no notice clause
is recorded for the mandate. Nothing here or downstream may turn one of those into a statement
about what a document legally covers.

The two halves of the old `verified` are both read from `WorkRights.status`, so the gate needs no
new fact: `documents_complete` is ours and formal, `authority_declared` is the customer's own
statement and the only thing that clears the last of the two.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from django.db.models import Count
from django.utils import timezone

from accounts.models import Organisation
from marceline.models import OriginalOwnership
from marceline.rights.models import (
    CLOSED_STATUSES,
    AgencyMandate,
    Capacity,
    IdentityStatus,
    LicensedPartner,
    RightsClaimant,
    RightsStatus,
    WorkRights,
)


class Blocker(StrEnum):
    WORK_NOT_OWNED = "work_not_owned"
    CLAIMANT_TYPE_MISSING = "claimant_type_missing"
    IDENTITY_NOT_VERIFIED = "identity_not_verified"
    RIGHTS_RECORD_MISSING = "rights_record_missing"
    DOCUMENTS_NOT_COMPLETE = "documents_not_complete"
    AUTHORITY_NOT_DECLARED = "authority_not_declared"
    DECLARATION_INCOMPLETE = "declaration_incomplete"
    RIGHTS_DISPUTED = "rights_disputed"
    RIGHTS_REVOKED = "rights_revoked"
    RIGHTS_EXPIRED = "rights_expired"
    COMPETING_CLAIM = "competing_claim"
    UNDERLYING_OWNER_MISSING = "underlying_owner_missing"
    MANDATE_MISSING = "mandate_missing"
    MANDATE_NOT_YET_VALID = "mandate_not_yet_valid"
    MANDATE_EXPIRED = "mandate_expired"
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_NOTICE_CLAUSE_NOT_RECORDED = "mandate_notice_clause_not_recorded"
    SCHEDULE_EXPIRED = "schedule_expired"
    PARTNERS_NOT_AFFIRMED = "partners_not_affirmed"


@dataclass(frozen=True)
class Work:
    """One work as the gate sees it: a file (sha256) as one organisation claims it."""

    organisation: Organisation
    sha256: str


@dataclass(frozen=True)
class MandateFacts:
    valid_from: date
    valid_until: date | None
    revoked: bool
    authority_to_prepare_notices: bool


@dataclass(frozen=True)
class GateFacts:
    today: date
    owned: bool
    claimant_type: str
    identity_status: str
    status: str | None  # None: no current rights record
    capacity: str = ""
    asserted_right: str = ""
    underlying_owner_name: str = ""
    mandate: MandateFacts | None = None
    schedule_valid_until: date | None = None
    competing_claims: int = 0
    partner_count: int = 0
    partners_none_affirmed: bool = False


#: The one status with no entry is `authority_declared`: our formal check has passed **and** the
#: customer has made the statement about their documents. Every other open status is missing one
#: of the two.
_STATUS_BLOCKERS: dict[str, Blocker] = {
    RightsStatus.UNVERIFIED: Blocker.DOCUMENTS_NOT_COMPLETE,
    RightsStatus.SELF_DECLARED: Blocker.DOCUMENTS_NOT_COMPLETE,
    RightsStatus.DOCUMENTS_RECEIVED: Blocker.DOCUMENTS_NOT_COMPLETE,
    RightsStatus.DOCUMENTS_COMPLETE: Blocker.AUTHORITY_NOT_DECLARED,
    RightsStatus.DISPUTED: Blocker.RIGHTS_DISPUTED,
    RightsStatus.REVOKED: Blocker.RIGHTS_REVOKED,
    RightsStatus.EXPIRED: Blocker.RIGHTS_EXPIRED,
}


def gate_blockers(facts: GateFacts) -> list[Blocker]:
    """Every rights reason this work may not get a notice draft, in enum order. Pure."""
    found: set[Blocker] = set()
    if not facts.owned:
        found.add(Blocker.WORK_NOT_OWNED)
    if not facts.claimant_type:
        found.add(Blocker.CLAIMANT_TYPE_MISSING)
    if facts.identity_status != IdentityStatus.VERIFIED:
        found.add(Blocker.IDENTITY_NOT_VERIFIED)
    if facts.status is None:
        found.add(Blocker.RIGHTS_RECORD_MISSING)
    elif facts.status in _STATUS_BLOCKERS:
        found.add(_STATUS_BLOCKERS[facts.status])
    # A declaration from the upload tick names no capacity or right; staff need both.
    if facts.status not in (None, RightsStatus.UNVERIFIED) and not (
        facts.capacity and facts.asserted_right
    ):
        found.add(Blocker.DECLARATION_INCOMPLETE)
    if facts.competing_claims:
        found.add(Blocker.COMPETING_CLAIM)
    if facts.capacity == Capacity.AUTHORISED_AGENT:
        if not facts.underlying_owner_name.strip():
            found.add(Blocker.UNDERLYING_OWNER_MISSING)
        mandate = facts.mandate
        if mandate is None:
            found.add(Blocker.MANDATE_MISSING)
        else:
            if mandate.revoked:
                found.add(Blocker.MANDATE_REVOKED)
            if facts.today < mandate.valid_from:
                found.add(Blocker.MANDATE_NOT_YET_VALID)
            if mandate.valid_until is not None and facts.today > mandate.valid_until:
                found.add(Blocker.MANDATE_EXPIRED)
            if not mandate.authority_to_prepare_notices:
                found.add(Blocker.MANDATE_NOTICE_CLAUSE_NOT_RECORDED)
    if facts.schedule_valid_until is not None and facts.today > facts.schedule_valid_until:
        found.add(Blocker.SCHEDULE_EXPIRED)
    if facts.partner_count == 0 and not facts.partners_none_affirmed:
        found.add(Blocker.PARTNERS_NOT_AFFIRMED)
    return [blocker for blocker in Blocker if blocker in found]


def mandate_facts(mandate: AgencyMandate | None) -> MandateFacts | None:
    if mandate is None:
        return None
    return MandateFacts(
        valid_from=mandate.valid_from,
        valid_until=mandate.valid_until,
        revoked=mandate.revoked_at is not None,
        authority_to_prepare_notices=mandate.authority_to_prepare_notices,
    )


def organisation_facts(
    organisation: Organisation, digests: Iterable[str], *, today: date | None = None
) -> dict[str, GateFacts]:
    """Gate snapshots for several works of one organisation, in a fixed number of queries.

    Read-only. `digests` are file sha256s; each gets its own `GateFacts`.
    """
    wanted = set(digests)
    claimant = RightsClaimant.objects.filter(organisation=organisation).first()
    records = {
        record.sha256: record
        for record in WorkRights.objects.filter(organisation=organisation, sha256__in=wanted)
        .exclude(status=RightsStatus.SUPERSEDED)
        .select_related("mandate", "schedule")
    }
    competing = dict(
        WorkRights.objects.filter(sha256__in=wanted)
        .exclude(organisation=organisation)
        .exclude(status__in=CLOSED_STATUSES)
        .values("sha256")
        .annotate(claims=Count("pk"))
        .values_list("sha256", "claims")
    )
    owned = set(
        OriginalOwnership.objects.filter(
            organisation=organisation, reference__sha256__in=wanted
        ).values_list("reference__sha256", flat=True)
    )
    partner_count = LicensedPartner.objects.filter(organisation=organisation).count()
    on = today or timezone.localdate()
    facts = {}
    for sha256 in wanted:
        record = records.get(sha256)
        facts[sha256] = GateFacts(
            today=on,
            owned=sha256 in owned,
            claimant_type=claimant.claimant_type if claimant else "",
            identity_status=claimant.identity_status if claimant else IdentityStatus.UNVERIFIED,
            status=record.status if record else None,
            capacity=record.capacity if record else "",
            asserted_right=record.asserted_right if record else "",
            underlying_owner_name=record.underlying_owner_name if record else "",
            mandate=mandate_facts(record.mandate) if record else None,
            schedule_valid_until=(
                record.schedule.valid_until if record and record.schedule is not None else None
            ),
            competing_claims=competing.get(sha256, 0),
            partner_count=partner_count,
            partners_none_affirmed=bool(claimant and claimant.partners_none_affirmed_at),
        )
    return facts


def gate_facts(work: Work, *, today: date | None = None) -> GateFacts:
    """The snapshot `gate_blockers` evaluates for one work, read without writing."""
    return organisation_facts(work.organisation, [work.sha256], today=today)[work.sha256]


def notice_gate_blockers(work: Work, *, today: date | None = None) -> list[str]:
    """Blocker codes for this work; empty when the rights side allows a notice draft."""
    return [str(blocker) for blocker in gate_blockers(gate_facts(work, today=today))]


def organisation_blockers(
    organisation: Organisation, digests: Iterable[str], *, today: date | None = None
) -> dict[str, list[str]]:
    """`notice_gate_blockers` for several works of one organisation, keyed by sha256."""
    return {
        sha256: [str(blocker) for blocker in gate_blockers(facts)]
        for sha256, facts in organisation_facts(organisation, digests, today=today).items()
    }
