"""Password reset and address verification over HTTP (`/api/v1/app/auth/…`, ADR 0054, issue #149).

Three of these routes are reachable **without a session** — the fourth, the resend, is not — so most
of this module is about not becoming an oracle. The rules are the invite's (ADR 0052 §6), applied to
a second doorway:

- **a request for a link answers the same thing whatever happened**: `202 mail_requested` for an
  address with an account, without one, belonging to a staff reviewer, inactive, or when the
  transport refused. There is no branch anywhere that formats another sentence;
- **a link that does not work answers one 404 with one body**: unknown, malformed, wrong kind,
  expired, already used, handed to the outbox, feature off, no transport. `test_account_actions.py`
  asserts the responses are byte-identical;
- **the lookup is one indexed query on the token's digest**, so an unknown link costs what a real
one
  costs;
- **throttling is ADR 0023's**, reused rather than rebuilt (`accounts/login_throttle.py`): the
  account bucket is keyed on the submitted address for a request and on the token's digest for a
  redemption, the IP bucket is the shared one. Nothing new stores an address or a token in the
  clear;
- every write checks CSRF exactly as `app/session.py` does, and `CustomerNoStoreMiddleware` already
  puts `private, no-store` on everything under `/api/v1/app/`.

**On the public host nothing is issued, mailed or set here** (ADR 0054 §1, ADR 0028): a request
and a redemption both go into the outbox and are answered 202, and development — which holds the
accounts, the tokens and the only mail transport — does the work. The timing of the *request* is
therefore constant there: production does not look the address up at all, because it is not
authoritative for whether an account exists (its replica may be fifteen minutes old).
"""

from typing import cast

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from ninja import Router, Status
from ninja.decorators import decorate_view
from ninja.errors import HttpError
from ninja.utils import check_csrf

from accounts import account_actions, action_tokens, login_throttle, passwords, sessions
from accounts.models import AccountActionToken
from delivery import outbox
from delivery.models import OutboxItem
from marceline.app.auth import customer_auth
from marceline.app.ownership import membership_of
from marceline.app.schemas import (
    AccountLanguageIn,
    AccountMailRequestedOut,
    AppErrorOut,
    EmailVerificationStateOut,
    EmailVerifiedOut,
    PasswordProblemsOut,
    PasswordResetDoneOut,
    PasswordResetIn,
    PasswordResetRequestIn,
    PasswordResetStateOut,
)

router = Router(tags=["app"])

TOO_MANY_ATTEMPTS = "Too many attempts. Try again later."
Kind = AccountActionToken.Kind


#: The one answer to a link that does not work, as (status, body). Built at each return site so mypy
#: can widen it; the *content* is this one constant and there is no second one (ADR 0054).
def _refusal() -> tuple[int, AppErrorOut]:
    return 404, AppErrorOut(detail=action_tokens.REFUSED)


def _too_many() -> tuple[int, AppErrorOut]:
    return 429, AppErrorOut(detail=TOO_MANY_ATTEMPTS)


def _accepted() -> AccountMailRequestedOut:
    return AccountMailRequestedOut(detail=action_tokens.ACCEPTED)


def _require_csrf(request: HttpRequest) -> None:
    if check_csrf(request) is not None:
        raise HttpError(403, "CSRF check failed")


def _into_outbox() -> bool:
    """Writes here are accepted into the outbox rather than applied (production, ADR 0028)."""
    return settings.MARCELINE_ROLE == "prod"


def _admit(
    request: HttpRequest, value: str, response: HttpResponse
) -> login_throttle.Attempt | None:
    """Count this attempt against the shared login buckets, or `None` when it is refused.

    The account bucket's value is what the caller supplied — the submitted address, or the submitted
    token's digest — and `login_throttle` HMACs it again before storing it, so nothing here keeps an
    address or a token.
    """
    attempt = login_throttle.begin(value, login_throttle.client_ip(request))
    if isinstance(attempt, login_throttle.Refusal):
        response["Retry-After"] = str(attempt.retry_after_s)
        return None
    return attempt


def _outbox_available() -> tuple[int, AppErrorOut] | None:
    """503 while the public host cannot pass a write on; `None` when it can (or is development)."""
    if not _into_outbox():
        return None
    if not outbox.writes_enabled():
        return 503, AppErrorOut(detail="account_action_unavailable")
    if outbox.paused(timezone.now()):
        return 503, AppErrorOut(detail="processing_paused")
    return None


# --- asking for a reset link --------------------------------------------------------------------


