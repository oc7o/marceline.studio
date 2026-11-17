"""Proof of control of the creator's own platform account: `/api/v1/app/verification` (ADR 0055).

The customer's half of the flow, and it is short by design: say which account is yours, read the
code, put it up, press "I have placed it", wait for a person to look. Three things a customer never
does here — upload a document, type a date of birth, or learn anything about another organisation.

- `GET ""` — the platforms we know, this organisation's claims with their state, and the live code
  of an open one. It is the document production serves from the signed bundle (ADR 0028), so it must
  stay a pure read: an expired code reads `expired` through `account_control_services.display_state`
  and **nothing is written** to record that.
- `POST "/accounts"` — claim a platform account; the answer carries the fresh code.
- `POST "/accounts/{challenge_id}/placed"` — "it is up now". A statement, never a check.

The staff decision is not here and never will be: verifying is a person opening a public profile in
their own browser (`rights/admin.py`). **This module fetches nothing** — the profile URL a creator
gives us is stored as text and shown to staff (`rights/account_control.py`).

With `ACCOUNT_CONTROL_VERIFICATION_ENABLED` off every route answers 404, like the upload routes
with cloud fingerprinting off. On production (ADR 0028 write path) the two writes are accepted into
the outbox and answered **202**; development applies them with these same service calls
(`marceline/customer_writes.py`). Same rules as the rest of the customer API: scoped by
organisation, public ids only (`chal_…`), a malformed, foreign or unknown id is one byte-identical
404, CSRF on every write, codes not sentences.
"""

from datetime import datetime
from typing import Any, Literal, cast

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema, Status
from pydantic import Field

from accounts.models import Organisation
from core import public_ids
from delivery import outbox
from delivery.models import OutboxItem
from marceline.app import upload_throttle
from marceline.app.auth import customer_auth
from marceline.app.ownership import organisation_of
from marceline.app.rights_status import into_outbox, writes_available
from marceline.app.schemas import AppErrorOut
from marceline.rights import account_control, services
from marceline.rights import account_control_services as verification
from marceline.rights.models import AccountControlChallenge, VerificationMethod, VerificationState

router = Router(tags=["app"], auth=customer_auth)

CHALLENGE_ID_PREFIX = "chal"
#: One answer for "no such claim", "someone else's claim" and "not a claim id at all".
CHALLENGE_NOT_FOUND = "Account not found"
#: What the whole feature answers when it is switched off: the same bare not-found as an unknown id.
UNAVAILABLE = "Not found"

PlatformCode = Literal[
    "fanhub",
    "fanbase",
    "clipmarket",
    "fancentral",
    "loyalclub",
    "patreon",
    "instagram",
    "x",
    "reddit",
    "tiktok",
    "own_website",
    "other_platform",
]
VerificationStateCode = Literal["pending", "placed", "verified", "rejected", "expired", "revoked"]
VerificationMethodCode = Literal["bio_code", "dm_code", "video_call", "staff_legacy"]

assert set(account_control.PLATFORM_CODES) == set(PlatformCode.__args__)  # type: ignore[attr-defined]
assert set(VerificationState.values) == set(VerificationStateCode.__args__)  # type: ignore[attr-defined]
assert set(VerificationMethod.values) == set(VerificationMethodCode.__args__)  # type: ignore[attr-defined]


class PlatformOut(Schema):
    code: PlatformCode
    label: str = Field(description="The platform's own name, not a translated word.")
    hosts: list[str] = Field(
        description="The only hosts a profile link may name. Empty: this platform takes no link."
    )
    code_accepted: bool = Field(
        description="Whether a placed code can settle a claim on this platform. False where the "
        "platform runs no identity check of its own before publication (ADR 0055): no code is "
        "issued there and staff arrange a short video call instead."
    )


class ClaimedAccountOut(Schema):
    id: str = Field(description="Public id (chal_…).")
    platform: PlatformCode
    handle: str
    profile_url: str | None = Field(
        description="What you gave us, as text. We never open it; a person does."
    )
    state: VerificationStateCode = Field(
        description="pending: a code is waiting. placed: you told us it is up. expired: the code "
        "ran out — ask for a new one."
    )
    code: str | None = Field(
        description="The one-time code to place, while one is live. Null once the claim is settled."
    )
    code_expires_at: datetime | None
    method: VerificationMethodCode | None = Field(
        description="How it was established, once it was. Null otherwise."
    )
    decided_at: datetime | None
    placed_at: datetime | None


class VerificationStatusOut(Schema):
    identity_status: Literal["unverified", "verified", "revoked"] = Field(
        description="The organisation's identity check — the same field the notice gate reads."
    )
    method: VerificationMethodCode | None = Field(
        description="How it was established. `staff_legacy`: recorded by staff outside this flow."
    )
    code_ttl_hours: int = Field(description="How long a code stays usable.")
    platforms: list[PlatformOut]
    accounts: list[ClaimedAccountOut]


class ClaimAccountIn(Schema):
    platform: PlatformCode
    handle: str = Field(max_length=account_control.MAX_HANDLE)
    profile_url: str = Field(default="", max_length=account_control.MAX_PROFILE_URL)


class VerificationWriteAcceptedOut(Schema):
    """Production's answer to a write it cannot apply itself (ADR 0028 write path): accepted into
    the outbox, applied by development shortly. The screen says so and shows no code yet — the code
    is generated where the record lives and arrives with the next read."""

    pending: bool = Field(default=True, description="Accepted; not yet applied.")


