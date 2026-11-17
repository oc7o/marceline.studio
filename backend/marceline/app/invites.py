"""Redeeming an invite (`/api/v1/app/invite/…`, ADR 0052, issue #115).

**The only customer surface reachable without a session**, which is why almost everything in this
module is about not becoming an oracle:

- every failure — unknown token, malformed token, expired, revoked, already redeemed, feature off
  — answers the **same 404 with the same body**. There is no branch that formats another sentence,
  and `test_app_invites.py` asserts the responses are byte-identical;
- the lookup is one indexed query on the token's digest, so an unknown token costs what a real one
  costs. No path does more work before answering;
- throttling is ADR 0023's, reused rather than rebuilt (`accounts/login_throttle.py`): the account
  bucket is keyed on the **submitted token's digest** and the IP bucket is the shared one, so
  nothing new stores a token or an address in the clear;
- every write checks CSRF exactly as `app/session.py` does, and `CustomerNoStoreMiddleware`
  already puts `private, no-store` on everything under `/api/v1/app/`.

This router is registered under one prefix on purpose, and since ADR 0052 §9 was built (slice 2,
2026-09-19) that prefix is **no longer** in `public_surface.ROLE_ONLY_PREFIXES`: the redemption
works on the public host, where the last step creates nothing and hands the finished redemption to
`delivery.OutboxItem` instead (`_hand_over`). Everything above still holds there — the same one 404,
the same buckets, the same CSRF — and `ONBOARDING_DOCUMENTS` still refuses the accept step while it
is empty, because production reads the same setting.
"""

from django.conf import settings
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from ninja import Router, Status
from ninja.decorators import decorate_view
from ninja.errors import HttpError
from ninja.utils import check_csrf

from accounts import business_status as business_status_rules
from accounts import invites, login_throttle, onboarding
from accounts.declarations import (
    CURRENT_AGE_DECLARATION,
    CURRENT_BUSINESS_STATUS_DECLARATION,
    OPERATIVE_LANGUAGE,
)
from accounts.models import CustomerInvite
from accounts.onboarding import REFUSED, Step
from delivery import outbox
from delivery.models import OutboxItem
from marceline.app.account import account_out
from marceline.app.schemas import (
    AccountOut,
    AppErrorOut,
    InviteAcceptedOut,
    InviteAcceptIn,
    InviteCountryIn,
    InviteDeclarationIn,
    InviteDocumentOut,
    InvitePasswordIn,
    InviteProblemsOut,
    InviteStateOut,
)
from marceline.rights import account_control_services

router = Router(tags=["app"])

TOO_MANY_ATTEMPTS = "Too many attempts. Try again later."
#: Sorted so the select box is stable and the order says nothing about anything.
EU_COUNTRIES = tuple(sorted(business_status_rules.EU_MEMBER_STATES))


#: The one answer, as (status, body). Built at each return site so mypy can widen it to the
#: endpoint's own response union; the *content* is this one constant, and there is no second one
#: (ADR 0052 §6).
def _refusal() -> tuple[int, AppErrorOut]:
    return 404, AppErrorOut(detail=REFUSED)


def _too_many() -> tuple[int, AppErrorOut]:
    return 429, AppErrorOut(detail=TOO_MANY_ATTEMPTS)


def _require_csrf(request: HttpRequest) -> None:
    if check_csrf(request) is not None:
        raise HttpError(403, "CSRF check failed")


def _admit(
    request: HttpRequest, token: str, response: HttpResponse
) -> login_throttle.Attempt | None:
    """Count this attempt against the shared login buckets, or `None` when it is refused.

    The account bucket's value is the token's digest, so guessing one token is limited wherever the
    guesses come from, and the digest is HMAC'd again by `login_throttle` before it is stored.
    """
    attempt = login_throttle.begin(
        f"invite:{onboarding.token_hash(token)}", login_throttle.client_ip(request)
    )
    if isinstance(attempt, login_throttle.Refusal):
        response["Retry-After"] = str(attempt.retry_after_s)
        return None
    return attempt


def _state(invite: CustomerInvite, *, outside_eu: bool = False) -> InviteStateOut:
    """What the screen needs for the step it is on. Never why anything else failed."""
    try:
        documents = onboarding.published_documents()
        published = True
    except onboarding.DocumentsNotPublished:
        documents = ()
        published = False
    declaration = CURRENT_BUSINESS_STATUS_DECLARATION
    return InviteStateOut(
        step=invites.step_of(invite).value,
        email=invite.email,
        organisation_name=(
            invite.organisation.name
            if invite.organisation is not None
            else invite.organisation_name
        ),
        documents=[
            InviteDocumentOut(
                document_id=document.document_id,
                version=document.version,
                sha256=document.sha256,
                url=document.url,
            )
            for document in documents
        ],
        documents_published=published,
        declaration_version=declaration.version,
        declaration_text=declaration.text(),
        age_declaration_version=CURRENT_AGE_DECLARATION.version,
        age_declaration_text=CURRENT_AGE_DECLARATION.text(),
        declaration_language=OPERATIVE_LANGUAGE,
        password_min_length=invites.password_min_length(),
        eu_countries=list(EU_COUNTRIES),
        outside_eu=outside_eu,
        # ADR 0055: whether the finished panel names the account-control step as what comes next.
        # A fact about this deployment's switch, not about the token — it is the same for every
        # caller, so it tells nobody anything about the invite they are holding.
        account_step_next=account_control_services.available(),
    )


