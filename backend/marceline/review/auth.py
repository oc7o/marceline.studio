"""Session login for staff users. CSRF-protected: the SPA reads the `csrftoken` cookie set by
`GET /auth/me` and sends it back as `X-CSRFToken`.

Same door, same lock as the customer login (`marceline/app/session.py`, ADR 0023): one user model,
one session mechanism, and the rate limiting and lockout of `accounts.login_throttle` on both. The
only difference is which authorisation is demanded afterwards — `is_staff` here, an organisation
membership there.
"""

from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from ninja import Router, Schema, Status
from ninja.decorators import decorate_view
from ninja.errors import HttpError
from ninja.security import SessionAuth
from ninja.utils import check_csrf

from accounts import login_throttle
from marceline.review.schemas import ErrorOut


class StaffSessionAuth(SessionAuth):
    """Django session auth (CSRF-checked for unsafe methods); authenticated non-staff get 403."""

    def authenticate(self, request: HttpRequest, key: str | None) -> Any | None:
        user = super().authenticate(request, key)
        if user is None:
            return None
        if not getattr(user, "is_staff", False):
            raise HttpError(403, "Staff only")
        return user


staff_auth = StaffSessionAuth()

router = Router(tags=["auth"])


class LoginIn(Schema):
    username: str
    password: str


class MeOut(Schema):
    authenticated: bool
    username: str | None = None
    is_staff: bool = False


def _me(request: HttpRequest) -> MeOut:
    user = request.user
    if not user.is_authenticated:
        return MeOut(authenticated=False)
    return MeOut(authenticated=True, username=user.get_username(), is_staff=user.is_staff)


def _require_csrf(request: HttpRequest) -> None:
    if check_csrf(request) is not None:
        raise HttpError(403, "CSRF check failed")


@router.get("/me", response=MeOut)
@decorate_view(ensure_csrf_cookie)
def me(request: HttpRequest) -> MeOut:
    """Current session; also sets the CSRF cookie the SPA needs for POSTs."""
    return _me(request)


@router.post("/login", response={200: MeOut, 401: ErrorOut, 403: ErrorOut, 429: ErrorOut})
def login_view(
    request: HttpRequest, payload: LoginIn, response: HttpResponse
) -> Status[MeOut | ErrorOut]:
    """Start a staff session. 429 with `Retry-After` once the account or IP is over its limit."""
    _require_csrf(request)
    # Admitted and counted in one step, so a parallel burst cannot outrun the limit.
    attempt = login_throttle.begin(payload.username, login_throttle.client_ip(request))
    if isinstance(attempt, login_throttle.Refusal):
        response["Retry-After"] = str(attempt.retry_after_s)
        return Status(429, ErrorOut(detail="Too many login attempts. Try again later."))
    user = authenticate(request, username=payload.username, password=payload.password)
    if user is None:  # already counted as a failure
        return Status(401, ErrorOut(detail="Invalid username or password"))
    login_throttle.succeeded(attempt, payload.username)
    if not user.is_staff:
        return Status(403, ErrorOut(detail="Staff only"))
    login(request, user)
    return Status(200, _me(request))


@router.post("/logout", response=MeOut)
def logout_view(request: HttpRequest) -> MeOut:
    _require_csrf(request)
    logout(request)
    return MeOut(authenticated=False)
