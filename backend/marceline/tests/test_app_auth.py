"""Customer sign-in: `/api/v1/app/auth/…` (ADR 0023).

The three things this endpoint exists to get right, in the order they matter:

1. **One user model, two authorisations.** A member of an organisation gets a customer session; a
   staff reviewer without a membership does not, and is sent to the review API instead. The same
   account can be both, because the separation is authorisation, not two identity tables.
2. **It is on the public internet.** Per-account lockout, per-IP throttling, and neither of them
   able to answer "does this account exist".
3. **A failed login says one thing only.** Unknown account, wrong password and disabled account are
   byte-identical answers, and each of them pays for a password hash.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth.hashers import get_hasher
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils import timezone

from accounts import login_throttle
from accounts.models import LoginAttempt, Organisation
from marceline.api import api
from marceline.tests.app_helpers import (
    APP,
    add_member,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.models import Reference

AUTH = f"{APP}/auth"
REVIEW_LOGIN = "/api/v1/auth/login"
# The same throwaway fixture password `app_helpers.add_member` sets. Real ones live in nobody's
# repository: the operator sets them with `manage.py changepassword`.
PASSWORD = "secret-pass-123"
WRONG = "not-the-password"
SPRAY_IP = "203.0.113.7"

# Fifteen minutes of failures, in a window of fifteen minutes. Small numbers here so the tests
# exercise the mechanism rather than the constants; `test_the_shipped_limits_are_the_documented_
# ones` pins the defaults.
ACCOUNT_LOCKOUT = override_settings(
    LOGIN_MAX_ACCOUNT_ATTEMPTS=3,
    LOGIN_MAX_IP_ATTEMPTS=100,
    LOGIN_ATTEMPT_WINDOW_MINUTES=15,
    LOGIN_LOCKOUT_MINUTES=15,
    LOGIN_TRUSTED_PROXIES=0,
)
IP_THROTTLE = override_settings(
    LOGIN_MAX_ACCOUNT_ATTEMPTS=100,
    LOGIN_MAX_IP_ATTEMPTS=3,
    LOGIN_ATTEMPT_WINDOW_MINUTES=15,
    LOGIN_LOCKOUT_MINUTES=15,
    LOGIN_TRUSTED_PROXIES=0,
)


@pytest.fixture(autouse=True)
def fast_password_hashing() -> Iterator[None]:
    """Sign-in tests verify real passwords; PBKDF2 on every attempt would dominate the run."""
    with override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]):
        yield


@pytest.fixture
def password_hashes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every password hash computed, to prove none of the failure paths short-circuits."""
    hasher = type(get_hasher("default"))
    calls: list[str] = []

    for name in ("encode", "verify"):
        original = getattr(hasher, name)

        def record(
            self: Any, *args: Any, _name: str = name, _original: Any = original, **kwargs: Any
        ) -> Any:
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(hasher, name, record)
    return calls


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def original(member: User) -> Reference:
    return make_reference(member, title="Alpha's own original")


def post(client: Client, path: str, body: object, token: str | None = None, **extra: Any) -> Any:
    headers = {"X-CSRFToken": token} if token else {}
    return client.post(
        path, json.dumps(body), content_type="application/json", headers=headers, **extra
    )


def sign_in(
    client: Client, username: str, password: str = PASSWORD, token: str | None = None, **extra: Any
) -> Any:
    return post(
        client, f"{AUTH}/login", {"username": username, "password": password}, token, **extra
    )


def test_a_member_signs_in_and_reads_only_their_own_originals(
    organisation: Organisation, member: User, original: Reference
) -> None:
    stranger = add_member(make_organisation("Bravo Creator"), "bravo-owner")
    theirs = make_reference(stranger, title="Bravo's own original")
    client = Client()

    response = sign_in(client, "alpha-owner")

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "alpha-owner"
    assert body["organisation"]["id"] == organisation.public_id
    assert body["organisation"]["name"] == "Alpha Studio"
    assert body["organisation"]["role"] == "owner"
    # A successful login answers exactly what `GET /app/me` answers: one shape for "who am I and
    # what am I scoped to", so the SPA needs no second round trip and no second schema.
    assert body == client.get(f"{APP}/me").json()
    assert [row["id"] for row in client.get(f"{APP}/originals").json()] == [public_id_of(original)]
    assert client.get(f"{APP}/originals/{public_id_of(theirs)}").status_code == 404