@router.get(
    "/{token}", response={200: InviteStateOut, 404: AppErrorOut, 429: AppErrorOut}, auth=None
)
@decorate_view(ensure_csrf_cookie)
def invite_state(
    request: HttpRequest, token: str, response: HttpResponse
) -> Status[InviteStateOut | AppErrorOut]:
    """Where this redemption stands, and the CSRF cookie every step after it needs.

    404 for every reason a token does not work — see the module docstring.
    """
    attempt = _admit(request, token, response)
    if attempt is None:
        return Status(*_too_many())
    invite = invites.lookup(token)
    if invite is None:
        return Status(*_refusal())
    login_throttle.succeeded(attempt, f"invite:{onboarding.token_hash(token)}")
    return Status(200, _state(invite))


def _step(
    request: HttpRequest, token: str, response: HttpResponse, expected: Step
) -> CustomerInvite | tuple[int, AppErrorOut]:
    """Shared preamble: CSRF, throttle, lookup, and "is this the step you are on".

    A step answered out of order is refused with `REFUSED` like anything else: the flow is
    resumable, so the screen always knows which step it is on, and a request for another one is
    either a stale tab or somebody poking.
    """
    _require_csrf(request)
    attempt = _admit(request, token, response)
    if attempt is None:
        return _too_many()
    invite = invites.lookup(token)
    if invite is None or invites.step_of(invite) is not expected:
        return _refusal()
    login_throttle.succeeded(attempt, f"invite:{onboarding.token_hash(token)}")
    return invite


@router.post(
    "/{token}/password",
    response={200: InviteStateOut, 400: InviteProblemsOut, 404: AppErrorOut, 429: AppErrorOut},
    auth=None,
)
def invite_password(
    request: HttpRequest, token: str, payload: InvitePasswordIn, response: HttpResponse
) -> Status[InviteStateOut | InviteProblemsOut | AppErrorOut]:
    """Step one. Django's own validators decide; the username is the address on the invite."""
    invite = _step(request, token, response, Step.PASSWORD)
    if isinstance(invite, tuple):
        return Status(*invite)
    problems = invites.password_problems(payload.password, invite.email)
    if problems:
        return Status(400, InviteProblemsOut(problems=problems))
    invites.set_password(invite, payload.password)
    return Status(200, _state(invite))


@router.post(
    "/{token}/declaration",
    response={200: InviteStateOut, 400: InviteProblemsOut, 404: AppErrorOut, 429: AppErrorOut},
    auth=None,
)
def invite_declaration(
    request: HttpRequest, token: str, payload: InviteDeclarationIn, response: HttpResponse
) -> Status[InviteStateOut | InviteProblemsOut | AppErrorOut]:
    """Step two: both versioned wordings, stored verbatim with their versions.

    The *Unternehmer* declaration (§ 14 BGB, issue #92) and the declaration of majority and full
    legal capacity (issue #123). Two ticks, one step, one moment — and an unticked one is named, so
    somebody who missed a box is told which.

    The evidence behind the first stays a staff step (issue #92): a tick converts nobody, and the
    screen says so rather than pretending the account is finished. The second is a declaration by
    decision and not a verification: **no date of birth is asked for and no document is accepted**.

    A version the screen displayed that is no longer current answers `declaration_version_outdated`
    — the same code the rights declarations use. The screen reloads and shows today's wording.
    """
    invite = _step(request, token, response, Step.DECLARATION)
    if isinstance(invite, tuple):
        return Status(*invite)
    problems: list[str] = []
    if not payload.accepted:
        problems.append("declaration_required")
    if not payload.age_accepted:
        problems.append("age_declaration_required")
    if (
        payload.declaration_version != CURRENT_BUSINESS_STATUS_DECLARATION.version
        or payload.age_declaration_version != CURRENT_AGE_DECLARATION.version
    ):
        problems.append("declaration_version_outdated")
    if problems:
        return Status(400, InviteProblemsOut(problems=problems))
    invites.record_declarations(invite)
    return Status(200, _state(invite))


@router.post(
    "/{token}/documents",
    response={200: InviteStateOut, 400: InviteProblemsOut, 404: AppErrorOut, 429: AppErrorOut},
    auth=None,
)
def invite_documents(
    request: HttpRequest, token: str, payload: InviteAcceptIn, response: HttpResponse
) -> Status[InviteStateOut | InviteProblemsOut | AppErrorOut]:
    """Step three: the pilot agreement and the service privacy notice, one acceptance row each.

    `documents_not_published` while `ONBOARDING_DOCUMENTS` is empty — the gate that keeps this
    flow behind the legal work. It is a problem code, not a token refusal: the token is fine and
    the person should be told to come back, not shown the neutral page.
    """
    invite = _step(request, token, response, Step.DOCUMENTS)
    if isinstance(invite, tuple):
        return Status(*invite)
    try:
        required = {document.document_id for document in onboarding.published_documents()}
    except onboarding.DocumentsNotPublished:
        return Status(400, InviteProblemsOut(problems=["documents_not_published"]))
    if not required.issubset(set(payload.accepted)):
        return Status(400, InviteProblemsOut(problems=["all_documents_required"]))
    invites.record_acceptances(invite, language=payload.language.strip()[:8])
    return Status(200, _state(invite))


