"""The customer's rights status (ADR 0034): `/api/v1/app/rights`.

What a customer can do here, and nothing more:
- choose the claimant type (studio / self-producing creator / agency / studio performer /
  collaboration) and request an account-control token for staff to check;
- **self-declare** one work at a time: capacity, right asserted, the exact declaration text
  (served from `rights.declarations`, confirmed with its version and language);
- **declare their authority** for a work whose documents staff found formally complete: the
  statement that those documents give the declared right or authority. Nobody else can make it —
  there is no staff route to `authority_declared`, by design (ADR 0034 amendment 2026-09-18);
- maintain licensed partners, or explicitly affirm there are none;
- upload proof documents (PDF, PNG, JPEG), which go encrypted into the vault. Customers cannot
  download them again; staff read them in the admin, and every read is logged.

Verification, disputes and revocation are staff actions in the admin. Every answer is the whole
overview, in codes: the UI owns the words, apart from the declaration texts, which are shown
verbatim because what was confirmed must be exactly what was stored.

Same rules as the rest of the customer API: scoped by `ownership`, public ids only (ADR 0033:
`orig_`, `partner_`, `proof_`), a malformed, foreign or unknown id is one byte-identical 404, CSRF
on every write (session auth), no media, no confidence.

**Reads and writes reach production** (ADR 0028 amendment "The production write path"). `GET
""` is answered from the signed cache bundle there, like `originals`/`cases` (`marceline.export`,
`marceline.customer_cache`). Every write here except `POST /proofs` goes through the outbox on
production: validated with the same checks as development (session, CSRF, the rate limit,
`validated`/service-level checks that need no local data), stored as a `delivery.OutboxItem` and
answered **202** with `RightsWriteAcceptedOut` — development pulls it and applies it with the same
`marceline.rights.services` call this module's own handlers use
(`marceline/customer_writes.py`). Proof documents stay **development only, deliberately**: the
vault and its encrypted documents must never travel through the outbox, so `/proofs` keeps
`public_surface.ROLE_ONLY_PREFIXES["dev"]` and answers 404 on production.
"""

from datetime import datetime
from typing import Any, Literal, cast

from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils import timezone
from ninja import File, Form, Router, Schema, Status, UploadedFile
from pydantic import Field

from accounts.models import Organisation
from core import public_ids
from delivery import outbox
from delivery.models import OutboxItem
from marceline.app import upload_throttle
from marceline.app.auth import customer_auth
from marceline.app.ownership import organisation_of, owned_original, owned_originals
from marceline.app.schemas import AppErrorOut
from marceline.rights import services, vault
from marceline.rights.declarations import (
    CURRENT_AUTHORITY_DECLARATION,
    CURRENT_AUTHORITY_WITHDRAWAL,
    CURRENT_PARTNER_DECLARATION,
    CURRENT_WITHDRAWAL,
    CURRENT_WORK_DECLARATION,
    WITHDRAWAL_REASONS,
    Declaration,
)
from marceline.rights.gate import Blocker, organisation_blockers
from marceline.rights.models import (
    AssertedRight,
    Capacity,
    ClaimantType,
    IdentityMethod,
    IdentityStatus,
    LicensedPartner,
    PartnerKind,
    ProofDocument,
    ProofKind,
    RightsStatus,
)
from marceline.rights.partners import InvalidDomainError

router = Router(tags=["app"], auth=customer_auth)

# One answer for "does not exist" and "belongs to someone else". Never make them differ.
ORIGINAL_NOT_FOUND = "Original not found"
PARTNER_NOT_FOUND = "Partner not found"

