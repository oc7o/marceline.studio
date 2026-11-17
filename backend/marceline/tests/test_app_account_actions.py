"""The mailed account actions over HTTP (`/api/v1/app/auth/…`, ADR 0054, issue #149).

Three of these routes have no session, so most of this file is about what they refuse to say: one
answer for a request whatever the address is, one 404 for every reason a link does not work, CSRF on
every write, and the shared login buckets counting both.

The service's own guarantees are `accounts/tests/test_account_actions.py`; the cross-host journey is
`marceline/tests/test_account_flows_on_production.py`.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core import mail as django_mail
from django.test import Client, override_settings
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts import account_actions, action_tokens, passwords
from accounts.models import AccountActionToken, EmailVerification
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation

pytestmark = pytest.mark.django_db

AUTH = f"{APP}/auth"
RESET = f"{AUTH}/password-reset"
VERIFY = f"{AUTH}/verify-email"
PASSWORD = "a-long-enough-passphrase"
NEW_PASSWORD = "an-even-longer-passphrase"
Kind = AccountActionToken.Kind


@pytest.fixture(autouse=True)
def fast_password_hashing() -> Iterator[None]:
    with override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]):
        yield


@pytest.fixture(autouse=True)
def enabled(settings: Settings) -> None:
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = True
    settings.ONBOARDING_INVITE_BASE_URL = "https://dash.example.invalid"


@pytest.fixture(autouse=True)
def empty_outbox() -> Iterator[None]:
    django_mail.outbox.clear()
    yield
    django_mail.outbox.clear()


@pytest.fixture
def customer() -> User:
    """A customer whose username is their address, as every invited account's is (ADR 0052)."""
    user = add_member(make_organisation("A Studio"), "creator@example.invalid")
    user.email = user.get_username()
    user.set_password(PASSWORD)
    user.save(update_fields=["email", "password"])
    return user


def post(
    client: Client, path: str, body: object = None, token: str | None = None, **extra: Any
) -> Any:
    headers = {"X-CSRFToken": token} if token else {}
    return client.post(
        path, json.dumps(body or {}), content_type="application/json", headers=headers, **extra
    )


def with_csrf(path: str = f"{AUTH}/session") -> tuple[Client, str]:
    client = Client(enforce_csrf_checks=True)
    answer = client.get(path)
    return client, str(answer.cookies["csrftoken"].value)


def mailed_token() -> str:
    """The token out of the one mail that was sent. The only place it exists (ADR 0054)."""
    [sent] = django_mail.outbox
    links = [word for word in sent.body.split() if word.startswith("https://")]
    assert len(links) == 1
    return links[0].rsplit("/", 1)[-1]


# --- asking for a reset -------------------------------------------------------------------------


def test_a_request_answers_the_same_thing_for_every_address(customer: User) -> None:
    """The enumeration rule (ADR 0054 §2): known, unknown and malformed are one answer."""
    client, csrf = with_csrf()

    answers = [
        post(client, RESET, {"email": customer.get_username(), "language": "de"}, csrf),
        post(client, RESET, {"email": "nobody@example.invalid"}, csrf),
        post(client, RESET, {"email": "not-an-address"}, csrf),
        post(client, RESET, {}, csrf),
    ]

    for answer in answers:
        assert answer.status_code == 202, answer.content
        assert answer.content == answers[0].content
        assert answer.json() == {"detail": "mail_requested"}
    # Exactly one of them had an account.
    assert len(django_mail.outbox) == 1
    assert AccountActionToken.objects.count() == 1


def test_a_request_needs_a_csrf_token(customer: User) -> None:
    client = Client(enforce_csrf_checks=True)

    assert post(client, RESET, {"email": customer.get_username()}).status_code == 403
    assert django_mail.outbox == []


