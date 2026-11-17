"""Signing a customer in and out (`/api/v1/app/auth/…`, ADR 0023).

**One user model, two authorisations.** A customer is a `auth.User` with a `Membership`; a staff
reviewer is a `auth.User` with `is_staff`. The same Django session, the same password hashing and
the same CSRF mechanism serve both, and the only thing this router adds over the staff login
(`marceline/review/auth.py`) is which authorisation it demands: a membership instead of `is_staff`.
Two identity tables would have meant two login paths, two password flows and two session
mechanisms — three more places to get security wrong for a boundary that organisation membership
and the 404 rule (ADR 0021) already enforce.

What this router does *not* do, on purpose:

- **no signup.** Accounts are created by the operator (admin or `manage.py`) while onboarding is
  by invitation (issue #50). There is no registration endpoint, and a test asserts the
  API exposes none — the absence is a decision, not an oversight;
- **no password reset.** The operator sets passwords with `manage.py changepassword` (ADR 0023);
- **no token scheme.** A session cookie is what the SPA already speaks (ADR 0014).

A failed login says exactly one thing, whatever went wrong: unknown account, wrong password and
disabled account all produce the same 401 and the same body, and all three pay the cost of a
password hash so the answer does not arrive noticeably sooner for a username that does not exist.
Throttling and lockout live in `accounts.login_throttle`, shared with the staff login.
"""

from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from ninja import Router, Status
from ninja.decorators import decorate_view
from ninja.errors import HttpError
from ninja.utils import check_csrf

from accounts import login_throttle
from marceline.app.account import account_out
from marceline.app.ownership import membership_of
from marceline.app.schemas import (
    AccountOut,
    AppErrorOut,
    CustomerLoginIn,
    CustomerSessionOut,
)

router = Router(tags=["app"])

# One refusal for every credential failure. Never make these differ: which of them applies is
# precisely what a stranger must not learn.
INVALID_CREDENTIALS = "Invalid username or password"
# Said only after the password was correct, so it tells a stranger nothing they did not supply.
NO_CUSTOMER_ACCOUNT = (
    "This account has no customer organisation. Customer accounts are created by the operator."
)
TOO_MANY_ATTEMPTS = "Too many login attempts. Try again later."


def _require_csrf(request: HttpRequest) -> None:
    """The SPA reads the `csrftoken` cookie and echoes it as `X-CSRFToken`, as staff auth does."""
    if check_csrf(request) is not None:
        raise HttpError(403, "CSRF check failed")


@router.get("/session", response=CustomerSessionOut, auth=None)
@decorate_view(ensure_csrf_cookie)
def session(request: HttpRequest) -> CustomerSessionOut:
    """Whether this browser is signed in; also sets the CSRF cookie the login POST needs.

    Unauthenticated on purpose — a sign-in form has to be able to obtain a CSRF token before it has
    a session. It reports only on the caller's own session and never mentions an organisation;
    `GET /api/v1/app/me` is where scope is answered, and that one requires a session.
    """
    user = request.user
    if not user.is_authenticated:
        return CustomerSessionOut(authenticated=False)
    return CustomerSessionOut(authenticated=True, username=user.get_username())


@router.post(
    "/login",
    response={200: AccountOut, 401: AppErrorOut, 403: AppErrorOut, 429: AppErrorOut},
    auth=None,
)
def login_view(
    request: HttpRequest, payload: CustomerLoginIn, response: HttpResponse
) -> Status[AccountOut | AppErrorOut]:
    """Start a customer session. Accounts are created by the operator; there is no signup.

    401 for any credential failure, 403 when the credentials were right but the account belongs to
    no organisation (a staff reviewer, say — `is_staff` opens the review API, not a customer's
    account), 429 once the per-account or per-IP limit is reached, with `Retry-After`.
    """
    _require_csrf(request)
    # Admitted and counted in one step, so a parallel burst cannot outrun the limit.
    attempt = login_throttle.begin(payload.username, login_throttle.client_ip(request))
    if isinstance(attempt, login_throttle.Refusal):
        response["Retry-After"] = str(attempt.retry_after_s)
        return Status(429, AppErrorOut(detail=TOO_MANY_ATTEMPTS))
    # Django's ModelBackend hashes the submitted password even when the username is unknown, and
    # refuses an inactive account the same way it refuses a wrong password. Both are why all three
    # failures answer identically below. A failure is already counted.
    user = authenticate(request, username=payload.username, password=payload.password)
    if user is None:
        return Status(401, AppErrorOut(detail=INVALID_CREDENTIALS))
    login_throttle.succeeded(attempt, payload.username)
    membership = membership_of(user)
    if membership is None:
        return Status(403, AppErrorOut(detail=NO_CUSTOMER_ACCOUNT))
    login(request, user)  # rotates the session key and the CSRF token
    return Status(200, account_out(user))


@router.post("/logout", response=CustomerSessionOut, auth=None)
def logout_view(request: HttpRequest) -> CustomerSessionOut:
    """End the session. CSRF-protected, and harmless (200) when there was no session."""
    _require_csrf(request)
    logout(request)
    return CustomerSessionOut(authenticated=False)