ClaimantTypeCode = Literal[
    "studio_producer", "self_producing_creator", "agency", "studio_performer", "collaboration"
]
IdentityStatusCode = Literal["unverified", "verified", "revoked"]
IdentityMethodCode = Literal[
    "commercial_register",
    "company_email",
    "account_control_token",
    # ADR 0055's fallback for doubt and for studios: staff saw a document on a video call and
    # recorded only that the check happened. No image, number or birth date is stored anywhere.
    "video_call",
    "other",
]
RightsStatusCode = Literal[
    "unverified",
    "self_declared",
    "documents_received",
    "documents_complete",
    "authority_declared",
    "disputed",
    "revoked",
    "expired",
    "superseded",
]
CapacityCode = Literal["owner", "authorised_agent"]
AssertedRightCode = Literal[
    "copyright",
    "film_producer_right",
    "exclusive_licence",
    "performer_right",
    "joint_right",
    "other",
]
PartnerKindCode = Literal["domain", "platform", "distributor", "affiliate"]
ProofKindCode = Literal[
    "contract_excerpt",
    "licence",
    "assignment",
    "agency_mandate",
    "catalogue_schedule",
    "company_register",
    "publication_record",
    "other",
]
BlockerCode = Literal[
    "work_not_owned",
    "claimant_type_missing",
    "identity_not_verified",
    "rights_record_missing",
    "documents_not_complete",
    "authority_not_declared",
    "declaration_incomplete",
    "rights_disputed",
    "rights_revoked",
    "rights_expired",
    "competing_claim",
    "underlying_owner_missing",
    "mandate_missing",
    "mandate_not_yet_valid",
    "mandate_expired",
    "mandate_revoked",
    "mandate_notice_clause_not_recorded",
    "schedule_expired",
    "partners_not_affirmed",
]
LanguageCode = Literal["en", "de"]
WithdrawalReasonCode = Literal[
    "rights_transferred", "authority_ended", "declared_in_error", "other"
]


class RightsClaimantOut(Schema):
    claimant_type: ClaimantTypeCode | None
    identity_status: IdentityStatusCode
    identity_method: IdentityMethodCode | None = Field(
        description="How staff verified identity; null until verified."
    )
    account_control_token: str | None = Field(
        description="Place this in a profile you control so staff can check account control."
    )


class DeclarationTextOut(Schema):
    version: str
    en: str
    de: str


class LicensedPartnerOut(Schema):
    id: str = Field(description="Public id (partner_…).")
    kind: PartnerKindCode
    name: str
    domain: str | None
    path_prefix: str | None
    note: str
    created_at: datetime


class RightsWorkOut(Schema):
    original_id: str = Field(description="The original's public id (orig_…).")
    title: str
    rights_status: RightsStatusCode = Field(description="unverified when nothing was recorded.")
    capacity: CapacityCode | None
    underlying_owner_name: str | None
    asserted_right: AssertedRightCode | None
    asserted_right_detail: str
    declared_at: datetime | None
    declaration_version: str | None
    authority_declared_at: datetime | None = Field(
        description="When the customer declared that the documents give this right or authority."
    )
    authority_declaration_version: str | None
    notice_blockers: list[BlockerCode] = Field(
        description="What a notice draft would still need, in a fixed order. Empty: nothing."
    )


class ProofOut(Schema):
    id: str = Field(description="Public id (proof_…).")
    kind: ProofKindCode
    original_id: str | None
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: datetime


class ProofUploadOut(Schema):
    available: bool
    max_bytes: int
    content_types: list[str]


class RightsOverviewOut(Schema):
    claimant: RightsClaimantOut
    work_declaration: DeclarationTextOut
    partner_declaration: DeclarationTextOut
    withdrawal_declaration: DeclarationTextOut = Field(
        description="Confirmed when a self-declaration is withdrawn (only while self_declared)."
    )
    authority_declaration: DeclarationTextOut = Field(
        description="The customer's own statement that the documents on file give the declared "
        "right or authority (only while documents_complete)."
    )
    authority_withdrawal_declaration: DeclarationTextOut = Field(
        description="Confirmed when that statement is taken back (only while authority_declared)."
    )
    partners: list[LicensedPartnerOut]
    partners_none_affirmed_at: datetime | None
    works: list[RightsWorkOut]
    proofs: list[ProofOut]
    proof_upload: ProofUploadOut


class ClaimantTypeIn(Schema):
    claimant_type: ClaimantTypeCode


class WorkDeclarationIn(Schema):
    capacity: CapacityCode
    asserted_right: AssertedRightCode
    underlying_owner_name: str = Field(default="", max_length=200)
    asserted_right_detail: str = Field(default="", max_length=2000)
    declaration_version: str = Field(max_length=32)
    language: LanguageCode
    confirmed: bool


class PartnerIn(Schema):
    kind: PartnerKindCode
    name: str = Field(max_length=200)
    domain: str = Field(default="", max_length=253)
    path_prefix: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=2000)


class AuthorityIn(Schema):
    declaration_version: str = Field(max_length=32)
    language: LanguageCode
    confirmed: bool


class WithdrawalIn(Schema):
    reason: WithdrawalReasonCode
    note: str = Field(default="", max_length=500)
    declaration_version: str = Field(max_length=32)
    language: LanguageCode
    confirmed: bool


