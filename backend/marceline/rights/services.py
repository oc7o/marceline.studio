"""The only writer of the rights tables (ADR 0034).

**Status changes are explicit.** `transition` is the one function that changes
`WorkRights.status`, and every call writes a `WorkRightsTransition` (who, when, from → to, code,
reason). Who may do what:

| from | to | by |
|---|---|---|
| unverified | self_declared | customer (or staff on their behalf) |
| self_declared | self_declared | customer re-declaring (logged like any change) |
| self_declared | unverified | customer withdrawing (reason, text version logged) |
| unverified, self_declared, documents_complete, authority_declared | documents_received | staff |
| documents_received | documents_complete | staff |
| documents_complete | authority_declared | **customer only** |
| authority_declared | documents_complete | **customer only** (withdrawing it) |
| any open status | disputed | staff, system (competing claim) |
| any open status, disputed | revoked, expired | staff, system (mandate / schedule) |
| disputed | unverified, documents_received | staff (dispute resolved) |
| revoked, expired | unverified | staff (reopened, e.g. after a new mandate) |
| anything but superseded | superseded | staff (`supersede` creates the successor) |

Nothing reaches `documents_complete` without passing `documents_received`, and nothing a customer
does goes beyond `self_declared` until the documents are complete.

**The two halves are deliberately in different hands** (ADR 0034 amendment 2026-09-18):

- **`documents_complete` is ours and it is formal** — parties, work, dates and a signature present,
  the period still running. It never means staff read the documents and concluded they legally
  support the claimed right or authority. It was called `verified` until 2026-09-18.
- **`authority_declared` is the customer's and it is the affirmative statement** — that those
  documents give the organisation the right or authority it declared. `declare_authority` accepts
  `ActorKind.CUSTOMER` only: not staff "on the customer's behalf", because the statement has to
  come from the customer themselves. A customer who cannot use
  the screen writes to the operator, and nothing is recorded until they can.

Leaving `authority_declared` clears the declaration fields (`transition`), so a reopened record
never shows a statement the customer has not made about the documents now on file.

**Competing claims.** Two organisations with a current (not closed) record for one sha256 are a
conflict, whoever registered first: every such record moves to `disputed` (`flag_competing_claims`)
and the gate reports `competing_claim` for as long as both stand.
"""

import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from accounts.models import Organisation
from marceline.rights import vault
from marceline.rights.declarations import (
    CURRENT_AUTHORITY_DECLARATION,
    CURRENT_AUTHORITY_WITHDRAWAL,
    CURRENT_PARTNER_DECLARATION,
    CURRENT_WITHDRAWAL,
    CURRENT_WORK_DECLARATION,
    LANGUAGES,
    WITHDRAWAL_REASONS,
)
from marceline.rights.models import (
    CLOSED_STATUSES,
    ActorKind,
    AgencyMandate,
    AssertedRight,
    Capacity,
    ClaimantType,
    IdentityMethod,
    IdentityStatus,
    LicensedPartner,
    PartnerKind,
    ProofDocument,
    ProofKind,
    RightsClaimant,
    RightsEvent,
    RightsEventKind,
    RightsSchedule,
    RightsStatus,
    WorkRights,
    WorkRightsTransition,
)
from marceline.rights.partners import normalise_domain, normalise_path_prefix

S = RightsStatus
OPEN_STATUSES = frozenset(
    {
        S.UNVERIFIED,
        S.SELF_DECLARED,
        S.DOCUMENTS_RECEIVED,
        S.DOCUMENTS_COMPLETE,
        S.AUTHORITY_DECLARED,
    }
)
#: Fields holding the customer's authority declaration; cleared when a record leaves that status.
AUTHORITY_FIELDS: dict[str, object] = {
    "authority_declaration_version": "",
    "authority_declaration_language": "",
    "authority_declaration_text": "",
    "authority_declared_at": None,
    "authority_declared_by": None,
}