def test_the_session_endpoint_hands_out_the_csrf_token_the_login_needs(member: User) -> None:
    """The SPA's existing dance (ADR 0014): read the cookie, echo it back. No token scheme."""
    client = Client(enforce_csrf_checks=True)

    assert sign_in(client, "alpha-owner").status_code == 403

    session = client.get(f"{AUTH}/session")
    assert session.json() == {"authenticated": False, "username": None}
    token = session.cookies["csrftoken"].value

    assert sign_in(client, "alpha-owner", token=token).status_code == 200
    assert client.get(f"{AUTH}/session").json() == {
        "authenticated": True,
        "username": "alpha-owner",
    }

    rotated = client.cookies["csrftoken"].value  # login rotates the token
    assert post(client, f"{AUTH}/logout", {}).status_code == 403
    assert post(client, f"{AUTH}/logout", {}, rotated).status_code == 200
    assert client.get(f"{APP}/me").status_code == 401


def test_staff_without_a_membership_get_the_review_api_instead(staff_user: User) -> None:
    """`is_staff` opens the review API, not a customer account — ADR 0021, from the login side."""
    client = Client()

    refusal = sign_in(client, "reviewer")

    assert refusal.status_code == 403
    assert "operator" in refusal.json()["detail"]
    assert client.get(f"{APP}/me").status_code == 401
    # The same credentials, the same session mechanism, the door that is theirs:
    assert post(client, REVIEW_LOGIN, {"username": "reviewer", "password": PASSWORD}).json() == {
        "authenticated": True,
        "username": "reviewer",
        "is_staff": True,
    }
    assert client.get("/api/v1/review/counts").status_code == 200


def test_one_account_can_be_both_when_it_has_both(organisation: Organisation) -> None:
    """The point of one user model: being staff is not a different kind of account."""
    add_member(organisation, "alpha-admin", is_staff=True)

    assert sign_in(Client(), "alpha-admin").status_code == 200
    assert (
        post(Client(), REVIEW_LOGIN, {"username": "alpha-admin", "password": PASSWORD}).status_code
        == 200
    )


def test_an_inactive_account_cannot_sign_in(member: User) -> None:
    User.objects.filter(pk=member.pk).update(is_active=False)
    client = Client()

    response = sign_in(client, "alpha-owner")

    assert response.status_code == 401
    assert client.get(f"{APP}/me").status_code == 401


def test_a_failed_login_never_says_whether_the_account_exists(
    member: User, password_hashes: list[str]
) -> None:
    """Unknown, wrong, disabled: one answer, and each of them pays for a password hash."""
    add_member(make_organisation("Charlie Creator"), "charlie-owner")
    User.objects.filter(username="charlie-owner").update(is_active=False)
    client = Client()

    answers = []
    for username, password in (
        ("nobody@example.test", PASSWORD),
        ("alpha-owner", WRONG),
        ("charlie-owner", PASSWORD),
    ):
        password_hashes.clear()
        response = sign_in(client, username, password)
        answers.append((response.status_code, response.json()))
        assert password_hashes, f"{username} answered without hashing a password"

    assert answers[0] == (401, {"detail": "Invalid username or password"})
    assert answers[0] == answers[1] == answers[2]


@ACCOUNT_LOCKOUT
def test_an_account_is_locked_out_after_three_failures(member: User) -> None:
    client = Client()
    for _ in range(3):
        assert sign_in(client, "alpha-owner", WRONG).status_code == 401

    locked = sign_in(client, "alpha-owner")  # the *correct* password, and still refused

    assert locked.status_code == 429
    # Fifteen minutes from the last counted failure, less the second this test took.
    assert 890 <= int(locked.headers["Retry-After"]) <= 900
    assert client.get(f"{APP}/me").status_code == 401
    # A refused attempt is not counted again, so the lockout never grows by being hammered.
    assert LoginAttempt.objects.filter(scope=LoginAttempt.Scope.ACCOUNT).count() == 3

    login_throttle.unlock("alpha-owner")  # what `manage.py unlock_login` does
    assert sign_in(client, "alpha-owner").status_code == 200