class NoPartnersIn(Schema):
    declaration_version: str = Field(max_length=32)
    language: LanguageCode
    confirmed: bool


class RightsWriteAcceptedOut(Schema):
    """Production's answer to a rights write it cannot apply itself (ADR 0028 amendment "The
    production write path"): accepted into the outbox, development applies it shortly. The
    customer UI shows a factual pending state and refetches the overview after a short wait —
    there is no per-item submission list here, unlike `POST /app/fingerprints`."""

    pending: bool = Field(default=True, description="Accepted; not yet applied.")


def _texts(declaration: Declaration) -> DeclarationTextOut:
    return DeclarationTextOut(
        version=declaration.version, en=declaration.text("en"), de=declaration.text("de")
    )


def overview(organisation: Organisation) -> RightsOverviewOut:
    claimant = services.claimant_for(organisation)
    originals = owned_originals(organisation)
    digests = {original.representative.sha256 for original in originals}
    blockers = organisation_blockers(organisation, digests)
    records = {
        record.sha256: record
        for record in services.current_works(organisation).filter(sha256__in=digests)
    }
    original_of_digest = {
        original.representative.sha256: original.public_id for original in originals
    }
    works = []
    for original in originals:
        sha256 = original.representative.sha256
        record = records.get(sha256)
        works.append(
            RightsWorkOut(
                original_id=original.public_id,
                title=original.representative.title,
                rights_status=cast(
                    RightsStatusCode, record.status if record else RightsStatus.UNVERIFIED
                ),
                capacity=cast(CapacityCode, record.capacity)
                if record and record.capacity
                else None,
                underlying_owner_name=(record.underlying_owner_name or None) if record else None,
                asserted_right=(
                    cast(AssertedRightCode, record.asserted_right)
                    if record and record.asserted_right
                    else None
                ),
                asserted_right_detail=record.asserted_right_detail if record else "",
                declared_at=record.declared_at if record else None,
                declaration_version=(record.declaration_version or None) if record else None,
                authority_declared_at=record.authority_declared_at if record else None,
                authority_declaration_version=(
                    (record.authority_declaration_version or None) if record else None
                ),
                notice_blockers=cast(list[BlockerCode], blockers.get(sha256, [])),
            )
        )
    partners = [
        LicensedPartnerOut(
            id=partner.public_id,
            kind=cast(PartnerKindCode, partner.kind),
            name=partner.name,
            domain=partner.domain or None,
            path_prefix=partner.path_prefix or None,
            note=partner.note,
            created_at=partner.created_at,
        )
        for partner in LicensedPartner.objects.filter(organisation=organisation).order_by("pk")
    ]
    proofs = [
        ProofOut(
            id=proof.public_id,
            kind=cast(ProofKindCode, proof.kind),
            original_id=original_of_digest.get(proof.work.sha256) if proof.work else None,
            filename=proof.original_filename,
            content_type=proof.content_type,
            size_bytes=proof.size_bytes,
            uploaded_at=proof.uploaded_at,
        )
        for proof in ProofDocument.objects.filter(organisation=organisation)
        .select_related("work")
        .order_by("-uploaded_at", "-pk")
    ]
    verified = claimant.identity_status == IdentityStatus.VERIFIED
    return RightsOverviewOut(
        claimant=RightsClaimantOut(
            claimant_type=cast(ClaimantTypeCode, claimant.claimant_type) or None,
            identity_status=cast(IdentityStatusCode, claimant.identity_status),
            identity_method=(
                cast(IdentityMethodCode, claimant.identity_method)
                if verified and claimant.identity_method
                else None
            ),
            account_control_token=claimant.account_control_token or None,
        ),
        work_declaration=_texts(CURRENT_WORK_DECLARATION),
        partner_declaration=_texts(CURRENT_PARTNER_DECLARATION),
        withdrawal_declaration=_texts(CURRENT_WITHDRAWAL),
        authority_declaration=_texts(CURRENT_AUTHORITY_DECLARATION),
        authority_withdrawal_declaration=_texts(CURRENT_AUTHORITY_WITHDRAWAL),
        partners=partners,
        partners_none_affirmed_at=claimant.partners_none_affirmed_at,
        works=works,
        proofs=proofs,
        proof_upload=ProofUploadOut(
            available=vault.available(),
            max_bytes=settings.RIGHTS_PROOF_MAX_BYTES,
            content_types=list(vault.CONTENT_TYPES),
        ),
    )