class RightsError(Exception):
    """A refused rights action, with a machine-readable code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Actor:
    kind: str
    user: User | None = None

    @classmethod
    def staff(cls, user: User | None) -> "Actor":
        return cls(ActorKind.STAFF, user)

    @classmethod
    def customer(cls, user: User) -> "Actor":
        return cls(ActorKind.CUSTOMER, user)

    @property
    def user_id(self) -> int | None:
        return self.user.pk if self.user is not None else None

    @property
    def label(self) -> str:
        return self.user.get_username() if self.user is not None else ""

    @property
    def account(self) -> User | None:
        return self.user if self.user is not None and self.user.pk is not None else None


SYSTEM = Actor(ActorKind.SYSTEM)

#: (from, to) → actor kinds allowed to make that change.
_STAFF_SYSTEM = frozenset({ActorKind.STAFF, ActorKind.SYSTEM})
ALLOWED: dict[tuple[str, str], frozenset[str]] = {
    (S.UNVERIFIED, S.SELF_DECLARED): frozenset({ActorKind.CUSTOMER, ActorKind.STAFF}),
    (S.SELF_DECLARED, S.SELF_DECLARED): frozenset({ActorKind.CUSTOMER, ActorKind.STAFF}),
    # A customer may take back their own statement while nobody has checked documents for it.
    (S.SELF_DECLARED, S.UNVERIFIED): frozenset({ActorKind.CUSTOMER, ActorKind.STAFF}),
    (S.UNVERIFIED, S.DOCUMENTS_RECEIVED): frozenset({ActorKind.STAFF}),
    (S.SELF_DECLARED, S.DOCUMENTS_RECEIVED): frozenset({ActorKind.STAFF}),
    (S.DOCUMENTS_COMPLETE, S.DOCUMENTS_RECEIVED): frozenset({ActorKind.STAFF}),
    (S.AUTHORITY_DECLARED, S.DOCUMENTS_RECEIVED): frozenset({ActorKind.STAFF}),
    (S.DOCUMENTS_RECEIVED, S.DOCUMENTS_COMPLETE): frozenset({ActorKind.STAFF}),
    # The affirmative statement about the documents is the customer's alone (see the module
    # docstring): no staff, no system actor, in either direction.
    (S.DOCUMENTS_COMPLETE, S.AUTHORITY_DECLARED): frozenset({ActorKind.CUSTOMER}),
    (S.AUTHORITY_DECLARED, S.DOCUMENTS_COMPLETE): frozenset({ActorKind.CUSTOMER}),
    **{(status, S.DISPUTED): _STAFF_SYSTEM for status in OPEN_STATUSES},
    **{
        (status, target): _STAFF_SYSTEM
        for status in (*OPEN_STATUSES, S.DISPUTED)
        for target in (S.REVOKED, S.EXPIRED)
    },
    (S.DISPUTED, S.UNVERIFIED): frozenset({ActorKind.STAFF}),
    (S.DISPUTED, S.DOCUMENTS_RECEIVED): frozenset({ActorKind.STAFF}),
    (S.REVOKED, S.UNVERIFIED): frozenset({ActorKind.STAFF}),
    (S.EXPIRED, S.UNVERIFIED): frozenset({ActorKind.STAFF}),
    **{
        (status, S.SUPERSEDED): frozenset({ActorKind.STAFF})
        for status in (*OPEN_STATUSES, S.DISPUTED, S.REVOKED, S.EXPIRED)
    },
}


def allowed_targets(status: str, actor_kind: str) -> list[str]:
    """Where a record in `status` may go when `actor_kind` acts, in status order."""
    return [
        target for target in S.values if actor_kind in ALLOWED.get((status, target), frozenset())
    ]


def _log_event(
    organisation: Organisation,
    kind: str,
    actor: Actor,
    *,
    proof: ProofDocument | None = None,
    mandate: AgencyMandate | None = None,
    schedule: RightsSchedule | None = None,
    detail: dict[str, object] | None = None,
) -> RightsEvent:
    return RightsEvent.objects.create(
        organisation=organisation,
        kind=kind,
        proof=proof,
        mandate=mandate,
        schedule=schedule,
        actor_kind=actor.kind,
        actor_id=actor.user_id,
        actor_label=actor.label,
        detail=detail or {},
    )


# --- claimant ------------------------------------------------------------------------------------


def claimant_for(organisation: Organisation) -> RightsClaimant:
    claimant, _ = RightsClaimant.objects.get_or_create(organisation=organisation)
    return claimant


@transaction.atomic
def set_claimant_type(
    organisation: Organisation, claimant_type: str, actor: Actor
) -> RightsClaimant:
    """Choose the claimant type. Locked once a work has documents or is verified: those were
    checked under the old type, so changing it is a staff matter (supersede the records first)."""
    if claimant_type not in ClaimantType.values:
        raise RightsError("unknown_claimant_type")
    claimant = claimant_for(organisation)
    claimant = RightsClaimant.objects.select_for_update().get(pk=claimant.pk)
    if claimant.claimant_type == claimant_type:
        return claimant
    checked = current_works(organisation).filter(
        status__in=(S.DOCUMENTS_RECEIVED, S.DOCUMENTS_COMPLETE, S.AUTHORITY_DECLARED)
    )
    if claimant.claimant_type and actor.kind == ActorKind.CUSTOMER and checked.exists():
        raise RightsError("claimant_type_locked")
    before = claimant.claimant_type
    claimant.claimant_type = claimant_type
    claimant.claimant_type_set_at = timezone.now()
    claimant.save(update_fields=["claimant_type", "claimant_type_set_at", "updated_at"])
    _log_event(
        organisation,
        RightsEventKind.CLAIMANT_TYPE_SET,
        actor,
        detail={"from": before, "to": claimant_type},
    )
    return claimant


@transaction.atomic
def verify_identity(
    organisation: Organisation, *, method: str, evidence_note: str, actor: Actor
) -> RightsClaimant:
    """Staff record that they checked who stands behind the organisation, and how."""
    if actor.kind != ActorKind.STAFF:
        raise RightsError("staff_only")
    if method not in IdentityMethod.values:
        raise RightsError("unknown_identity_method")
    if not evidence_note.strip():
        raise RightsError("evidence_note_required")
    claimant = RightsClaimant.objects.select_for_update().get(pk=claimant_for(organisation).pk)
    claimant.identity_status = IdentityStatus.VERIFIED
    claimant.identity_method = method
    claimant.identity_evidence_note = evidence_note.strip()
    claimant.identity_verified_at = timezone.now()
    claimant.identity_verified_by = actor.account
    claimant.save()
    _log_event(organisation, RightsEventKind.IDENTITY_VERIFIED, actor, detail={"method": method})
    return claimant


@transaction.atomic
def revoke_identity(organisation: Organisation, *, reason: str, actor: Actor) -> RightsClaimant:
    if actor.kind != ActorKind.STAFF:
        raise RightsError("staff_only")
    claimant = RightsClaimant.objects.select_for_update().get(pk=claimant_for(organisation).pk)
    claimant.identity_status = IdentityStatus.REVOKED
    claimant.save(update_fields=["identity_status", "updated_at"])
    _log_event(
        organisation, RightsEventKind.IDENTITY_REVOKED, actor, detail={"reason": reason.strip()}
    )
    return claimant


@transaction.atomic
def issue_account_control_token(organisation: Organisation, actor: Actor) -> RightsClaimant:
    """A fresh random token for the customer to place in a profile they control."""
    claimant = RightsClaimant.objects.select_for_update().get(pk=claimant_for(organisation).pk)
    claimant.account_control_token = f"marceline-{secrets.token_hex(12)}"
    claimant.account_control_token_issued_at = timezone.now()
    claimant.save(
        update_fields=["account_control_token", "account_control_token_issued_at", "updated_at"]
    )
    _log_event(organisation, RightsEventKind.TOKEN_ISSUED, actor)
    return claimant


# --- work records --------------------------------------------------------------------------------


def current_works(organisation: Organisation) -> QuerySet[WorkRights]:
    return WorkRights.objects.filter(organisation=organisation).exclude(status=S.SUPERSEDED)


def current_work(organisation: Organisation, sha256: str) -> WorkRights | None:
    return current_works(organisation).filter(sha256=sha256).first()


def competing_records(work: WorkRights) -> QuerySet[WorkRights]:
    """Current records of *other* organisations for the same file that are not closed."""
    return (
        WorkRights.objects.filter(sha256=work.sha256)
        .exclude(organisation_id=work.organisation_id)
        .exclude(status__in=CLOSED_STATUSES)
    )


def transition(
    work: WorkRights,
    to_status: str,
    actor: Actor,
    *,
    reason: str = "",
    code: str = "",
) -> WorkRights:
    """Move one record to `to_status` if the table allows it for this actor; log it."""
    with transaction.atomic():
        locked = WorkRights.objects.select_for_update().get(pk=work.pk)
        from_status = locked.status
        allowed = ALLOWED.get((from_status, to_status), frozenset())
        if actor.kind not in allowed:
            raise RightsError(
                "transition_not_allowed",
                f"{from_status} → {to_status} is not allowed for {actor.kind}",
            )
        if actor.kind == ActorKind.STAFF and not reason.strip() and not code:
            raise RightsError("reason_required")
        now = timezone.now()
        fields = ["status", "status_changed_at"]
        locked.status = to_status
        locked.status_changed_at = now
        if from_status == S.AUTHORITY_DECLARED and to_status != S.AUTHORITY_DECLARED:
            # The customer stated something about the documents as they stood; once the record
            # moves on, that statement is no longer current. The log keeps it.
            for field, value in AUTHORITY_FIELDS.items():
                setattr(locked, field, value)
            fields += list(AUTHORITY_FIELDS)
        locked.save(update_fields=fields)
        WorkRightsTransition.objects.create(
            work=locked,
            from_status=from_status,
            to_status=to_status,
            actor_kind=actor.kind,
            actor_id=actor.user_id,
            actor_label=actor.label,
            code=code,
            reason=reason.strip(),
            at=now,
        )
    work.refresh_from_db()
    return work


@transaction.atomic
def open_record(organisation: Organisation, sha256: str, actor: Actor) -> WorkRights:
    """The current record of this work, created `unverified` (and logged) when there is none."""
    existing = current_work(organisation, sha256)
    if existing is not None:
        return existing
    work = WorkRights.objects.create(organisation=organisation, sha256=sha256)
    WorkRightsTransition.objects.create(
        work=work,
        from_status="",
        to_status=S.UNVERIFIED,
        actor_kind=actor.kind,
        actor_id=actor.user_id,
        actor_label=actor.label,
        code="opened",
    )
    return work


@dataclass(frozen=True)
class DeclarationInput:
    capacity: str
    asserted_right: str
    underlying_owner_name: str = ""
    asserted_right_detail: str = ""
    declaration_version: str = ""
    language: str = "en"
    confirmed: bool = False


def self_declare(
    organisation: Organisation, sha256: str, declaration: DeclarationInput, actor: Actor
) -> WorkRights:
    """Record the customer's self-declaration for one work (→ `self_declared`)."""
    if not declaration.confirmed:
        raise RightsError("declaration_not_confirmed")
    if declaration.declaration_version != CURRENT_WORK_DECLARATION.version:
        raise RightsError("declaration_version_outdated")
    if declaration.language not in LANGUAGES:
        raise RightsError("unknown_language")
    if declaration.capacity not in Capacity.values:
        raise RightsError("unknown_capacity")
    if declaration.asserted_right not in AssertedRight.values:
        raise RightsError("unknown_asserted_right")
    owner_name = declaration.underlying_owner_name.strip()
    if declaration.capacity == Capacity.AUTHORISED_AGENT and not owner_name:
        raise RightsError("underlying_owner_required")
    detail = declaration.asserted_right_detail.strip()
    if declaration.asserted_right == AssertedRight.OTHER and not detail:
        raise RightsError("asserted_right_detail_required")
    with transaction.atomic():
        work = open_record(organisation, sha256, actor)
        if work.status not in (S.UNVERIFIED, S.SELF_DECLARED):
            raise RightsError("declaration_not_open", f"the record is {work.status}")
        WorkRights.objects.filter(pk=work.pk).update(
            capacity=declaration.capacity,
            underlying_owner_name=owner_name if declaration.capacity != Capacity.OWNER else "",
            asserted_right=declaration.asserted_right,
            asserted_right_detail=detail,
            declaration_version=CURRENT_WORK_DECLARATION.version,
            declaration_language=declaration.language,
            declaration_text=CURRENT_WORK_DECLARATION.text(declaration.language),
            declared_at=timezone.now(),
            declared_by=actor.account,
        )
        work = transition(
            work,
            S.SELF_DECLARED,
            actor,
            code="declared" if work.status == S.UNVERIFIED else "redeclared",
            reason=f"declaration {CURRENT_WORK_DECLARATION.version} ({declaration.language})",
        )
        flag_competing_claims(work.sha256)
    work.refresh_from_db()
    return work