def test_a_request_is_throttled_per_address_and_per_ip(customer: User) -> None:
    """ADR 0023's buckets, reused: the address is the caller's own, so a 429 tells them nothing."""
    with override_settings(
        LOGIN_MAX_ACCOUNT_ATTEMPTS=2,
        LOGIN_MAX_IP_ATTEMPTS=100,
        LOGIN_ATTEMPT_WINDOW_MINUTES=15,
        LOGIN_LOCKOUT_MINUTES=15,
        LOGIN_TRUSTED_PROXIES=0,
    ):
        client, csrf = with_csrf()
        body = {"email": customer.get_username()}
        assert post(client, RESET, body, csrf, REMOTE_ADDR="203.0.113.7").status_code == 202
        assert post(client, RESET, body, csrf, REMOTE_ADDR="203.0.113.7").status_code == 202
        refused = post(client, RESET, body, csrf, REMOTE_ADDR="203.0.113.7")

    assert refused.status_code == 429
    assert refused["Retry-After"]


def test_the_flag_off_sends_nothing_and_still_answers_the_same(
    settings: Settings, customer: User
) -> None:
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    client, csrf = with_csrf()

    answer = post(client, RESET, {"email": customer.get_username()}, csrf)

    assert answer.status_code == 202
    assert answer.json() == {"detail": "mail_requested"}
    assert django_mail.outbox == []
    assert not AccountActionToken.objects.exists()


# --- using a reset link --------------------------------------------------------------------------


def test_the_whole_reset_journey(customer: User) -> None:
    client, csrf = with_csrf()
    assert post(client, RESET, {"email": customer.get_username()}, csrf).status_code == 202
    token = mailed_token()

    state = Client().get(f"{RESET}/{token}")
    assert state.status_code == 200
    assert state.json() == {
        "email": customer.get_username(),
        "password_min_length": passwords.min_length(),
    }

    client, csrf = with_csrf(f"{RESET}/{token}")
    done = post(client, f"{RESET}/{token}", {"password": NEW_PASSWORD}, csrf)

    assert done.status_code == 200
    assert done.json() == {"pending": False, "submission_id": ""}
    assert not done.cookies.get("sessionid"), "a mailed link does not sign anybody in"
    customer.refresh_from_db()
    assert customer.check_password(NEW_PASSWORD)
    # The link is finished.
    again = Client().get(f"{RESET}/{token}")
    assert again.status_code == 404


def test_a_weak_password_is_refused_by_code(customer: User) -> None:
    account_actions.request_password_reset(customer.get_username())
    token = mailed_token()
    client, csrf = with_csrf(f"{RESET}/{token}")

    answer = post(client, f"{RESET}/{token}", {"password": "short"}, csrf)

    assert answer.status_code == 400
    assert answer.json()["problems"] == ["password_too_short"]
    customer.refresh_from_db()
    assert customer.check_password(PASSWORD), "nothing changed"


def test_a_password_like_the_address_is_refused(customer: User) -> None:
    """`UserAttributeSimilarityValidator` needs the account, which is why the token names one."""
    account_actions.request_password_reset(customer.get_username())
    token = mailed_token()
    client, csrf = with_csrf(f"{RESET}/{token}")

    answer = post(client, f"{RESET}/{token}", {"password": "creator@example.invalid"}, csrf)

    assert answer.status_code == 400
    assert "password_too_similar" in answer.json()["problems"]