def _customer(request: HttpRequest) -> tuple[Organisation | None, services.Actor]:
    user = cast(User, request.user)
    return organisation_of(user), services.Actor.customer(user)


def _refused(error: services.RightsError) -> Status[AppErrorOut]:
    return Status(409, AppErrorOut(detail=error.code))


def writes_available() -> bool:
    """This machine applies rights writes itself: development always, production only into the
    outbox (`OUTBOX_ENABLED`) — the same rule `marceline.app.fingerprints.adding_originals_
    available` uses for originals."""
    return settings.MARCELINE_ROLE != "prod" or outbox.writes_enabled()


def into_outbox() -> bool:
    """Writes here are accepted into the outbox rather than applied (production)."""
    return settings.MARCELINE_ROLE == "prod"


def _gate_or_enqueue(
    request: HttpRequest, organisation: Organisation, kind: str, payload: dict[str, Any]
) -> Status[RightsWriteAcceptedOut] | Status[AppErrorOut] | None:
    """On production, accept a write into the outbox instead of applying it locally: 202, or a 503
    (not available / paused) or 429 (rate limit) before anything is queued. `None` on development,
    where the caller runs its own logic exactly as before — existence checks (an unknown original
    or partner) included, since production's local tables hold no originals or partners to check
    against and defers that to development's apply (`marceline/customer_writes.py`)."""
    if not into_outbox():
        return None
    if not writes_available():
        return Status(503, AppErrorOut(detail="rights_write_unavailable"))
    now = timezone.now()
    if outbox.paused(now):
        return Status(503, AppErrorOut(detail="processing_paused"))
    user = cast(User, request.user)
    refusal = upload_throttle.admit(organisation.pk, upload_throttle.Kind.RIGHTS_WRITE, now=now)
    if refusal is not None:
        return Status(429, AppErrorOut(detail="too_many_requests"))
    outbox.enqueue(
        kind=kind, organisation_id=organisation.pk, user_id=user.pk, payload=payload, now=now
    )
    return Status(202, RightsWriteAcceptedOut())


# Codes the API can refuse with; kept here so the enums stay aligned with the models.
assert set(WITHDRAWAL_REASONS) == set(WithdrawalReasonCode.__args__)  # type: ignore[attr-defined]
assert set(ClaimantType.values) == set(ClaimantTypeCode.__args__)  # type: ignore[attr-defined]
assert set(IdentityMethod.values) == set(IdentityMethodCode.__args__)  # type: ignore[attr-defined]
assert set(RightsStatus.values) == set(RightsStatusCode.__args__)  # type: ignore[attr-defined]
assert set(Capacity.values) == set(CapacityCode.__args__)  # type: ignore[attr-defined]
assert set(AssertedRight.values) == set(AssertedRightCode.__args__)  # type: ignore[attr-defined]
assert set(PartnerKind.values) == set(PartnerKindCode.__args__)  # type: ignore[attr-defined]
assert set(ProofKind.values) == set(ProofKindCode.__args__)  # type: ignore[attr-defined]
assert {str(code) for code in Blocker} == set(BlockerCode.__args__)  # type: ignore[attr-defined]
assert set(IdentityStatus.values) == set(IdentityStatusCode.__args__)  # type: ignore[attr-defined]


@router.get("", response={200: RightsOverviewOut, 404: AppErrorOut})
def rights(request: HttpRequest) -> Status[RightsOverviewOut] | Status[AppErrorOut]:
    """Claimant, partners, the rights status of every owned original, and uploaded proofs.

    A signed-in account without an organisation has nothing to declare: 404.
    """
    organisation, _ = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    return Status(200, overview(organisation))


@router.put(
    "/claimant",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def claimant_type(
    request: HttpRequest, payload: ClaimantTypeIn
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Choose the claimant type. 409 `claimant_type_locked` once staff checked documents.

    202 on production: accepted into the outbox, applied by development shortly.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    gate = _gate_or_enqueue(
        request, organisation, OutboxItem.Kind.RIGHTS_CLAIMANT_TYPE, payload.model_dump(mode="json")
    )
    if gate is not None:
        return gate
    try:
        services.set_claimant_type(organisation, payload.claimant_type, actor)
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))