def _account_out(challenge: AccountControlChallenge, *, now: datetime) -> ClaimedAccountOut:
    state = verification.display_state(challenge, now=now)
    live = state in (VerificationState.PENDING, VerificationState.PLACED)
    return ClaimedAccountOut(
        id=challenge.public_id,
        platform=cast(PlatformCode, challenge.platform),
        handle=challenge.handle,
        profile_url=challenge.profile_url or None,
        state=cast(VerificationStateCode, state),
        code=challenge.code if (live and challenge.code) else None,
        code_expires_at=challenge.code_expires_at if live else None,
        method=cast(VerificationMethodCode, challenge.method) if challenge.method else None,
        decided_at=challenge.decided_at,
        placed_at=challenge.placed_at,
    )


def status_out(organisation: Organisation) -> VerificationStatusOut:
    now = timezone.now()
    claimant = services.claimant_for(organisation)
    method = verification.method_of(organisation)
    return VerificationStatusOut(
        identity_status=cast(Any, claimant.identity_status),
        method=cast(VerificationMethodCode, method) if method else None,
        code_ttl_hours=int(account_control.CODE_TTL.total_seconds() // 3600),
        platforms=[
            PlatformOut(
                code=cast(PlatformCode, platform.code),
                label=platform.label,
                hosts=list(platform.hosts),
                code_accepted=platform.checks_identity,
            )
            for platform in account_control.PLATFORMS
        ],
        accounts=[
            _account_out(challenge, now=now)
            for challenge in verification.challenges_for(organisation)
        ],
    )


def _organisation(request: HttpRequest) -> tuple[Organisation | None, services.Actor]:
    user = cast(User, request.user)
    return organisation_of(user), services.Actor.customer(user)


def claimed_account(
    organisation: Organisation, challenge_id: str
) -> AccountControlChallenge | None:
    """One claim of this organisation, by public id. Malformed, unknown and foreign give None."""
    if not public_ids.is_valid(CHALLENGE_ID_PREFIX, challenge_id):
        return None
    return AccountControlChallenge.objects.filter(
        organisation=organisation, public_id=challenge_id
    ).first()


def _gate_or_enqueue(
    request: HttpRequest, organisation: Organisation, kind: str, payload: dict[str, Any]
) -> Status[VerificationWriteAcceptedOut] | Status[AppErrorOut] | None:
    """Production takes the write down instead of applying it; `None` on development.

    The same shape as `rights_status._gate_or_enqueue`, and for the same reason: production holds no
    challenge rows of its own, so which claim a write names is resolved where the rows are.
    """
    if not into_outbox():
        return None
    if not writes_available():
        return Status(503, AppErrorOut(detail="verification_write_unavailable"))
    now = timezone.now()
    if outbox.paused(now):
        return Status(503, AppErrorOut(detail="processing_paused"))
    user = cast(User, request.user)
    if (
        upload_throttle.admit(organisation.pk, upload_throttle.Kind.RIGHTS_WRITE, now=now)
        is not None
    ):
        return Status(429, AppErrorOut(detail="too_many_requests"))
    outbox.enqueue(
        kind=kind, organisation_id=organisation.pk, user_id=user.pk, payload=payload, now=now
    )
    return Status(202, VerificationWriteAcceptedOut())


@router.get("", response={200: VerificationStatusOut, 404: AppErrorOut})
def verification_status(
    request: HttpRequest,
) -> Status[VerificationStatusOut] | Status[AppErrorOut]:
    """Which accounts this organisation has claimed, and the code of an open claim."""
    if not verification.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    organisation, _ = _organisation(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    return Status(200, status_out(organisation))


@router.post(
    "/accounts",
    response={
        200: VerificationStatusOut,
        202: VerificationWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def claim_account(
    request: HttpRequest, payload: ClaimAccountIn
) -> Status[VerificationStatusOut] | Status[VerificationWriteAcceptedOut] | Status[AppErrorOut]:
    """Claim one platform account and get the code to place. Claiming it again gives a new code.

    A platform that runs no identity check of its own gets **no code** (`code_accepted` false): a
    code in that page's bio could not establish who the creator is, so staff arrange a video call.

    409 with a code for a handle or link we will not store (`invalid_handle`, `host_not_allowed`,
    `url_not_accepted_for_platform`). 202 on production: accepted into the outbox.
    """
    if not verification.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    organisation, actor = _organisation(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.VERIFICATION_CLAIM,
        payload.model_dump(mode="json"),
    )
    if gate is not None:
        return gate
    try:
        verification.claim_account(
            organisation,
            verification.ClaimInput(
                platform=payload.platform,
                handle=payload.handle,
                profile_url=payload.profile_url,
            ),
            actor,
        )
    except services.RightsError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, status_out(organisation))


@router.post(
    "/accounts/{challenge_id}/placed",
    response={
        200: VerificationStatusOut,
        202: VerificationWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def mark_placed(
    request: HttpRequest, challenge_id: str
) -> Status[VerificationStatusOut] | Status[VerificationWriteAcceptedOut] | Status[AppErrorOut]:
    """ "I have placed the code." Nothing is verified by it; a person still has to look.

    409 `code_expired` once the code has run out — claim the account again for a fresh one.
    202 on production: accepted into the outbox, where the claim is resolved by development.
    """
    if not verification.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    organisation, actor = _organisation(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=CHALLENGE_NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.VERIFICATION_PLACED,
        {"challenge_id": challenge_id},
    )
    if gate is not None:
        return gate
    challenge = claimed_account(organisation, challenge_id)
    if challenge is None:
        return Status(404, AppErrorOut(detail=CHALLENGE_NOT_FOUND))
    try:
        verification.mark_placed(challenge, actor)
    except services.RightsError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, status_out(organisation))