#: Upload-tick declarations are stored under this prefix plus the tick's own text version, so a
#: record says which wording it came from (`marceline/app/rights_confirmation.json`).
UPLOAD_DECLARATION_PREFIX = "upload-"


def declare_at_upload(
    organisation: Organisation,
    sha256: str,
    *,
    text_version: str,
    language: str,
    text: str,
    actor: Actor,
) -> WorkRights:
    """The rights tick on the Add original page (#29) as a self-declaration of this work.

    An unverified record becomes `self_declared` and a self-declared one is re-declared, both logged
    with code `declared_at_upload`. The tick names no capacity and no asserted right, so those stay
    as they were (empty on a new record: the gate reports `declaration_incomplete` until the
    customer completes the declaration on the rights screen). A record staff already work on
    (documents received, verified, disputed, …) keeps its status: an upload never downgrades or
    reopens it. Returns the current record either way.
    """
    if language not in LANGUAGES:
        raise RightsError("unknown_language")
    with transaction.atomic():
        work = open_record(organisation, sha256, actor)
        if work.status not in (S.UNVERIFIED, S.SELF_DECLARED):
            return work
        version = f"{UPLOAD_DECLARATION_PREFIX}{text_version}"[:32]
        WorkRights.objects.filter(pk=work.pk).update(
            declaration_version=version,
            declaration_language=language,
            declaration_text=text,
            declared_at=timezone.now(),
            declared_by=actor.account,
        )
        work = transition(
            work,
            S.SELF_DECLARED,
            actor,
            code="declared_at_upload",
            reason=f"rights tick {text_version} ({language}) when adding the original",
        )
        flag_competing_claims(work.sha256)
    work.refresh_from_db()
    return work


