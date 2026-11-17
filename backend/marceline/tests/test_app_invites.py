"""Redeeming an invite over HTTP: `/api/v1/app/invite/…` (issue #115, ADR 0052).

The one customer surface reachable without a session, so most of this is about what it refuses to
say. The service's own guarantees (single use, expiry, what is stored) are
`accounts/tests/test_invites.py`.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts import invites, onboarding
from accounts.declarations import CURRENT_AGE_DECLARATION, CURRENT_BUSINESS_STATUS_DECLARATION
from accounts.models import CustomerInvite, DocumentAcceptance, Membership, Organisation
from marceline import public_surface
from marceline.tests.app_helpers import APP

INVITE = f"{APP}/invite"
PASSWORD = "a-long-enough-passphrase"
DOCUMENTS = (
    "pilot_agreement:2026-10-01:" + "a" * 64 + ":https://example.invalid/pilot,"
    "service_privacy_notice:2026-10-01:" + "b" * 64 + ":https://example.invalid/privacy"
)
#: A correctly answered declaration step: both ticks, and the version the screen displayed for each
#: (issue #123). The versions travel back so a stale page cannot sign wording nobody is showing.
DECLARATIONS: dict[str, object] = {
    "accepted": True,
    "declaration_version": CURRENT_BUSINESS_STATUS_DECLARATION.version,
    "age_accepted": True,
    "age_declaration_version": CURRENT_AGE_DECLARATION.version,
}


@pytest.fixture(autouse=True)
def fast_password_hashing() -> Iterator[None]:
    """The redemption hashes a real password; PBKDF2 would dominate this file's run."""
    with override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]):
        yield


@pytest.fixture(autouse=True)
def enabled(settings: Settings) -> None:
    settings.ONBOARDING_INVITES_ENABLED = True
    settings.ONBOARDING_DOCUMENTS = DOCUMENTS


@pytest.fixture
def issued(db: None) -> tuple[CustomerInvite, str]:
    return invites.issue(email="creator@example.invalid", organisation_name="A New Studio")


def post(client: Client, path: str, body: object, token: str | None = None, **extra: Any) -> Any:
    headers = {"X-CSRFToken": token} if token else {}
    return client.post(
        path, json.dumps(body), content_type="application/json", headers=headers, **extra
    )


def walk(client: Client, token: str, csrf: str, country: str = "DE") -> Any:
    """The whole sequence, in order, with a real CSRF token."""
    assert (
        post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf).status_code == 200
    )
    assert post(client, f"{INVITE}/{token}/declaration", DECLARATIONS, csrf).status_code == 200
    documents = {"accepted": list(onboarding.REQUIRED_DOCUMENTS), "language": "de"}
    assert post(client, f"{INVITE}/{token}/documents", documents, csrf).status_code == 200
    assert post(client, f"{INVITE}/{token}/country", {"country": country}, csrf).status_code == 200
    return post(client, f"{INVITE}/{token}/complete", {}, csrf)


def open_flow(issued: tuple[CustomerInvite, str]) -> tuple[Client, str, str]:
    """A client that has read the state (and therefore holds the CSRF cookie)."""
    client = Client(enforce_csrf_checks=True)
    _, token = issued
    state = client.get(f"{INVITE}/{token}")
    assert state.status_code == 200
    return client, token, state.cookies["csrftoken"].value


# --- the one neutral answer ---------------------------------------------------------------------


