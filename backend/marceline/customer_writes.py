"""Development applies the customer writes production accepted (ADR 0028 amendment "The production
write path").

`delivery.inbox` moves, verifies and records; this module decides what a write *means*, and only by
calling the code development's own endpoints run — never a second implementation:

- `add_original` → `fingerprints.register_original` (the endpoint's function, which re-validates):
  `done` with `created` / `already_present` and the original's public id, or `refused` with the
  endpoint's code (`claim_needs_review`, `invalid_fingerprint`, …);
- `client_event` → `fingerprints.record_client_event`: `done` with `recorded`.
- Rights writes (ADR 0034) → one handler per kind, each calling the same `marceline.rights.services`
  function `marceline/app/rights_status.py` calls, with `Actor.customer(user)`: `rights_claimant_
  type` → `set_claimant_type`, `rights_account_token` → `issue_account_control_token`,
  `rights_declare` → `self_declare`, `rights_withdraw` → `withdraw_declaration`, `rights_add_
  partner` → `add_partner`, `rights_remove_partner` → `remove_partner`, `rights_affirm_no_partners`
  → `affirm_no_partners`. `declare_work` and `withdraw_declaration`/`remove_partner` carry
  `original_id`/`partner_id` in the payload (path parameters on the endpoint) alongside the body;
  production's local tables hold no originals or partners, so *this* is where `owned_original` /
  the partner lookup runs and can answer `original_not_found` / `partner_not_found` — the 404 the
  endpoint itself gives on development. **Proof documents stay development-only** (the rights vault
  holds sensitive documents; production must never store them, not even in transit) — that upload
  is not a kind, and `/api/v1/app/rights/proofs` keeps `ROLE_ONLY_PREFIXES["dev"]`.
- Notice writes (ADR 0035, ADR 0046) → `notice_create` → `app.notices.create_notice` (the very
  function the development endpoint calls), the rest through `marceline.notices.services`:
  `notice_approve` → `approve`, `notice_mark_sent` → `mark_sent`, `notice_response` →
  `record_provider_response`, `notice_withdraw` → `withdraw`, `notice_correct` → `correct`, and
  `notice_exported` → `export_notice`, the audit record of a download production served from its
  cache. Each carries `notice_id` in the payload and is resolved with `app.notices.draft_for`, so
  the tenancy rule and the safety filter are applied **here**, against today's state: a draft
  whose page became safety-flagged after production accepted the write is refused with
  `notice_not_found`, the same answer an unknown id gets. **The sandbox send is not a kind** — it
  is the one path that may hand a draft to a mail transport and stays development-only.

- The **mailed account actions** (ADR 0054) arrive here because development holds the accounts, the
  tokens and the only mail transport: `password_reset_request` mails a link if the address has a
  customer account and says nothing either way, `password_reset_set` sets `User.password` from the
  hash production made and ends every session, `email_verify` records a confirmation and
  `email_verify_resend` sends the mail again for the signed-in account that asked.
- The **invite redemption** (ADR 0052 §9) is the one write that arrives before an account exists,
  so it names no user and no organisation (both ids are 0) and carries the invite's digest instead.
  `accounts.invites.apply_redemption` re-checks the declarations' versions, the published documents
  and the invite's state and ends in `complete()` — the same transaction the development endpoint
  runs. An invite already redeemed is `done` with no effect, not a refusal.
- The creator's own data requests (ADR 0056, issue #133) → `data_request_open` /
  `data_request_withdraw` → `deletion.requests.open_request` / `withdraw`, the same two calls
  `marceline/app/data_requests.py` makes. Development is where the request has to land, because it
  is where the staff member who answers it works and where `end_pilot` and `export_customer_data`
  run. The switch is re-read here, so a request accepted by a public host whose switch is on while
  development's is off is refused rather than applied. Neither handler exports or deletes anything.
- Account-control verification (ADR 0055) → `verification_claim` →
  `account_control_services.claim_account` and `verification_placed` → `mark_placed`, the same two
  calls `marceline/app/verification.py` makes. The code itself is generated **here**, never on the
  public host: it has to live where the staff member who compares it by eye will read it, and it
  reaches the creator through the next signed bundle. `verification_placed` carries
  `challenge_id` (a path parameter on the endpoint) and resolves it here for the same reason
  `rights_declare` resolves `original_id` here — production holds no challenge rows.

Re-validation is deliberate: production checked the document with the same `validated`, but
development is where the originals (and rights records) are, so a write is judged again against
today's data and code (a rights wording that became outdated in between is refused, not stored).
For a notice write there is nothing production *could* have checked beyond the schema: the notice
gate reads match rows, copy extent, the rights record and the provider directory, none of which
exist there.

**Who the write is from** is checked here, not trusted: the user must still exist and still be a
member of the organisation the item names (accounts replicate by development's ids, so both ids
are development's). Otherwise the write is refused with `no_organisation`.

After a write applies, the organisation's cached bundle on production is expired twice over: by the
acknowledgement itself (`delivery.outbox.apply_ack`) and by the usual `expire` purge
(`cache_purge`), so the customer's next request shows it — for a rights write that means the
`rights` document, added to the export the same way as `originals`/`cases`.

**Order matters per organisation** (declare before a later withdrawal, an upload before a
declaration of the same work): `delivery.inbox.run_once` applies pulled items in acceptance order
and, once, per organisation — nothing here reorders them.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from accounts import account_actions, onboarding
from accounts import invites as invite_services
from accounts.models import Membership, Organisation
from core import public_ids
from delivery.inbox import Outcome, PulledItem
from delivery.models import OutboxItem, PurgeOutbox
from marceline import cache_purge
from marceline.app.fingerprints import (
    AddOriginalIn,
    AppClientEventIn,
    Refused,
    record_client_event,
    register_original,
)
from marceline.app.notices import (
    ConfirmationIn,
    CorrectIn,
    CreateNoticeIn,
    ResponseIn,
    WithdrawIn,
    create_notice,
    draft_for,
)
from marceline.app.ownership import owned_original
from marceline.app.rights_status import (
    AuthorityIn,
    ClaimantTypeIn,
    NoPartnersIn,
    PartnerIn,
    WithdrawalIn,
    WorkDeclarationIn,
)
from marceline.app.verification import ClaimAccountIn, claimed_account
from marceline.deletion import requests as data_requests
from marceline.deletion.models import CustomerDataRequest
from marceline.notices import services as notice_services
from marceline.notices.models import NoticeDraft
from marceline.rights import account_control_services as verification_services
from marceline.rights import services as rights_services
from marceline.rights.models import LicensedPartner
from marceline.rights.partners import InvalidDomainError

logger = logging.getLogger("marceline.outbox")


class UnknownKind(Exception):
    """A kind this development code does not know (production is newer): retried, then
    quarantined. Deploy development before production."""


def _member(item: PulledItem) -> tuple[User, Organisation] | None:
    membership = (
        Membership.objects.select_related("user", "organisation")
        .filter(user_id=item.user_id, organisation_id=item.organisation_id, user__is_active=True)
        .first()
    )
    if membership is None:
        return None
    return membership.user, membership.organisation


def apply_add_original(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        payload = AddOriginalIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_fingerprint")
    try:
        created, original = register_original(user, organisation, payload)
    except Refused as refused:
        return Outcome("refused", refused.code)
    cache_purge.request_purge(
        PurgeOutbox.Mode.EXPIRE, reason="customer_write", organisation_ids=[organisation.pk]
    )
    return Outcome(
        "done", "created" if created else "already_present", original_id=original.public_id
    )


def apply_client_event(item: PulledItem) -> Outcome:
    try:
        payload = AppClientEventIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_event")
    member = _member(item)
    record_client_event(member[0] if member is not None else None, payload)
    return Outcome("done", "recorded")


def _expire_bundle(organisation: Organisation) -> None:
    cache_purge.request_purge(
        PurgeOutbox.Mode.EXPIRE, reason="customer_write", organisation_ids=[organisation.pk]
    )


def apply_rights_claimant_type(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        payload = ClaimantTypeIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        rights_services.set_claimant_type(
            organisation, payload.claimant_type, rights_services.Actor.customer(user)
        )
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "claimant_type_set")


def apply_rights_account_token(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    rights_services.issue_account_control_token(organisation, rights_services.Actor.customer(user))
    _expire_bundle(organisation)
    return Outcome("done", "token_issued")


def apply_rights_declare(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        raw: dict[str, Any] = json.loads(item.payload)
        payload = WorkDeclarationIn.model_validate(raw)
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    original_id = raw.get("original_id")
    original = owned_original(organisation, original_id) if isinstance(original_id, str) else None
    if original is None:
        return Outcome("refused", "original_not_found")
    try:
        rights_services.self_declare(
            organisation,
            original.representative.sha256,
            rights_services.DeclarationInput(
                capacity=payload.capacity,
                asserted_right=payload.asserted_right,
                underlying_owner_name=payload.underlying_owner_name,
                asserted_right_detail=payload.asserted_right_detail,
                declaration_version=payload.declaration_version,
                language=payload.language,
                confirmed=payload.confirmed,
            ),
            rights_services.Actor.customer(user),
        )
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "declared")


def apply_rights_withdraw(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        raw: dict[str, Any] = json.loads(item.payload)
        payload = WithdrawalIn.model_validate(raw)
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    original_id = raw.get("original_id")
    original = owned_original(organisation, original_id) if isinstance(original_id, str) else None
    if original is None:
        return Outcome("refused", "original_not_found")
    try:
        rights_services.withdraw_declaration(
            organisation,
            original.representative.sha256,
            rights_services.WithdrawalInput(
                reason=payload.reason,
                note=payload.note,
                declaration_version=payload.declaration_version,
                language=payload.language,
                confirmed=payload.confirmed,
            ),
            rights_services.Actor.customer(user),
        )
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "withdrawn")


def apply_rights_declare_authority(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        raw: dict[str, Any] = json.loads(item.payload)
        payload = AuthorityIn.model_validate(raw)
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    original_id = raw.get("original_id")
    original = owned_original(organisation, original_id) if isinstance(original_id, str) else None
    if original is None:
        return Outcome("refused", "original_not_found")
    try:
        rights_services.declare_authority(
            organisation,
            original.representative.sha256,
            rights_services.AuthorityInput(
                declaration_version=payload.declaration_version,
                language=payload.language,
                confirmed=payload.confirmed,
            ),
            rights_services.Actor.customer(user),
        )
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "authority_declared")


def apply_rights_withdraw_authority(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        raw = json.loads(item.payload)
        payload_out = WithdrawalIn.model_validate(raw)
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    original_id = raw.get("original_id")
    original = owned_original(organisation, original_id) if isinstance(original_id, str) else None
    if original is None:
        return Outcome("refused", "original_not_found")
    try:
        rights_services.withdraw_authority(
            organisation,
            original.representative.sha256,
            rights_services.WithdrawalInput(
                reason=payload_out.reason,
                note=payload_out.note,
                declaration_version=payload_out.declaration_version,
                language=payload_out.language,
                confirmed=payload_out.confirmed,
            ),
            rights_services.Actor.customer(user),
        )
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "authority_withdrawn")


def apply_rights_add_partner(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        payload = PartnerIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        rights_services.add_partner(
            organisation,
            rights_services.PartnerInput(
                kind=payload.kind,
                name=payload.name,
                domain=payload.domain,
                path_prefix=payload.path_prefix,
                note=payload.note,
            ),
            rights_services.Actor.customer(user),
        )
    except InvalidDomainError:
        return Outcome("refused", "invalid_domain")
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "partner_added")


def apply_rights_remove_partner(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        raw = json.loads(item.payload)
    except ValueError:
        return Outcome("refused", "invalid_payload")
    partner_id = raw.get("partner_id")
    partner = (
        LicensedPartner.objects.filter(organisation=organisation, public_id=partner_id).first()
        if isinstance(partner_id, str) and public_ids.is_valid("partner", partner_id)
        else None
    )
    if partner is None:
        return Outcome("refused", "partner_not_found")
    rights_services.remove_partner(partner, rights_services.Actor.customer(user))
    _expire_bundle(organisation)
    return Outcome("done", "partner_removed")


def apply_rights_affirm_no_partners(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        payload = NoPartnersIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        rights_services.affirm_no_partners(
            organisation,
            version=payload.declaration_version,
            language=payload.language,
            confirmed=payload.confirmed,
            actor=rights_services.Actor.customer(user),
        )
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "partners_none_affirmed")


# --- notice drafts (ADR 0035, ADR 0046) ---------------------------------------------------------
#
# Production holds no findings, no match rows, no copy extent, no rights records and no provider
# directory, so it cannot judge a notice write at all — it only takes it down. Everything is
# decided here, against today's data: `draft_for` re-applies the tenancy and safety filter (a
# draft whose page became safety-flagged in the meantime is refused exactly as it would be on
# development, with the 404 an unknown id gets), and each `marceline.notices.services` call
# re-runs the notice gate and the state machine. The customer reads the outcome in
# `GET /app/notices/submissions`.

#: The rights-side blocker codes `marceline.rights.gate.notice_gate_blockers` can name (the same
#: set `notices/CLAUDE.md` and the SPA's `BLOCKERS` list carry) — everything the notice gate folds
#: in ahead of its own availability/match/channel checks.
_RIGHTS_BLOCKERS = frozenset(
    {
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
    }
)
#: `notices/gate.py FindingBlocker`'s own availability codes.
_AVAILABILITY_BLOCKERS = frozenset({"page_not_available", "availability_not_recent"})


def _submission_code_for(error: notice_services.NoticeError) -> str:
    """The code `GET /app/notices/submissions` stores for a refused create or approve.

    `finding_not_eligible` / `finding_no_longer_eligible` fold every blocker of every selected
    page into one exception (`notices/CLAUDE.md`), because the gate is re-checked as one whole —
    but that single code gives the customer nothing to act on. This narrows it, from the very same
    blocker codes the live create/approve screen already shows inline, to what the async
    submissions screen can say on its own: a refusal whose blockers are all on the rights side
    points at the rights page, one whose blockers are only about the page's availability needs
    nothing from the customer, and a mixed or otherwise-shaped refusal keeps the generic code —
    "reload and check the list below it" is true of it either way. Every other `NoticeError` code
    is returned unchanged.
    """
    if error.code not in ("finding_not_eligible", "finding_no_longer_eligible"):
        return error.code
    blockers_by_page = error.detail if isinstance(error.detail, dict) else {}
    codes = {code for page_codes in blockers_by_page.values() for code in page_codes}
    if codes and codes <= _RIGHTS_BLOCKERS:
        return "finding_rights_blocked"
    if codes and codes <= _AVAILABILITY_BLOCKERS:
        return "finding_unavailable"
    return error.code


def _draft_of(item: PulledItem, organisation: Organisation) -> tuple[str, NoticeDraft] | Outcome:
    """The draft an item names, or the refusal the customer sees. `(notice_id, draft)`."""
    try:
        raw: dict[str, Any] = json.loads(item.payload)
    except ValueError:
        return Outcome("refused", "invalid_payload")
    notice_id = raw.get("notice_id")
    if not isinstance(notice_id, str):
        return Outcome("refused", "notice_not_found")
    draft = draft_for(organisation, notice_id)
    if draft is None:
        return Outcome("refused", "notice_not_found")
    return notice_id, draft


def apply_notice_create(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        payload = CreateNoticeIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        draft = create_notice(organisation, payload, notice_services.Actor.customer(user))
    except notice_services.NoticeError as error:
        return Outcome("refused", _submission_code_for(error))
    _expire_bundle(organisation)
    return Outcome("done", "created", original_id=draft.public_id)


def apply_notice_approve(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    found = _draft_of(item, organisation)
    if isinstance(found, Outcome):
        return found
    notice_id, draft = found
    try:
        confirmations = [
            ConfirmationIn.model_validate(entry)
            for entry in json.loads(item.payload).get("confirmations", [])
        ]
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    inputs = [
        notice_services.ConfirmationInput(
            kind=entry.kind,
            version=entry.version,
            language=entry.language,
            confirmed=entry.confirmed,
        )
        for entry in confirmations
    ]
    try:
        notice_services.approve(draft, inputs, notice_services.Actor.customer(user))
    except notice_services.NoticeError as error:
        return Outcome("refused", _submission_code_for(error), original_id=notice_id)
    _expire_bundle(organisation)
    return Outcome("done", "approved", original_id=notice_id)


def apply_notice_mark_sent(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    found = _draft_of(item, organisation)
    if isinstance(found, Outcome):
        return found
    notice_id, draft = found
    try:
        notice_services.mark_sent(draft, notice_services.Actor.customer(user))
    except notice_services.NoticeError as error:
        return Outcome("refused", error.code, original_id=notice_id)
    _expire_bundle(organisation)
    return Outcome("done", "marked_sent", original_id=notice_id)


def apply_notice_response(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    found = _draft_of(item, organisation)
    if isinstance(found, Outcome):
        return found
    notice_id, draft = found
    try:
        payload = ResponseIn.model_validate(json.loads(item.payload))
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    try:
        notice_services.record_provider_response(
            draft,
            payload.outcome,
            notice_services.Actor.customer(user),
            responded_at=payload.responded_at,
        )
    except notice_services.NoticeError as error:
        return Outcome("refused", error.code, original_id=notice_id)
    _expire_bundle(organisation)
    return Outcome("done", "response_recorded", original_id=notice_id)


def apply_notice_withdraw(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    found = _draft_of(item, organisation)
    if isinstance(found, Outcome):
        return found
    notice_id, draft = found
    try:
        payload = WithdrawIn.model_validate(json.loads(item.payload))
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    try:
        notice_services.withdraw(
            draft, payload.reason, payload.note, notice_services.Actor.customer(user)
        )
    except notice_services.NoticeError as error:
        return Outcome("refused", error.code, original_id=notice_id)
    _expire_bundle(organisation)
    return Outcome("done", "withdrawn", original_id=notice_id)


def apply_notice_correct(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    found = _draft_of(item, organisation)
    if isinstance(found, Outcome):
        return found
    _, draft = found
    try:
        payload = CorrectIn.model_validate(json.loads(item.payload))
    except (ValueError, ValidationError):
        return Outcome("refused", "invalid_payload")
    try:
        successor = notice_services.correct(
            draft, payload.reason, payload.note, notice_services.Actor.customer(user)
        )
    except notice_services.NoticeError as error:
        return Outcome("refused", error.code, original_id=draft.public_id)
    _expire_bundle(organisation)
    return Outcome("done", "corrected", original_id=successor.public_id)


def apply_notice_exported(item: PulledItem) -> Outcome:
    """A download that happened on production: append the `exported` event, send nothing.

    The bytes left production's cache; the append-only log lives here, so this is the only way
    the record of a real download reaches it. The draft is looked up through the same
    `draft_for`, so a draft that became safety-hidden in the meantime records nothing.
    """
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    found = _draft_of(item, organisation)
    if isinstance(found, Outcome):
        return found
    notice_id, draft = found
    try:
        fmt = json.loads(item.payload).get("format", "")
        notice_services.export_notice(draft, fmt, notice_services.Actor.customer(user))
    except (ValueError, notice_services.NoticeError) as error:
        code = error.code if isinstance(error, notice_services.NoticeError) else "invalid_payload"
        return Outcome("refused", code, original_id=notice_id)
    return Outcome("done", "exported", original_id=notice_id)


# --- onboarding by invite (ADR 0052 §9) ---------------------------------------------------------
#
# The one write that arrives **before an account exists**, so there is no `_member` to check: the
# item names the invite by its digest and nothing else. Production could check the token against the
# open invites the accounts document carries, that the documents it displayed were the published
# ones and that Django's password validators accepted the password — and nothing more, because the
# declarations' wording, the published versions and the invite's own state live here. So all of that
# is judged again in `accounts.invites.apply_redemption`, which ends in the same `complete()` the
# development endpoint runs: one transaction, `Membership.full_clean()`, the issue #92 guard.
#
# **The password never travelled in clear**: production hashed it with `make_password` (the same
# `PASSWORD_HASHERS`, which the project does not change) and only the hash is in this payload;
# `User.password` is set from it as it arrives, which is why `apply_redemption` refuses a string
# Django does not recognise as one of its own hashes.


class AcceptedDocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(max_length=64)
    version: str = Field(max_length=40)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InviteRedemptionIn(BaseModel):
    """A finished redemption as production accepted it.

    No token, no address, no declaration text: development holds all three.
    """

    model_config = ConfigDict(extra="forbid")

    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    password_hash: str = Field(min_length=1, max_length=128)
    declaration_version: str = Field(max_length=40)
    age_declaration_version: str = Field(max_length=40)
    declared_at: AwareDatetime
    age_declared_at: AwareDatetime
    country: str = Field(max_length=2)
    language: str = Field(default="", max_length=8)
    accepted_at: AwareDatetime
    documents: list[AcceptedDocumentIn] = Field(max_length=8)

    def as_redemption(self) -> invite_services.Redemption:
        return invite_services.Redemption(
            token_hash=self.token_hash,
            password_hash=self.password_hash,
            declaration_version=self.declaration_version,
            age_declaration_version=self.age_declaration_version,
            declared_at=self.declared_at,
            age_declared_at=self.age_declared_at,
            country=self.country,
            language=self.language,
            accepted_at=self.accepted_at,
            documents=tuple(
                invite_services.AcceptedDocument(
                    document_id=document.document_id,
                    version=document.version,
                    sha256=document.sha256,
                )
                for document in self.documents
            ),
        )


def apply_invite_redeem(item: PulledItem) -> Outcome:
    """Create the account a creator redeemed an invite for on production (ADR 0052 §9).

    An invite that is already redeemed settles as **done with no effect**: it is the same person
    pressing twice, or an acknowledgement that was lost, not an error. Everything else is a refusal
    with a code an operator reads on the item in the admin — the creator cannot be told, because
    production has no channel to reach somebody who has no account (ADR 0052 §9), and the invite
    stays open so the operator can finish the job by hand.
    """
    try:
        payload = InviteRedemptionIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        user = invite_services.apply_redemption(payload.as_redemption())
    except invite_services.AlreadyRedeemed:
        return Outcome("done", "already_redeemed")
    except invite_services.AccountAlreadyExists:
        return Outcome("refused", "account_exists")
    except invite_services.RedemptionRefused as refused:
        return Outcome("refused", refused.code)
    except invite_services.InviteRefused:
        return Outcome("refused", onboarding.REFUSED)
    except DjangoValidationError:
        # `ONBOARDING_BUSINESS_STATUS_GUARD=refuse`: the evidence of issue #92 is a human's step.
        return Outcome("refused", "business_status_not_evidenced")
    logger.info("invite redeemed on production: account %s created", user.pk)
    return Outcome("done", "account_created")


# --- account-control verification (ADR 0055) -----------------------------------------------------
#
# Production holds no challenge rows and generates no codes: a claim is a fact about our record, and
# the code has to exist where a staff member will compare it by eye. So both writes are resolved
# here, against today's state — which is also where the switch is read, so a write accepted by a
# public host whose switch is on while development's is off is refused rather than applied.


def apply_verification_claim(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        payload = ClaimAccountIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        challenge = verification_services.claim_account(
            organisation,
            verification_services.ClaimInput(
                platform=payload.platform,
                handle=payload.handle,
                profile_url=payload.profile_url,
            ),
            rights_services.Actor.customer(user),
        )
    except rights_services.RightsError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", "claimed", original_id=challenge.public_id)


def apply_verification_placed(item: PulledItem) -> Outcome:
    """ "I have placed the code" — the claim is looked up **here**, so a foreign or unknown id is
    the same refusal the endpoint gives on development, and nothing says which it was."""
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    try:
        raw: dict[str, Any] = json.loads(item.payload)
    except ValueError:
        return Outcome("refused", "invalid_payload")
    challenge_id = raw.get("challenge_id")
    challenge = (
        claimed_account(organisation, challenge_id) if isinstance(challenge_id, str) else None
    )
    if challenge is None:
        return Outcome("refused", "account_not_found")
    try:
        verification_services.mark_placed(challenge, rights_services.Actor.customer(user))
    except rights_services.RightsError as error:
        return Outcome("refused", error.code, original_id=challenge.public_id)
    _expire_bundle(organisation)
    return Outcome("done", "placed", original_id=challenge.public_id)


# --- the creator's own data requests (ADR 0056, issue #133) ---------------------------------------
#
# The request belongs on development: that is where a staff member reads it, where `end_pilot` sets
# the date a deletion runs from (ADR 0045) and where `export_customer_data` renders an export. The
# public host validated the typed confirmation already; it is checked again here, because a write
# that travels between two machines is re-judged on the machine that acts on it and a deletion
# request is the last one to take on trust.


def _request_payload(item: PulledItem) -> tuple[str, str] | None:
    """The requested kind and the typed confirmation, or None when the payload is not one."""
    try:
        raw: dict[str, Any] = json.loads(item.payload)
    except ValueError:
        return None
    kind = raw.get("kind")
    confirm = raw.get("confirm", "")
    if not isinstance(kind, str) or not isinstance(confirm, str):
        return None
    return kind, confirm


def apply_data_request_open(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, organisation = member
    if not data_requests.available():
        return Outcome("refused", "data_requests_unavailable")
    parsed = _request_payload(item)
    if parsed is None:
        return Outcome("refused", "invalid_payload")
    kind, confirm = parsed
    if (
        kind == CustomerDataRequest.Kind.DELETION
        and confirm.strip() != data_requests.DELETION_PHRASE
    ):
        return Outcome("refused", "confirmation_mismatch")
    try:
        data_requests.open_request(organisation, kind, by=user)
    except data_requests.DataRequestError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", f"{kind}_requested")


def apply_data_request_withdraw(item: PulledItem) -> Outcome:
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    _, organisation = member
    if not data_requests.available():
        return Outcome("refused", "data_requests_unavailable")
    parsed = _request_payload(item)
    if parsed is None:
        return Outcome("refused", "invalid_payload")
    kind, _confirm = parsed
    try:
        data_requests.withdraw(organisation, kind)
    except data_requests.DataRequestError as error:
        return Outcome("refused", error.code)
    _expire_bundle(organisation)
    return Outcome("done", f"{kind}_withdrawn")


# --- the mailed account actions (ADR 0054) -------------------------------------------------------
#
# Development holds the accounts, the tokens **and the only mail transport**: the public host has
# none and is given none (ADR 0054 §1), so all four of these arrive from there. Three carry no
# account at all (`organisation_id`/`user_id` 0) because a reset is asked for before anybody is
# signed in; the resend is a signed-in customer's own write and names itself.
#
# A request's outcome says only that it was processed — never whether the address had an account.
# The item is staff-visible in the admin and belongs to no organisation, so nothing renders it to a
# customer, but the discipline is the endpoint's and this is not the place to break it.


class PasswordResetRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(default="", max_length=254)
    language: str = Field(default="", max_length=8)


class PasswordResetSetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    password_hash: str = Field(min_length=1, max_length=128)


class TokenOnlyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class LanguageOnlyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="", max_length=8)


def apply_password_reset_request(item: PulledItem) -> Outcome:
    """Mail a reset link, if that address has a customer account. Says nothing either way."""
    try:
        payload = PasswordResetRequestIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    account_actions.request_password_reset(payload.email, language=payload.language)
    return Outcome("done", "requested")


def apply_password_reset_set(item: PulledItem) -> Outcome:
    """Set the new password from the hash production made, and end every session (ADR 0054 §3)."""
    try:
        payload = PasswordResetSetIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        account_actions.apply_password_reset(payload.token_hash, payload.password_hash)
    except account_actions.PayloadRefused as refused:
        return Outcome("refused", refused.code)
    return Outcome("done", "password_set")


def apply_email_verify(item: PulledItem) -> Outcome:
    """Record that the address was confirmed from the mailed link."""
    try:
        payload = TokenOnlyIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    try:
        account_actions.apply_verification(payload.token_hash)
    except account_actions.PayloadRefused as refused:
        return Outcome("refused", refused.code)
    return Outcome("done", "verified")


def apply_email_verify_resend(item: PulledItem) -> Outcome:
    """Send the verification mail again, for the signed-in account that asked."""
    member = _member(item)
    if member is None:
        return Outcome("refused", "no_organisation")
    user, _ = member
    try:
        payload = LanguageOnlyIn.model_validate_json(item.payload)
    except ValidationError:
        return Outcome("refused", "invalid_payload")
    account_actions.request_verification(user, language=payload.language)
    return Outcome("done", "requested")


HANDLERS: dict[str, Callable[[PulledItem], Outcome]] = {
    OutboxItem.Kind.ADD_ORIGINAL: apply_add_original,
    OutboxItem.Kind.CLIENT_EVENT: apply_client_event,
    OutboxItem.Kind.RIGHTS_CLAIMANT_TYPE: apply_rights_claimant_type,
    OutboxItem.Kind.RIGHTS_ACCOUNT_TOKEN: apply_rights_account_token,
    OutboxItem.Kind.RIGHTS_DECLARE: apply_rights_declare,
    OutboxItem.Kind.RIGHTS_WITHDRAW: apply_rights_withdraw,
    OutboxItem.Kind.RIGHTS_DECLARE_AUTHORITY: apply_rights_declare_authority,
    OutboxItem.Kind.RIGHTS_WITHDRAW_AUTHORITY: apply_rights_withdraw_authority,
    OutboxItem.Kind.RIGHTS_ADD_PARTNER: apply_rights_add_partner,
    OutboxItem.Kind.RIGHTS_REMOVE_PARTNER: apply_rights_remove_partner,
    OutboxItem.Kind.RIGHTS_AFFIRM_NO_PARTNERS: apply_rights_affirm_no_partners,
    OutboxItem.Kind.NOTICE_CREATE: apply_notice_create,
    OutboxItem.Kind.NOTICE_APPROVE: apply_notice_approve,
    OutboxItem.Kind.NOTICE_MARK_SENT: apply_notice_mark_sent,
    OutboxItem.Kind.NOTICE_RESPONSE: apply_notice_response,
    OutboxItem.Kind.NOTICE_WITHDRAW: apply_notice_withdraw,
    OutboxItem.Kind.NOTICE_CORRECT: apply_notice_correct,
    OutboxItem.Kind.NOTICE_EXPORTED: apply_notice_exported,
    OutboxItem.Kind.INVITE_REDEEM: apply_invite_redeem,
    OutboxItem.Kind.VERIFICATION_CLAIM: apply_verification_claim,
    OutboxItem.Kind.VERIFICATION_PLACED: apply_verification_placed,
    OutboxItem.Kind.DATA_REQUEST_OPEN: apply_data_request_open,
    OutboxItem.Kind.DATA_REQUEST_WITHDRAW: apply_data_request_withdraw,
    OutboxItem.Kind.PASSWORD_RESET_REQUEST: apply_password_reset_request,
    OutboxItem.Kind.PASSWORD_RESET_SET: apply_password_reset_set,
    OutboxItem.Kind.EMAIL_VERIFY: apply_email_verify,
    OutboxItem.Kind.EMAIL_VERIFY_RESEND: apply_email_verify_resend,
}


def apply(item: PulledItem) -> Outcome:
    """The `delivery.inbox.Apply` development runs: dispatch by kind."""
    handler = HANDLERS.get(item.kind)
    if handler is None:
        raise UnknownKind(item.kind)
    return handler(item)