@dataclass(frozen=True)
class WithdrawalInput:
    reason: str
    declaration_version: str
    language: str
    confirmed: bool
    note: str = ""


def withdraw_declaration(
    organisation: Organisation, sha256: str, withdrawal: WithdrawalInput, actor: Actor
) -> WorkRights:
    """Take back a self-declaration (`self_declared → unverified`), with a reason and the
    confirmed withdrawal text version logged. Not possible once staff received documents or
    verified the record: from there only staff change it."""
    if not withdrawal.confirmed:
        raise RightsError("declaration_not_confirmed")
    if withdrawal.declaration_version != CURRENT_WITHDRAWAL.version:
        raise RightsError("declaration_version_outdated")
    if withdrawal.language not in LANGUAGES:
        raise RightsError("unknown_language")
    if withdrawal.reason not in WITHDRAWAL_REASONS:
        raise RightsError("unknown_withdrawal_reason")
    note = withdrawal.note.strip()[:500]
    with transaction.atomic():
        work = current_work(organisation, sha256)
        if work is None or work.status != S.SELF_DECLARED:
            raise RightsError("declaration_not_withdrawable")
        work = transition(
            work,
            S.UNVERIFIED,
            actor,
            code="withdrawn",
            reason=f"withdrawal {CURRENT_WITHDRAWAL.version} ({withdrawal.language}): "
            f"{withdrawal.reason}" + (f": {note}" if note else ""),
        )
        # The record no longer carries a declaration; the log keeps what was declared and when.
        WorkRights.objects.filter(pk=work.pk).update(
            declaration_version="",
            declaration_language="",
            declaration_text="",
            declared_at=None,
            declared_by=None,
        )
    work.refresh_from_db()
    return work