def test_every_reason_a_token_fails_answers_identically(
    issued: tuple[CustomerInvite, str], settings: Settings
) -> None:
    """Unknown, malformed, expired, revoked, redeemed and switched-off must be one answer.

    Byte-identical, because anything else tells whoever holds the link something about whether an
    address was invited (ADR 0052 §6).
    """
    client = Client()
    _, good = issued

    unknown = client.get(f"{INVITE}/{onboarding.new_token()}")
    malformed = client.get(f"{INVITE}/not-a-valid-token-at-all-%21")

    _, expired_token = invites.issue(
        email="b@example.invalid",
        organisation_name="B",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    revoked, revoked_token = invites.issue(email="c@example.invalid", organisation_name="C")
    invites.revoke(revoked)

    answers = [
        unknown,
        malformed,
        client.get(f"{INVITE}/{expired_token}"),
        client.get(f"{INVITE}/{revoked_token}"),
    ]
    for answer in answers:
        assert answer.status_code == 404, answer.content
        assert answer.content == answers[0].content

    settings.ONBOARDING_INVITES_ENABLED = False
    off = client.get(f"{INVITE}/{good}")
    assert off.status_code == 404
    assert off.content == answers[0].content


def test_a_used_invite_answers_the_same_page(issued: tuple[CustomerInvite, str]) -> None:
    client, token, csrf = open_flow(issued)
    assert walk(client, token, csrf).status_code == 200
    again = Client().get(f"{INVITE}/{token}")
    unknown = Client().get(f"{INVITE}/{onboarding.new_token()}")
    assert again.status_code == 404
    assert again.content == unknown.content


def test_the_declaration_step_answers_the_one_404_whatever_the_body_says(
    issued: tuple[CustomerInvite, str],
) -> None:
    """The fields issue #123 added must not become an earlier, different refusal (ADR 0052 §6).

    Every field carries a default precisely so the schema never rejects a body before the view has
    looked at the token: a body missing everything and a complete one answer the same neutral 404.
    Before issue #123, `accepted` was required and an empty body answered 422 for *any* token.
    """
    client, _, csrf = open_flow(issued)
    unknown = onboarding.new_token()
    bare = post(client, f"{INVITE}/{unknown}/declaration", {}, csrf)
    full = post(client, f"{INVITE}/{unknown}/declaration", DECLARATIONS, csrf)
    neutral = Client().get(f"{INVITE}/{onboarding.new_token()}")
    assert bare.status_code == full.status_code == 404
    assert bare.content == full.content == neutral.content


def test_a_step_out_of_order_is_the_same_refusal(issued: tuple[CustomerInvite, str]) -> None:
    client, token, csrf = open_flow(issued)
    early = post(client, f"{INVITE}/{token}/complete", {}, csrf)
    unknown = Client().get(f"{INVITE}/{onboarding.new_token()}")
    assert early.status_code == 404
    assert early.content == unknown.content


# --- the walk through ---------------------------------------------------------------------------


def test_the_whole_sequence_ends_signed_in(issued: tuple[CustomerInvite, str]) -> None:
    client, token, csrf = open_flow(issued)
    done = walk(client, token, csrf)
    assert done.status_code == 200
    body = done.json()
    assert body["username"] == "creator@example.invalid"
    assert body["organisation"]["name"] == "A New Studio"
    # The session really is a customer session: `/app/me` answers it.
    assert client.get(f"{APP}/me").status_code == 200
    user = User.objects.get(username="creator@example.invalid")
    assert Membership.objects.filter(user=user).count() == 1
    assert DocumentAcceptance.objects.filter(user=user).count() == 2


def test_the_state_says_which_step_and_carries_no_secret(
    issued: tuple[CustomerInvite, str],
) -> None:
    client, token, _ = open_flow(issued)
    state = client.get(f"{INVITE}/{token}").json()
    assert state["step"] == "password"
    assert state["email"] == "creator@example.invalid"
    assert state["documents_published"] is True
    assert {document["document_id"] for document in state["documents"]} == set(
        onboarding.REQUIRED_DOCUMENTS
    )
    assert state["declaration_version"]
    assert "DE" in state["eu_countries"]
    # Nothing about the token, the note or who invited: the screen needs none of it.
    assert "token" not in json.dumps(state)
    assert "note" not in state


def test_every_step_is_resumable(issued: tuple[CustomerInvite, str]) -> None:
    client, token, csrf = open_flow(issued)
    assert (
        post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf).status_code == 200
    )
    # A different browser opens the same link and lands on the step that is next.
    later = Client(enforce_csrf_checks=True)
    state = later.get(f"{INVITE}/{token}")
    assert state.json()["step"] == "declaration"


def test_a_weak_password_is_refused_with_codes(issued: tuple[CustomerInvite, str]) -> None:
    client, token, csrf = open_flow(issued)
    refusal = post(client, f"{INVITE}/{token}/password", {"password": "1234"}, csrf)
    assert refusal.status_code == 400
    assert all(code.startswith("password_") for code in refusal.json()["problems"])