@router.post(
    "/password-reset",
    response={202: AccountMailRequestedOut, 429: AppErrorOut, 503: AppErrorOut},
    auth=None,
)
def request_password_reset(
    request: HttpRequest, payload: PasswordResetRequestIn, response: HttpResponse
) -> Status[AccountMailRequestedOut | AppErrorOut]:
    """Ask for a password-reset link. **The answer says nothing about the address** (ADR 0054 §2).

    202 whether or not it has an account, and nothing in the body or the status differs. 429 once
    the per-address or per-IP limit is reached, which tells a caller only about their own attempts.

    With the flows switched off it is the **same** 202 and **no outbox row**: "off" means nothing
    happens, not a different sentence (issue #153).
    """
    _require_csrf(request)
    # The flag first, and **before anything is written** (issue #153): this route used to go
    # straight to the outbox, so an anonymous caller could put rows into production's queue while
    # the feature was switched off. Off answers the same 202 as every other outcome — §2's neutral
    # answer is this one, not a 404 — and nothing at all happens.
    if not action_tokens.available():
        return Status(202, _accepted())
    address = payload.email.strip()
    attempt = _admit(request, f"reset:{address.lower()}", response)
    if attempt is None:
        return Status(*_too_many())
    unavailable = _outbox_available()
    if unavailable is not None:
        return Status(*unavailable)
    if _into_outbox():
        # Production does not look the address up: its replica is not authoritative (it may be
        # fifteen minutes old), and *not* looking is also what makes the timing identical for every
        # address. Development decides who, if anybody, gets a mail.
        now = timezone.now()
        outbox.enqueue(
            kind=OutboxItem.Kind.PASSWORD_RESET_REQUEST,
            organisation_id=0,
            user_id=0,
            payload={"email": address, "language": payload.language.strip()[:8]},
            now=now,
            dedupe_key=f"reset:{address.lower()}"[:128],
        )
    else:
        account_actions.request_password_reset(address, language=payload.language.strip()[:8])
    return Status(202, _accepted())


# --- using a reset link --------------------------------------------------------------------------


@router.get(
    "/password-reset/{token}",
    response={200: PasswordResetStateOut, 404: AppErrorOut, 429: AppErrorOut},
    auth=None,
)
@decorate_view(ensure_csrf_cookie)
def password_reset_state(
    request: HttpRequest, token: str, response: HttpResponse
) -> Status[PasswordResetStateOut | AppErrorOut]:
    """Whether this link still works, and the CSRF cookie the next step needs.

    404 for every reason it does not — see the module docstring. The address is named because the
    link came out of that inbox: whoever holds it can set the password anyway.
    """
    attempt = _admit(request, _bucket(token), response)
    if attempt is None:
        return Status(*_too_many())
    row = account_actions.lookup(token, Kind.PASSWORD_RESET)
    if row is None:
        return Status(*_refusal())
    login_throttle.succeeded(attempt, _bucket(token))
    return Status(
        200,
        PasswordResetStateOut(
            email=row.user.get_username(), password_min_length=passwords.min_length()
        ),
    )


def _bucket(token: str) -> str:
    """The throttle bucket of one link: its digest, never the token."""
    return f"action:{action_tokens.token_hash(token)}"