@dataclass(frozen=True)
class AuthorityInput:
    declaration_version: str
    language: str
    confirmed: bool


def declare_authority(
    organisation: Organisation, sha256: str, authority: AuthorityInput, actor: Actor
) -> WorkRights:
    """The customer states that the documents on file give them the right or authority they
    declared (`documents_complete → authority_declared`, ADR 0034 amendment 2026-09-18).

    This is the one statement nobody here may make for them: staff record only that the documents
    are formally complete, and the customer supplies the legal proposition that they support the
    claim. `ALLOWED` admits `ActorKind.CUSTOMER` only, so a staff session cannot write it at all.
    The exact text shown is stored with its version and language, like every other declaration.
    """
    if not authority.confirmed:
        raise RightsError("declaration_not_confirmed")
    if authority.declaration_version != CURRENT_AUTHORITY_DECLARATION.version:
        raise RightsError("declaration_version_outdated")
    if authority.language not in LANGUAGES:
        raise RightsError("unknown_language")
    with transaction.atomic():
        work = current_work(organisation, sha256)
        if work is None or work.status != S.DOCUMENTS_COMPLETE:
            raise RightsError("authority_not_declarable")
        WorkRights.objects.filter(pk=work.pk).update(
            authority_declaration_version=CURRENT_AUTHORITY_DECLARATION.version,
            authority_declaration_language=authority.language,
            authority_declaration_text=CURRENT_AUTHORITY_DECLARATION.text(authority.language),
            authority_declared_at=timezone.now(),
            authority_declared_by=actor.account,
        )
        work = transition(
            work,
            S.AUTHORITY_DECLARED,
            actor,
            code="authority_declared",
            reason=f"authority declaration {CURRENT_AUTHORITY_DECLARATION.version} "
            f"({authority.language})",
        )
    work.refresh_from_db()
    return work