@router.post(
    "/claimant/token",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def account_control_token(
    request: HttpRequest,
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Issue a new account-control token (replaces the previous one).

    202 on production: accepted into the outbox, applied by development shortly.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    gate = _gate_or_enqueue(request, organisation, OutboxItem.Kind.RIGHTS_ACCOUNT_TOKEN, {})
    if gate is not None:
        return gate
    services.issue_account_control_token(organisation, actor)
    return Status(200, overview(organisation))


@router.post(
    "/works/{original_id}/declaration",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def declare_work(
    request: HttpRequest, original_id: str, payload: WorkDeclarationIn
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Self-declare one original. 409 with a code when incomplete, outdated or not open.

    202 on production: accepted into the outbox — production's local tables carry no originals, so
    whether `original_id` is really this organisation's is checked when development applies it, not
    here. Applied by development shortly.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.RIGHTS_DECLARE,
        {"original_id": original_id, **payload.model_dump(mode="json")},
    )
    if gate is not None:
        return gate
    original = owned_original(organisation, original_id)
    if original is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    try:
        services.self_declare(
            organisation,
            original.representative.sha256,
            services.DeclarationInput(
                capacity=payload.capacity,
                asserted_right=payload.asserted_right,
                underlying_owner_name=payload.underlying_owner_name,
                asserted_right_detail=payload.asserted_right_detail,
                declaration_version=payload.declaration_version,
                language=payload.language,
                confirmed=payload.confirmed,
            ),
            actor,
        )
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))


@router.post(
    "/works/{original_id}/withdrawal",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def withdraw_declaration(
    request: HttpRequest, original_id: str, payload: WithdrawalIn
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Withdraw this original's self-declaration (back to unverified), with a reason.

    409 `declaration_not_withdrawable` unless the record is self_declared: once staff received
    documents or verified it, only staff change it. 202 on production: accepted into the outbox,
    applied by development shortly (see `declare_work` on why the existence check waits for it).
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.RIGHTS_WITHDRAW,
        {"original_id": original_id, **payload.model_dump(mode="json")},
    )
    if gate is not None:
        return gate
    original = owned_original(organisation, original_id)
    if original is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    try:
        services.withdraw_declaration(
            organisation,
            original.representative.sha256,
            services.WithdrawalInput(
                reason=payload.reason,
                note=payload.note,
                declaration_version=payload.declaration_version,
                language=payload.language,
                confirmed=payload.confirmed,
            ),
            actor,
        )
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))


@router.post(
    "/works/{original_id}/authority",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def declare_authority(
    request: HttpRequest, original_id: str, payload: AuthorityIn
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Declare that the documents on file give this organisation the right or authority it
    declared for this original (ADR 0034 amendment 2026-09-18).

    Only the customer may make this statement — staff record that the documents are formally
    complete and nothing more — so there is no staff route to it anywhere. 409
    `authority_not_declarable` unless the record is documents_complete, 409
    `declaration_version_outdated` when the wording has changed since the page was loaded.

    202 on production: accepted into the outbox, applied by development shortly (see
    `declare_work` on why the existence check waits for it).
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.RIGHTS_DECLARE_AUTHORITY,
        {"original_id": original_id, **payload.model_dump(mode="json")},
    )
    if gate is not None:
        return gate
    original = owned_original(organisation, original_id)
    if original is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    try:
        services.declare_authority(
            organisation,
            original.representative.sha256,
            services.AuthorityInput(
                declaration_version=payload.declaration_version,
                language=payload.language,
                confirmed=payload.confirmed,
            ),
            actor,
        )
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))


@router.post(
    "/works/{original_id}/authority/withdrawal",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def withdraw_authority(
    request: HttpRequest, original_id: str, payload: WithdrawalIn
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Take that statement back, with a reason (back to documents_complete). The documents stay
    on file; only the customer's statement about them goes.

    409 `authority_not_withdrawable` unless the record is authority_declared. 202 on production:
    accepted into the outbox, applied by development shortly.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.RIGHTS_WITHDRAW_AUTHORITY,
        {"original_id": original_id, **payload.model_dump(mode="json")},
    )
    if gate is not None:
        return gate
    original = owned_original(organisation, original_id)
    if original is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    try:
        services.withdraw_authority(
            organisation,
            original.representative.sha256,
            services.WithdrawalInput(
                reason=payload.reason,
                note=payload.note,
                declaration_version=payload.declaration_version,
                language=payload.language,
                confirmed=payload.confirmed,
            ),
            actor,
        )
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))