@ACCOUNT_LOCKOUT
def test_the_lockout_says_nothing_about_who_exists_either(member: User) -> None:
    client = Client()
    for username in ("alpha-owner", "nobody@example.test"):
        for _ in range(3):
            sign_in(client, username, WRONG)

    real = sign_in(client, "alpha-owner", WRONG)
    imaginary = sign_in(client, "nobody@example.test", WRONG)

    assert real.status_code == 429
    assert (real.status_code, real.json()) == (imaginary.status_code, imaginary.json())


@ACCOUNT_LOCKOUT
def test_the_lockout_expires_after_the_documented_window(member: User) -> None:
    client = Client()
    for _ in range(3):
        sign_in(client, "alpha-owner", WRONG)
    assert sign_in(client, "alpha-owner").status_code == 429

    LoginAttempt.objects.update(created_at=timezone.now() - timedelta(minutes=16))

    assert sign_in(client, "alpha-owner").status_code == 200


@IP_THROTTLE
def test_one_address_cannot_spray_many_accounts(member: User) -> None:
    """One attempt each at three usernames is under every account limit; the IP bucket stops it."""
    client = Client()
    for index in range(3):
        sign_in(client, f"victim-{index}@example.test", WRONG, REMOTE_ADDR=SPRAY_IP)

    throttled = sign_in(client, "alpha-owner", REMOTE_ADDR=SPRAY_IP)

    assert throttled.status_code == 429
    assert int(throttled.headers["Retry-After"]) > 0
    # The limit is per address, not a global outage for everyone else.
    assert sign_in(Client(), "alpha-owner", REMOTE_ADDR="198.51.100.9").status_code == 200


def test_the_api_offers_no_way_to_create_an_account() -> None:
    """No self-service signup while onboarding and KYC are open (#50, ADR 0023).

    Absence is the decision, so it is asserted rather than assumed: a future registration endpoint
    has to delete this test, which is exactly the conversation it should trigger. The **password
    reset** was the other half of this test until ADR 0054 (issue #149) lifted that absence with a
    mechanism; the test below names the paths it is allowed to be, so a fifth one still fails here.
    """
    paths = api.get_openapi_schema()["paths"]

    assert not [
        path
        for path in paths
        if any(word in path.lower() for word in ("register", "signup", "sign-up"))
    ]
    assert Client().post(f"{AUTH}/register").status_code == 404
    description = paths[f"{AUTH}/login"]["post"]["description"]
    assert "operator" in description and "no signup" in description


def test_the_only_mailed_account_actions_are_the_four_of_adr_0054() -> None:
    """ADR 0054 lifted ADR 0023's "no password reset" — for exactly these four paths and no more.

    A fifth one, or a `reset` path anywhere else in the customer API, is a new doorway into an
    account and belongs in an ADR before it belongs in a router.
    """
    paths = api.get_openapi_schema()["paths"]
    named = sorted(
        path for path in paths if "reset" in path.lower() or "verify-email" in path.lower()
    )

    assert named == [
        f"{AUTH}/password-reset",
        f"{AUTH}/password-reset/{{token}}",
        f"{AUTH}/verify-email",
        f"{AUTH}/verify-email/{{token}}",
    ]
    # And every one of them is under the customer auth prefix, where the surface allowlist and the
    # throttle already apply (ADR 0054 §2).
    assert all(path.startswith(f"{AUTH}/") for path in named)


def test_the_shipped_limits_and_cookies_are_the_documented_ones() -> None:
    """The numbers ADR 0023 promises, and the flags that make a session cookie safe in prod."""
    assert (settings.LOGIN_MAX_ACCOUNT_ATTEMPTS, settings.LOGIN_MAX_IP_ATTEMPTS) == (5, 20)
    assert (settings.LOGIN_ATTEMPT_WINDOW_MINUTES, settings.LOGIN_LOCKOUT_MINUTES) == (15, 15)
    assert settings.LOGIN_TRUSTED_PROXIES == 0
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_HTTPONLY is False  # the SPA reads it and echoes it back
    assert settings.SESSION_COOKIE_SECURE is settings.IS_PROD
    assert settings.CSRF_COOKIE_SECURE is settings.IS_PROD