def withdraw_authority(
    organisation: Organisation, sha256: str, withdrawal: WithdrawalInput, actor: Actor
) -> WorkRights:
    """Take the authority declaration back (`authority_declared → documents_complete`).

    A statement the customer can no longer stand behind must be theirs to end — the
    declaration says "I will promptly correct or withdraw". The documents stay on file and the
    record keeps its formal check; only the customer's statement about them goes, and the log
    keeps what it was.
    """
    if not withdrawal.confirmed:
        raise RightsError("declaration_not_confirmed")
    if withdrawal.declaration_version != CURRENT_AUTHORITY_WITHDRAWAL.version:
        raise RightsError("declaration_version_outdated")
    if withdrawal.language not in LANGUAGES:
        raise RightsError("unknown_language")
    if withdrawal.reason not in WITHDRAWAL_REASONS:
        raise RightsError("unknown_withdrawal_reason")
    note = withdrawal.note.strip()[:500]
    with transaction.atomic():
        work = current_work(organisation, sha256)
        if work is None or work.status != S.AUTHORITY_DECLARED:
            raise RightsError("authority_not_withdrawable")
        work = transition(
            work,
            S.DOCUMENTS_COMPLETE,
            actor,
            code="authority_withdrawn",
            reason=f"authority withdrawal {CURRENT_AUTHORITY_WITHDRAWAL.version} "
            f"({withdrawal.language}): {withdrawal.reason}" + (f": {note}" if note else ""),
        )
    work.refresh_from_db()
    return work


def flag_competing_claims(sha256: str) -> list[WorkRights]:
    """Put every current, non-closed record of this file in `disputed` when more than one
    organisation holds one. Returns the records moved."""
    records = list(
        WorkRights.objects.filter(sha256=sha256).exclude(status__in=CLOSED_STATUSES).order_by("pk")
    )
    if len({record.organisation_id for record in records}) < 2:
        return []
    moved = []
    for record in records:
        if record.status == S.DISPUTED:
            continue
        others = sorted(
            {
                str(other.organisation_id)
                for other in records
                if other.organisation_id != record.organisation_id
            }
        )
        moved.append(
            transition(
                record,
                S.DISPUTED,
                SYSTEM,
                code="competing_claim",
                reason="another organisation claims the same work "
                f"(organisation {', '.join(others)})",
            )
        )
    return moved


@transaction.atomic
def record_competing_claim(
    organisation: Organisation, sha256: str, *, reason: str, actor: Actor
) -> WorkRights:
    """Staff record a claim by another organisation (e.g. a studio writes in about a creator's
    upload). Opens that organisation's record and disputes every current record of the file."""
    if actor.kind != ActorKind.STAFF:
        raise RightsError("staff_only")
    if not reason.strip():
        raise RightsError("reason_required")
    work = open_record(organisation, sha256, actor)
    if work.status not in (S.DISPUTED, *CLOSED_STATUSES):
        transition(work, S.DISPUTED, actor, reason=reason, code="competing_claim")
    flag_competing_claims(sha256)
    work.refresh_from_db()
    return work


@transaction.atomic
def supersede(work: WorkRights, *, reason: str, actor: Actor) -> WorkRights:
    """Close this record as `superseded` and open a fresh `unverified` one for the same work."""
    transition(work, S.SUPERSEDED, actor, reason=reason, code="superseded")
    return open_record(work.organisation, work.sha256, actor)


# --- schedules and mandates ----------------------------------------------------------------------


@transaction.atomic
def verify_schedule(schedule: RightsSchedule, *, reason: str, actor: Actor) -> list[WorkRights]:
    """One document for many works: every linked work that has its documents passes the formal
    check, and every linked work still undocumented becomes `documents_received` first.

    `documents_complete` is the formal-completeness state (ADR 0034 amendment 2026-09-18), here
    recorded for a whole schedule at once; it says the documents are complete, never what they
    mean — and it does not state the customer's authority, which only they can declare."""
    moved = []
    for work in schedule.works.exclude(status=S.SUPERSEDED).order_by("pk"):
        if work.status in (S.UNVERIFIED, S.SELF_DECLARED):
            work = transition(
                work, S.DOCUMENTS_RECEIVED, actor, reason=reason, code="schedule_documents"
            )
        if work.status == S.DOCUMENTS_RECEIVED:
            moved.append(
                transition(work, S.DOCUMENTS_COMPLETE, actor, reason=reason, code="schedule_formal")
            )
    return moved