def test_every_reason_a_link_fails_answers_identically(settings: Settings, customer: User) -> None:
    account_actions.request_password_reset(customer.get_username())
    good = mailed_token()
    expired, expired_token = account_actions.issue(customer, Kind.PASSWORD_RESET)
    AccountActionToken.objects.filter(pk=expired.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    _, verification_token = account_actions.issue(customer, Kind.EMAIL_VERIFICATION)
    client = Client()

    answers = [
        client.get(f"{RESET}/{action_tokens.new_token()}"),
        client.get(f"{RESET}/not-a-token-%21"),
        client.get(f"{RESET}/{expired_token}"),
        # A verification link presented as a reset link.
        client.get(f"{RESET}/{verification_token}"),
    ]
    for answer in answers:
        assert answer.status_code == 404, answer.content
        assert answer.content == answers[0].content
        assert answer.json() == {"detail": "link_not_available"}

    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    off = client.get(f"{RESET}/{good}")
    assert off.status_code == 404
    assert off.content == answers[0].content


def test_setting_a_password_needs_a_csrf_token(customer: User) -> None:
    account_actions.request_password_reset(customer.get_username())
    token = mailed_token()
    client = Client(enforce_csrf_checks=True)

    assert post(client, f"{RESET}/{token}", {"password": NEW_PASSWORD}).status_code == 403
    customer.refresh_from_db()
    assert customer.check_password(PASSWORD)


def test_no_customer_answer_may_be_stored_by_a_cache(customer: User) -> None:
    account_actions.request_password_reset(customer.get_username())
    token = mailed_token()

    assert Client().get(f"{RESET}/{token}")["Cache-Control"] == "private, no-store"


# --- confirming an address -----------------------------------------------------------------------


def test_a_get_confirms_nothing_and_a_post_does(customer: User) -> None:
    """Mail scanners fetch links; a confirmation a scanner made is nobody's (ADR 0054)."""
    account_actions.request_verification(customer)
    token = mailed_token()

    state = Client().get(f"{VERIFY}/{token}")

    assert state.status_code == 200
    assert state.json() == {"email": customer.get_username()}
    assert EmailVerification.objects.get(user=customer).verified_at is None

    client, csrf = with_csrf(f"{VERIFY}/{token}")
    done = post(client, f"{VERIFY}/{token}", {}, csrf)

    assert done.status_code == 200
    assert done.json() == {"verified": True, "pending": False, "submission_id": ""}
    assert EmailVerification.objects.get(user=customer).verified_at is not None
    assert Client().get(f"{VERIFY}/{token}").status_code == 404, "single use"


def test_the_banner_can_ask_for_the_mail_again(customer: User) -> None:
    signed_in = client_for(customer)
    signed_in.get(f"{APP}/me")
    csrf = str(signed_in.cookies["csrftoken"].value)

    answer = post(signed_in, VERIFY, {"language": "en"}, csrf)

    assert answer.status_code == 202
    assert answer.json() == {"detail": "mail_requested"}
    assert len(django_mail.outbox) == 1


def test_a_resend_needs_a_session(customer: User) -> None:
    client, csrf = with_csrf()

    assert post(client, VERIFY, {"language": "en"}, csrf).status_code == 401
    assert django_mail.outbox == []


def test_a_resend_is_refused_while_the_flows_are_off(settings: Settings, customer: User) -> None:
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    signed_in = client_for(customer)
    signed_in.get(f"{APP}/me")
    csrf = str(signed_in.cookies["csrftoken"].value)

    answer = post(signed_in, VERIFY, {"language": "en"}, csrf)

    assert answer.status_code == 404
    assert answer.json() == {"detail": "link_not_available"}


# --- what the app is told ------------------------------------------------------------------------


def test_me_reports_the_verification_state(settings: Settings, customer: User) -> None:
    signed_in = client_for(customer)

    unverified = signed_in.get(f"{APP}/me").json()
    assert (unverified["email_verified"], unverified["can_verify_email"]) == (False, True)

    EmailVerification.objects.create(user=customer, verified_at=timezone.now())
    assert signed_in.get(f"{APP}/me").json()["email_verified"] is True

    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    off = signed_in.get(f"{APP}/me").json()
    assert off["can_verify_email"] is False, "the banner's button is hidden, not a dead end"


def test_redeeming_an_invite_asks_for_the_address_to_be_confirmed(
    settings: Settings, django_capture_on_commit_callbacks: Any
) -> None:
    """ADR 0054 §3: staff typed the address, so the first thing the account does is ask.

    The mail is sent from `transaction.on_commit`, so a transport taking seconds never holds the
    invite's row lock — which is why this test runs the commit callbacks to see it (issue #115).
    """
    from accounts import invites

    settings.ONBOARDING_INVITES_ENABLED = True
    invite, _ = invites.issue(email="new@example.invalid", organisation_name="A New Studio")
    invites.set_password(invite, PASSWORD)
    invites.record_declarations(invite)
    settings.ONBOARDING_DOCUMENTS = (
        "pilot_agreement:2026-10-01:" + "a" * 64 + ":https://example.invalid/pilot,"
        "service_privacy_notice:2026-10-01:" + "b" * 64 + ":https://example.invalid/privacy"
    )
    invites.record_acceptances(invite, language="de")
    invites.record_country(invite, "DE")

    with django_capture_on_commit_callbacks(execute=True):
        user = invites.complete(invite, language="de")

    assert len(django_mail.outbox) == 1
    assert django_mail.outbox[0].to == [user.get_username()]
    assert "bestätige" in django_mail.outbox[0].body.lower()