def test_an_unticked_declaration_is_refused(issued: tuple[CustomerInvite, str]) -> None:
    client, token, csrf = open_flow(issued)
    post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf)
    body = {**DECLARATIONS, "accepted": False}
    refusal = post(client, f"{INVITE}/{token}/declaration", body, csrf)
    assert refusal.status_code == 400
    assert refusal.json()["problems"] == ["declaration_required"]


def test_an_unticked_age_declaration_is_refused(issued: tuple[CustomerInvite, str]) -> None:
    """Issue #123: the majority declaration is required, and the refusal names *which* box."""
    client, token, csrf = open_flow(issued)
    post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf)
    body = {**DECLARATIONS, "age_accepted": False}
    refusal = post(client, f"{INVITE}/{token}/declaration", body, csrf)
    assert refusal.status_code == 400
    assert refusal.json()["problems"] == ["age_declaration_required"]
    invite = CustomerInvite.objects.get(pk=issued[0].pk)
    assert invite.age_declaration_version == ""
    assert invite.declaration_version == "", "neither is recorded when one box is empty"
    # And the redemption stays on this step: nothing may be completed without it.
    assert client.get(f"{INVITE}/{token}").json()["step"] == "declaration"


def test_a_declaration_of_an_old_version_is_refused(issued: tuple[CustomerInvite, str]) -> None:
    """A page opened before the wording changed cannot sign the wording it is showing."""
    client, token, csrf = open_flow(issued)
    post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf)
    body = {**DECLARATIONS, "age_declaration_version": "volljaehrigkeit-1999-01-01"}
    refusal = post(client, f"{INVITE}/{token}/declaration", body, csrf)
    assert refusal.status_code == 400
    assert refusal.json()["problems"] == ["declaration_version_outdated"]
    assert CustomerInvite.objects.get(pk=issued[0].pk).age_declaration_version == ""


def test_the_state_carries_both_wordings_and_no_birth_date_field(
    issued: tuple[CustomerInvite, str],
) -> None:
    client, token, _ = open_flow(issued)
    state = client.get(f"{INVITE}/{token}").json()
    assert state["age_declaration_version"] == CURRENT_AGE_DECLARATION.version
    assert state["age_declaration_text"] == CURRENT_AGE_DECLARATION.text()
    assert state["declaration_language"] == "de", "the German text is the operative one"
    # A declaration, not a verification: the screen is never asked for a date of birth.
    assert "birth" not in json.dumps(state).lower()


def test_accepting_only_one_document_is_refused(issued: tuple[CustomerInvite, str]) -> None:
    client, token, csrf = open_flow(issued)
    post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf)
    post(client, f"{INVITE}/{token}/declaration", DECLARATIONS, csrf)
    refusal = post(client, f"{INVITE}/{token}/documents", {"accepted": ["pilot_agreement"]}, csrf)
    assert refusal.status_code == 400
    assert refusal.json()["problems"] == ["all_documents_required"]


def test_no_published_document_version_refuses_the_accept_step(
    issued: tuple[CustomerInvite, str], settings: Settings
) -> None:
    """The gate that keeps the flow behind the unreviewed legal drafts (ADR 0052 §3)."""
    settings.ONBOARDING_DOCUMENTS = ""
    client, token, csrf = open_flow(issued)
    assert client.get(f"{INVITE}/{token}").json()["documents_published"] is False
    post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf)
    post(client, f"{INVITE}/{token}/declaration", DECLARATIONS, csrf)
    refusal = post(
        client,
        f"{INVITE}/{token}/documents",
        {"accepted": list(onboarding.REQUIRED_DOCUMENTS)},
        csrf,
    )
    assert refusal.status_code == 400
    assert refusal.json()["problems"] == ["documents_not_published"]
    assert not User.objects.filter(username="creator@example.invalid").exists()