@router.post(
    "/{token}/country",
    response={200: InviteStateOut, 400: InviteProblemsOut, 404: AppErrorOut, 429: AppErrorOut},
    auth=None,
)
def invite_country(
    request: HttpRequest, token: str, payload: InviteCountryIn, response: HttpResponse
) -> Status[InviteStateOut | InviteProblemsOut | AppErrorOut]:
    """Step four: the EU-only scope (issue #95 item 7).

    A country outside the EU answers 200 with `outside_eu` set — the screen ends politely and no
    account is created. **The invite is not consumed** and only a count is kept, never which
    country it was (ADR 0052 §5): a typo on a select box must not cost somebody their invitation.
    """
    invite = _step(request, token, response, Step.COUNTRY)
    if isinstance(invite, tuple):
        return Status(*invite)
    if not business_status_rules.COUNTRY_CODE.match(payload.country.strip().upper()):
        return Status(400, InviteProblemsOut(problems=["country_required"]))
    inside = invites.record_country(invite, payload.country)
    return Status(200, _state(invite, outside_eu=not inside))


def _into_outbox() -> bool:
    """The redemption is handed to the outbox here rather than applied (production, ADR 0052 §9)."""
    return settings.MARCELINE_ROLE == "prod"


def _hand_over(invite: CustomerInvite) -> Status[InviteAcceptedOut] | Status[AppErrorOut]:
    """Production's last step: one outbox item, 202, and this link is finished (ADR 0052 §9).

    Everything happens in one transaction, so two submissions produce **one** item: the second is
    refused by `lock_for_handover` and answers the one neutral 404, exactly as a link redeemed on
    development does. Nothing is created here — the account appears when development applies the
    item, and `pull_accounts` carries it back within its cycle.
    """
    now = timezone.now()
    if not outbox.writes_enabled():
        return Status(503, AppErrorOut(detail="invite_write_unavailable"))
    if outbox.paused(now):
        return Status(503, AppErrorOut(detail="processing_paused"))
    with transaction.atomic():
        try:
            locked = invites.lock_for_handover(invite, now=now)
        except invites.InviteRefused:
            return Status(*_refusal())
        redemption = invites.redemption_of(locked, now=now)
        item, _ = outbox.enqueue(
            kind=OutboxItem.Kind.INVITE_REDEEM,
            # Before an account exists there is no organisation and no user to name (ADR 0052 §9):
            # the item belongs to the invite, which its payload names by digest.
            organisation_id=0,
            user_id=0,
            payload=redemption.as_payload(),
            now=now,
            dedupe_key=f"invite:{locked.token_hash}",
        )
        invites.mark_handed_over(locked, submission_id=item.public_id, now=now)
    return Status(202, InviteAcceptedOut(submission_id=item.public_id))


@router.post(
    "/{token}/complete",
    response={
        200: AccountOut,
        202: InviteAcceptedOut,
        400: InviteProblemsOut,
        404: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
    auth=None,
)
def invite_complete(
    request: HttpRequest, token: str, response: HttpResponse
) -> (
    Status[AccountOut] | Status[InviteAcceptedOut] | Status[InviteProblemsOut] | Status[AppErrorOut]
):
    """The last step: the account appears and the browser is signed in (ADR 0052 §8).

    `business_status_not_evidenced` when the operator has set the issue #92 guard to `refuse` — the
    honest answer is that the account is being set up by hand, and the invite stays open so that
    the same link finishes the job once staff have recorded the evidence.

    **On production nothing is created**: the redemption goes into the outbox and the answer is 202
    (ADR 0052 §9). The creator is told the account is being set up, never that it exists, and the
    browser is not signed in — there is no account to sign in to yet.
    """
    invite = _step(request, token, response, Step.COMPLETE)
    if isinstance(invite, tuple):
        return Status(*invite)
    if _into_outbox():
        return _hand_over(invite)
    try:
        user = invites.complete(invite)
    except invites.InviteRefused:
        return Status(*_refusal())
    except invites.AccountAlreadyExists:
        # Logged at ERROR in `invites.complete`: this invite can never finish and a human must look.
        return Status(*_refusal())
    except ValidationError:
        return Status(400, InviteProblemsOut(problems=["business_status_not_evidenced"]))
    # No `authenticate()` ran (there was no password to check — the hash came off the invite), so
    # the backend has to be named explicitly. `login` still rotates the session key and the CSRF
    # token, exactly as the sign-in does.
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return Status(200, account_out(user))