@router.post(
    "/password-reset/{token}",
    response={
        200: PasswordResetDoneOut,
        202: PasswordResetDoneOut,
        400: PasswordProblemsOut,
        404: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
    auth=None,
)
def set_password(
    request: HttpRequest, token: str, payload: PasswordResetIn, response: HttpResponse
) -> Status[PasswordResetDoneOut] | Status[PasswordProblemsOut] | Status[AppErrorOut]:
    """Set the new password. **Nobody is signed in** — the next step is the sign-in form.

    The mailed link proves the reader has the inbox, not that they know the password they chose,
    and a session minted from a link is a session a forwarded mail could have. 202 on the public
    host: the password is hashed there and only the hash travels (ADR 0054 §1).
    """
    _require_csrf(request)
    attempt = _admit(request, _bucket(token), response)
    if attempt is None:
        return Status(*_too_many())
    unavailable = _outbox_available()
    if unavailable is not None:
        return Status(*unavailable)
    row = account_actions.lookup(token, Kind.PASSWORD_RESET)
    if row is None:
        return Status(*_refusal())
    login_throttle.succeeded(attempt, _bucket(token))
    problems = passwords.problems(payload.password, row.user)
    if problems:
        return Status(400, PasswordProblemsOut(problems=problems))
    if not _into_outbox():
        account_actions.apply_password_reset(row.token_hash, make_password(payload.password))
        return Status(200, PasswordResetDoneOut(pending=False))
    return _hand_over_password(row, payload.password)


def _hand_over_password(
    row: AccountActionToken, password: str
) -> Status[PasswordResetDoneOut] | Status[AppErrorOut]:
    """Production: the new password's **hash** into the outbox, the link finished, 202.

    Every session of that account ends here and now, in production's own session table (ADR 0054
    §3) — not in fifteen minutes when the new password arrives with `pull_accounts`. The session
    store is production's own data, so this is a local write and not a customer write.
    """
    now = timezone.now()
    with transaction.atomic():
        try:
            locked = account_actions.lock_for_handover(row, now=now)
        except account_actions.ActionRefused:
            return Status(*_refusal())
        item, _ = outbox.enqueue(
            kind=OutboxItem.Kind.PASSWORD_RESET_SET,
            organisation_id=0,
            user_id=0,
            payload={
                "token_hash": locked.token_hash,
                # Hashed **here**, where the password was typed: only the hash travels, and
                # `PASSWORD_HASHERS` is the same list on both hosts (ADR 0054 §1).
                "password_hash": make_password(password),
            },
            now=now,
            dedupe_key=f"reset-set:{locked.token_hash}"[:128],
        )
        account_actions.mark_handed_over(locked, submission_id=item.public_id, now=now)
    sessions.end_sessions(locked.user, now=now)
    return Status(202, PasswordResetDoneOut(pending=True, submission_id=item.public_id))


# --- confirming an address -----------------------------------------------------------------------


@router.get(
    "/verify-email/{token}",
    response={200: EmailVerificationStateOut, 404: AppErrorOut, 429: AppErrorOut},
    auth=None,
)
@decorate_view(ensure_csrf_cookie)
def verification_state(
    request: HttpRequest, token: str, response: HttpResponse
) -> Status[EmailVerificationStateOut | AppErrorOut]:
    """Whether this confirmation link still works, and the CSRF cookie the button needs.

    A **GET confirms nothing**: mail clients, scanners and link previews fetch links on their own,
    and an address confirmed by a scanner is a confirmation nobody made. The POST below is the act.
    """
    attempt = _admit(request, _bucket(token), response)
    if attempt is None:
        return Status(*_too_many())
    row = account_actions.lookup(token, Kind.EMAIL_VERIFICATION)
    if row is None:
        return Status(*_refusal())
    login_throttle.succeeded(attempt, _bucket(token))
    return Status(200, EmailVerificationStateOut(email=row.user.get_username()))


@router.post(
    "/verify-email/{token}",
    response={
        200: EmailVerifiedOut,
        202: EmailVerifiedOut,
        404: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
    auth=None,
)
def confirm_email(
    request: HttpRequest, token: str, response: HttpResponse
) -> Status[EmailVerifiedOut] | Status[AppErrorOut]:
    """Confirm the address. No session needed: the link was mailed to it (ADR 0054 §3)."""
    _require_csrf(request)
    attempt = _admit(request, _bucket(token), response)
    if attempt is None:
        return Status(*_too_many())
    unavailable = _outbox_available()
    if unavailable is not None:
        return Status(*unavailable)
    row = account_actions.lookup(token, Kind.EMAIL_VERIFICATION)
    if row is None:
        return Status(*_refusal())
    login_throttle.succeeded(attempt, _bucket(token))
    if not _into_outbox():
        account_actions.apply_verification(row.token_hash)
        return Status(200, EmailVerifiedOut(verified=True))
    now = timezone.now()
    with transaction.atomic():
        try:
            locked = account_actions.lock_for_handover(row, now=now)
        except account_actions.ActionRefused:
            return Status(*_refusal())
        item, _ = outbox.enqueue(
            kind=OutboxItem.Kind.EMAIL_VERIFY,
            organisation_id=0,
            user_id=0,
            payload={"token_hash": locked.token_hash},
            now=now,
            dedupe_key=f"verify:{locked.token_hash}"[:128],
        )
        account_actions.mark_handed_over(locked, submission_id=item.public_id, now=now)
    return Status(202, EmailVerifiedOut(pending=True, submission_id=item.public_id))


# --- asking for the verification mail again (signed in) ------------------------------------------

signed_in_router = Router(tags=["app"], auth=customer_auth)


@signed_in_router.post(
    "/verify-email",
    response={202: AccountMailRequestedOut, 404: AppErrorOut, 429: AppErrorOut, 503: AppErrorOut},
)
def resend_verification(
    request: HttpRequest, payload: AccountLanguageIn, response: HttpResponse
) -> Status[AccountMailRequestedOut | AppErrorOut]:
    """ "Send that mail again", from the banner. Signed in, so it says nothing about anybody else.

    202 whether a mail went out or `RESEND_INTERVAL` swallowed it: a screen that could tell the
    difference would be a screen that has to explain it. 404 while the flows are off — the same bare
    answer as a route that does not exist, because for this deployment it does not.
    """
    _require_csrf(request)
    if not action_tokens.available():
        return Status(*_refusal())
    user = cast(User, request.user)
    membership = membership_of(user)
    if membership is None:
        return Status(*_refusal())
    attempt = _admit(request, f"verify:{user.pk}", response)
    if attempt is None:
        return Status(*_too_many())
    unavailable = _outbox_available()
    if unavailable is not None:
        return Status(*unavailable)
    language = payload.language.strip()[:8]
    if _into_outbox():
        outbox.enqueue(
            kind=OutboxItem.Kind.EMAIL_VERIFY_RESEND,
            organisation_id=membership.organisation_id,
            user_id=user.pk,
            payload={"language": language},
            now=timezone.now(),
            dedupe_key=f"verify-resend:{user.pk}",
        )
    else:
        account_actions.request_verification(user, language=language)
    return Status(202, _accepted())