def _close_linked(
    works: Iterable[WorkRights], to_status: str, actor: Actor, *, code: str, reason: str
) -> list[WorkRights]:
    moved = []
    for work in works:
        if work.status in CLOSED_STATUSES:
            continue
        moved.append(transition(work, to_status, actor, code=code, reason=reason))
    return moved


@transaction.atomic
def revoke_mandate(
    mandate: AgencyMandate, *, reason: str, actor: Actor, at: datetime | None = None
) -> list[WorkRights]:
    """Revoke a mandate; every work acting under it becomes `revoked`."""
    if actor.kind != ActorKind.STAFF:
        raise RightsError("staff_only")
    if not reason.strip():
        raise RightsError("reason_required")
    locked = AgencyMandate.objects.select_for_update().get(pk=mandate.pk)
    if locked.revoked_at is not None:
        raise RightsError("mandate_already_revoked")
    locked.revoked_at = at or timezone.now()
    locked.revoked_reason = reason.strip()
    locked.save(update_fields=["revoked_at", "revoked_reason"])
    _log_event(locked.organisation, RightsEventKind.MANDATE_REVOKED, actor, mandate=locked)
    mandate.refresh_from_db()
    return _close_linked(
        locked.works.order_by("pk"),
        S.REVOKED,
        actor,
        code="mandate_revoked",
        reason=f"mandate {locked.pk} revoked: {reason.strip()}",
    )


@dataclass(frozen=True)
class ExpiryResult:
    mandates: list[AgencyMandate]
    schedules: list[RightsSchedule]
    works: list[WorkRights]


def expire_due(today: date, *, dry_run: bool = False) -> ExpiryResult:
    """Mandates and schedules whose end date lies before `today`: their open works → `expired`.

    Idempotent: a mandate or schedule with nothing left to expire is not reported again.
    """
    mandates = [
        mandate
        for mandate in AgencyMandate.objects.filter(
            valid_until__lt=today, revoked_at__isnull=True
        ).order_by("pk")
        if mandate.works.exclude(status__in=CLOSED_STATUSES).exists()
    ]
    schedules = [
        schedule
        for schedule in RightsSchedule.objects.filter(valid_until__lt=today).order_by("pk")
        if schedule.works.exclude(status__in=CLOSED_STATUSES).exists()
    ]
    if dry_run:
        works = list(
            WorkRights.objects.filter(Q(mandate__in=mandates) | Q(schedule__in=schedules))
            .exclude(status__in=CLOSED_STATUSES)
            .order_by("pk")
        )
        return ExpiryResult(mandates, schedules, works)
    works = []
    with transaction.atomic():
        for mandate in mandates:
            _log_event(
                mandate.organisation,
                RightsEventKind.MANDATE_EXPIRED,
                SYSTEM,
                mandate=mandate,
                detail={
                    "valid_until": mandate.valid_until.isoformat() if mandate.valid_until else ""
                },
            )
            works += _close_linked(
                mandate.works.order_by("pk"),
                S.EXPIRED,
                SYSTEM,
                code="mandate_expired",
                reason=f"mandate {mandate.pk} ended on {mandate.valid_until}",
            )
        for schedule in schedules:
            _log_event(
                schedule.organisation, RightsEventKind.SCHEDULE_EXPIRED, SYSTEM, schedule=schedule
            )
            works += _close_linked(
                schedule.works.order_by("pk"),
                S.EXPIRED,
                SYSTEM,
                code="schedule_expired",
                reason=f"schedule {schedule.pk} ended on {schedule.valid_until}",
            )
    return ExpiryResult(mandates, schedules, works)


# --- licensed partners ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PartnerInput:
    kind: str
    name: str
    domain: str = ""
    path_prefix: str = ""
    note: str = ""