def test_a_non_eu_country_ends_politely_and_creates_nothing(
    issued: tuple[CustomerInvite, str],
) -> None:
    client, token, csrf = open_flow(issued)
    post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf)
    post(client, f"{INVITE}/{token}/declaration", DECLARATIONS, csrf)
    post(
        client,
        f"{INVITE}/{token}/documents",
        {"accepted": list(onboarding.REQUIRED_DOCUMENTS), "language": "de"},
        csrf,
    )
    answer = post(client, f"{INVITE}/{token}/country", {"country": "CH"}, csrf)
    assert answer.status_code == 200
    assert answer.json()["outside_eu"] is True
    assert answer.json()["step"] == "country"
    assert not User.objects.filter(username="creator@example.invalid").exists()
    invite = CustomerInvite.objects.get(pk=issued[0].pk)
    assert invite.outside_eu_answers == 1
    assert invite.country == ""


# --- security ------------------------------------------------------------------------------------


def test_every_write_needs_the_csrf_token(issued: tuple[CustomerInvite, str]) -> None:
    client, token, _ = open_flow(issued)
    for path, body in (
        (f"{INVITE}/{token}/password", {"password": PASSWORD}),
        (f"{INVITE}/{token}/declaration", DECLARATIONS),
        (f"{INVITE}/{token}/documents", {"accepted": list(onboarding.REQUIRED_DOCUMENTS)}),
        (f"{INVITE}/{token}/country", {"country": "DE"}),
        (f"{INVITE}/{token}/complete", {}),
    ):
        assert post(client, path, body).status_code == 403, path


def test_guessing_tokens_is_throttled_like_a_login(db: None) -> None:
    """The same buckets as ADR 0023, so nothing new stores a token or an address in the clear."""
    with override_settings(
        LOGIN_MAX_ACCOUNT_ATTEMPTS=100,
        LOGIN_MAX_IP_ATTEMPTS=3,
        LOGIN_ATTEMPT_WINDOW_MINUTES=15,
        LOGIN_LOCKOUT_MINUTES=15,
        LOGIN_TRUSTED_PROXIES=0,
    ):
        client = Client()
        for _ in range(3):
            assert (
                client.get(
                    f"{INVITE}/{onboarding.new_token()}", REMOTE_ADDR="203.0.113.9"
                ).status_code
                == 404
            )
        refused = client.get(f"{INVITE}/{onboarding.new_token()}", REMOTE_ADDR="203.0.113.9")
        assert refused.status_code == 429
        assert refused["Retry-After"]


def test_no_customer_answer_may_be_stored_by_a_cache(issued: tuple[CustomerInvite, str]) -> None:
    _, token = issued
    answer = Client().get(f"{INVITE}/{token}")
    assert answer["Cache-Control"] == "private, no-store"


def test_the_prefix_is_public_now_that_the_outbox_path_exists() -> None:
    """ADR 0052 §9 slice 2 (2026-09-19): the invite link has to work for a creator off the tailnet.

    The prefix left `ROLE_ONLY_PREFIXES` when the redemption became an outbox item — production
    still creates no account (`test_invites_on_production.py`), and this asserts the one line that
    makes the link reachable at all, in both directions, so re-adding the prefix fails here.
    """
    for prefixes in public_surface.ROLE_ONLY_PREFIXES.values():
        assert "/api/v1/app/invite" not in prefixes
    assert public_surface.role_allows("prod", f"{INVITE}/anything") is True
    assert public_surface.role_allows("dev", f"{INVITE}/anything") is True
    assert public_surface.customer_path_allowed(f"{INVITE}/anything") is True


def test_an_invite_grants_nothing_in_another_organisation(db: None) -> None:
    a = Organisation.objects.create(name="Org A")
    Organisation.objects.create(name="Org B")
    _, token = invites.issue(email="creator@example.invalid", organisation=a)
    client = Client(enforce_csrf_checks=True)
    csrf = client.get(f"{INVITE}/{token}").cookies["csrftoken"].value
    assert walk(client, token, csrf).json()["organisation"]["name"] == "Org A"
    memberships = Membership.objects.filter(user__username="creator@example.invalid")
    assert [membership.organisation_id for membership in memberships] == [a.pk]


def test_the_api_still_offers_no_way_to_register(db: None) -> None:
    """ADR 0023 stands: an invite is not signup. There is no endpoint that takes an address."""
    assert Client().post(f"{APP}/auth/register").status_code == 404
    assert Client().post(f"{INVITE}").status_code == 404