@router.post(
    "/partners",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def add_partner(
    request: HttpRequest, payload: PartnerIn
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Add a licensed partner. 409 `invalid_domain`, `partner_domain_required`, … when refused.

    202 on production: accepted into the outbox, applied by development shortly.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    gate = _gate_or_enqueue(
        request, organisation, OutboxItem.Kind.RIGHTS_ADD_PARTNER, payload.model_dump(mode="json")
    )
    if gate is not None:
        return gate
    try:
        services.add_partner(
            organisation,
            services.PartnerInput(
                kind=payload.kind,
                name=payload.name,
                domain=payload.domain,
                path_prefix=payload.path_prefix,
                note=payload.note,
            ),
            actor,
        )
    except InvalidDomainError:
        return Status(409, AppErrorOut(detail="invalid_domain"))
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))


# Registered before `/partners/{partner_id}`: Django matches URL patterns in order, and the
# delete route's pattern would otherwise swallow `/partners/none` (and answer 405).
@router.post(
    "/partners/none",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def affirm_no_partners(
    request: HttpRequest, payload: NoPartnersIn
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Affirm there are no licensed partners. 409 `partners_exist` while any is listed.

    202 on production: accepted into the outbox, applied by development shortly.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.RIGHTS_AFFIRM_NO_PARTNERS,
        payload.model_dump(mode="json"),
    )
    if gate is not None:
        return gate
    try:
        services.affirm_no_partners(
            organisation,
            version=payload.declaration_version,
            language=payload.language,
            confirmed=payload.confirmed,
            actor=actor,
        )
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))


@router.delete(
    "/partners/{partner_id}",
    response={
        200: RightsOverviewOut,
        202: RightsWriteAcceptedOut,
        404: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def remove_partner(
    request: HttpRequest, partner_id: str
) -> Status[RightsOverviewOut] | Status[RightsWriteAcceptedOut] | Status[AppErrorOut]:
    """Remove one of this organisation's partners. Anyone else's: 404.

    202 on production: accepted into the outbox — the local table carries no partners, so whether
    `partner_id` is really this organisation's is checked when development applies it. Applied by
    development shortly.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=PARTNER_NOT_FOUND))
    gate = _gate_or_enqueue(
        request, organisation, OutboxItem.Kind.RIGHTS_REMOVE_PARTNER, {"partner_id": partner_id}
    )
    if gate is not None:
        return gate
    partner = (
        LicensedPartner.objects.filter(organisation=organisation, public_id=partner_id).first()
        if public_ids.is_valid("partner", partner_id)
        else None
    )
    if partner is None:
        return Status(404, AppErrorOut(detail=PARTNER_NOT_FOUND))
    services.remove_partner(partner, actor)
    return Status(200, overview(organisation))


@router.post(
    "/proofs",
    response={
        200: RightsOverviewOut,
        404: AppErrorOut,
        409: AppErrorOut,
        413: AppErrorOut,
        415: AppErrorOut,
        503: AppErrorOut,
    },
)
def upload_proof(
    request: HttpRequest,
    file: File[UploadedFile],
    kind: Form[ProofKindCode],
    original_id: Form[str | None] = None,
    note: Form[str] = "",
) -> Status[RightsOverviewOut] | Status[AppErrorOut]:
    """Upload a rights-relevant document excerpt (PDF, PNG or JPEG) — never media.

    It is stored encrypted and read by staff only. 413 too large, 415 not a PDF/PNG/JPEG,
    503 while the vault is not configured.
    """
    organisation, actor = _customer(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    sha256 = None
    if original_id:
        original = owned_original(organisation, original_id)
        if original is None:
            return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
        sha256 = original.representative.sha256
    if not vault.available():
        return Status(503, AppErrorOut(detail="unavailable"))
    if (file.size or 0) > settings.RIGHTS_PROOF_MAX_BYTES:
        return Status(413, AppErrorOut(detail="too_large"))
    data = file.read(settings.RIGHTS_PROOF_MAX_BYTES + 1)
    try:
        # Refuse a bad file before anything is written, so a refusal leaves no record behind.
        vault.check_upload(data)
        work = services.open_record(organisation, sha256, actor) if sha256 else None
        services.upload_proof(
            organisation,
            data,
            filename=file.name or "document",
            kind=kind,
            work=work,
            note=note[:2000],
            actor=actor,
        )
    except vault.VaultError as error:
        status = {"too_large": 413, "unsupported_type": 415, "empty": 415}.get(error.code, 503)
        return Status(status, AppErrorOut(detail=error.code))
    except services.RightsError as error:
        return _refused(error)
    return Status(200, overview(organisation))