@transaction.atomic
def add_partner(organisation: Organisation, partner: PartnerInput, actor: Actor) -> LicensedPartner:
    """Add a licensed partner; clears a "no partners" affirmation, which is no longer true."""
    if partner.kind not in PartnerKind.values:
        raise RightsError("unknown_partner_kind")
    name = partner.name.strip()
    if not name:
        raise RightsError("partner_name_required")
    domain = normalise_domain(partner.domain) if partner.domain.strip() else ""
    if partner.kind in (PartnerKind.DOMAIN, PartnerKind.PLATFORM) and not domain:
        raise RightsError("partner_domain_required")
    prefix = normalise_path_prefix(partner.path_prefix)
    if prefix and not domain:
        raise RightsError("partner_domain_required")
    row = LicensedPartner.objects.create(
        organisation=organisation,
        kind=partner.kind,
        name=name,
        domain=domain,
        path_prefix=prefix,
        note=partner.note.strip(),
        created_by=actor.account,
    )
    RightsClaimant.objects.filter(organisation=organisation).update(
        partners_none_affirmed_at=None, partners_none_affirmed_version="", updated_at=timezone.now()
    )
    _log_event(
        organisation,
        RightsEventKind.PARTNER_ADDED,
        actor,
        detail={"partner": row.pk, "kind": row.kind, "domain": row.domain},
    )
    return row


@transaction.atomic
def remove_partner(partner: LicensedPartner, actor: Actor) -> None:
    organisation = partner.organisation
    detail = {"partner": partner.pk, "kind": partner.kind, "domain": partner.domain}
    partner.delete()
    _log_event(organisation, RightsEventKind.PARTNER_REMOVED, actor, detail=detail)


@transaction.atomic
def affirm_no_partners(
    organisation: Organisation, *, version: str, language: str, confirmed: bool, actor: Actor
) -> RightsClaimant:
    """The customer's explicit "we have no licensed partners", with its timestamp and version."""
    if not confirmed:
        raise RightsError("declaration_not_confirmed")
    if version != CURRENT_PARTNER_DECLARATION.version:
        raise RightsError("declaration_version_outdated")
    if language not in LANGUAGES:
        raise RightsError("unknown_language")
    if LicensedPartner.objects.filter(organisation=organisation).exists():
        raise RightsError("partners_exist")
    claimant = RightsClaimant.objects.select_for_update().get(pk=claimant_for(organisation).pk)
    claimant.partners_none_affirmed_at = timezone.now()
    claimant.partners_none_affirmed_version = version
    claimant.save(
        update_fields=["partners_none_affirmed_at", "partners_none_affirmed_version", "updated_at"]
    )
    _log_event(
        organisation,
        RightsEventKind.PARTNERS_NONE_AFFIRMED,
        actor,
        detail={"version": version, "language": language},
    )
    return claimant


# --- proofs --------------------------------------------------------------------------------------


def safe_filename(name: str) -> str:
    """The uploaded name reduced to something harmless to show and to put in a header."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in base).strip(" .")
    return (cleaned or "document")[:200]


@transaction.atomic
def upload_proof(
    organisation: Organisation,
    data: bytes,
    *,
    filename: str,
    kind: str,
    actor: Actor,
    work: WorkRights | None = None,
    schedule: RightsSchedule | None = None,
    mandate: AgencyMandate | None = None,
    note: str = "",
) -> ProofDocument:
    """Encrypt one proof into the vault and record it. Uploading changes no status: staff record
    that the document arrived (`documents_received`)."""
    if kind not in ProofKind.values:
        raise RightsError("unknown_proof_kind")
    for linked in (work, schedule, mandate):
        if linked is not None and linked.organisation_id != organisation.pk:
            raise RightsError("foreign_link")
    blob = vault.store(data)
    try:
        proof = ProofDocument.objects.create(
            organisation=organisation,
            kind=kind,
            work=work,
            schedule=schedule,
            mandate=mandate,
            original_filename=safe_filename(filename),
            content_type=blob.content_type,
            size_bytes=blob.size_bytes,
            sha256=blob.sha256,
            storage_name=blob.storage_name,
            note=note.strip(),
            uploaded_via=actor.kind,
            uploaded_by=actor.account,
        )
        _log_event(
            organisation,
            RightsEventKind.PROOF_UPLOADED,
            actor,
            proof=proof,
            detail={"kind": kind, "size_bytes": blob.size_bytes},
        )
    except BaseException:
        vault.delete(blob.storage_name)
        raise
    return proof


def read_proof(proof: ProofDocument, actor: Actor) -> bytes:
    """Decrypt a proof for staff, and log the access first — also when decrypting fails."""
    if actor.kind != ActorKind.STAFF:
        raise RightsError("staff_only")
    _log_event(proof.organisation, RightsEventKind.PROOF_DOWNLOADED, actor, proof=proof)
    return vault.read(proof.storage_name, proof.sha256)
