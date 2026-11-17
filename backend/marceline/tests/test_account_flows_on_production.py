"""A reset and a verification across the two hosts (ADR 0054 §1, ADR 0028).

The public host has no mail transport and no authority over a password. So a request there becomes
an outbox item, development issues the token and sends the mail, the token's **digest** comes back
in the signed accounts document, the new password is hashed **on production** and only the hash
travels, and development applies it and ends every session.

One database plays both machines, as in `test_customer_writes.py` and
`test_invites_on_production.py`: `MARCELINE_ROLE` says which side a request is on, and development's
pull goes through an `httpx.MockTransport` into production's internal listener. No network, and the
mail never leaves the process (locmem).

**Production here has no mail transport**, which is what makes it production (issue #153). The test
runner installs the locmem backend for every test, and locmem *is* a configured transport — so a
"production" that kept it had a capability the real one is refused
(`infra/prod/check_config.py` fails the render on any `EMAIL_*` key), and
`available() = flag and mail_configured()` looked fine here while every mailed link answered the
neutral 404 on the real host. `PROD_MAILERS` is what production's environment actually yields: the
SMTP backend with no host. Anything that has to mail switches to development's mailer with
`as_development`, the only place the role changes.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core import mail as django_mail
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts import account_actions, action_tokens
from accounts.models import AccountActionToken, EmailVerification
from delivery import client as delivery_client
from delivery.models import InboxItem, OutboxItem, OutboxPullState
from delivery.tests.helpers import KeyPair, make_keys
from marceline import export
from marceline.accounts_replica import apply_accounts, verify_accounts
from marceline.tests.app_helpers import APP, add_member, make_organisation

pytestmark = pytest.mark.django_db

AUTH = f"{APP}/auth"
RESET = f"{AUTH}/password-reset"
VERIFY = f"{AUTH}/verify-email"
PROD_INTERNAL = "http://marceline-prod.test:8081"
#: What `infra/prod/compose.yaml` yields: no `EMAIL_*` at all, so `settings.py` falls back to the
#: SMTP backend with nothing to connect to. `mail_configured()` is False here for ever, by design.
PROD_MAILERS = {
    "default": {"BACKEND": "django.core.mail.backends.smtp.EmailBackend", "OPTIONS": {}}
}
#: The mailer the test runner installs, which only development is allowed to have.
DEV_MAILERS = {
    "default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend", "OPTIONS": {}}
}
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
def keys(tmp_path: Path, settings: Settings) -> tuple[KeyPair, KeyPair]:
    dev = make_keys(tmp_path, "dev")
    prod = make_keys(tmp_path, "prod")
    settings.CACHE_SIGNING_KEY_PATH = str(dev.private_path)
    settings.OUTBOX_VERIFY_KEY_PATH = str(prod.public_path)
    settings.CACHE_PURGE_BASE_URL = PROD_INTERNAL
    settings.CACHE_VERIFY_KEY_PATH = str(dev.public_path)
    settings.OUTBOX_SIGNING_KEY_PATH = str(prod.private_path)
    settings.OUTBOX_ENABLED = True
    return dev, prod


@pytest.fixture
def customer() -> User:
    user = add_member(make_organisation("A Studio"), "creator@example.invalid")
    user.email = user.get_username()
    user.set_password(PASSWORD)
    user.save(update_fields=["email", "password"])
    return user


@pytest.fixture
def on_production(settings: Settings, keys: tuple[KeyPair, KeyPair]) -> None:
    """The public host as it really is: the role **and** no mail transport (issue #153)."""
    settings.MARCELINE_ROLE = "prod"
    settings.MAILERS = PROD_MAILERS
    OutboxPullState.objects.create(last_pull_at=timezone.now())


@contextmanager
def as_development(settings: Settings) -> Iterator[None]:
    """Run a block as development: its role and its mailer, both put back afterwards.

    The only place either changes. A test that reached for `MARCELINE_ROLE = "dev"` alone would be
    development with production's mailer, which is nobody's machine.
    """
    role, mailers = settings.MARCELINE_ROLE, settings.MAILERS
    settings.MARCELINE_ROLE, settings.MAILERS = "dev", DEV_MAILERS
    try:
        yield
    finally:
        settings.MARCELINE_ROLE, settings.MAILERS = role, mailers


@pytest.fixture(autouse=True)
def production_listener(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    listener = Client()

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "marceline-prod.test"
        role, surface = settings.MARCELINE_ROLE, settings.PUBLIC_SURFACE
        settings.MARCELINE_ROLE, settings.PUBLIC_SURFACE = "prod", "internal"
        try:
            reply = listener.post(
                request.url.path, request.content, content_type="application/json"
            )
        finally:
            settings.MARCELINE_ROLE, settings.PUBLIC_SURFACE = role, surface
        return httpx.Response(reply.status_code, content=reply.content)

    monkeypatch.setattr(delivery_client, "transport", httpx.MockTransport(answer))
    yield


def development_pulls(settings: Settings) -> None:
    """Development pulls the outbox and applies what it finds — with its own mailer (issue #153)."""
    with as_development(settings):
        call_command("pull_customer_writes")


def post(client: Client, path: str, body: object = None, token: str | None = None) -> Any:
    headers = {"X-CSRFToken": token} if token else {}
    return client.post(
        path, json.dumps(body or {}), content_type="application/json", headers=headers
    )


def with_csrf(path: str) -> tuple[Client, str]:
    client = Client(enforce_csrf_checks=True)
    answer = client.get(path)
    return client, str(answer.cookies["csrftoken"].value)


def mailed_token() -> str:
    [sent] = django_mail.outbox
    links = [word for word in sent.body.split() if word.startswith("https://")]
    assert len(links) == 1
    return links[0].rsplit("/", 1)[-1]


def replicate(settings: Settings, keys: tuple[KeyPair, KeyPair]) -> None:
    """One `pull_accounts` cycle: development signs the document, production applies it."""
    now = timezone.now()
    with as_development(settings):
        wire = export.signed_accounts(now=now)
    payload = verify_accounts(wire, keys[0].private.public_key(), now=now)
    apply_accounts(payload, now=now)


# --- the reset, end to end -----------------------------------------------------------------------


@pytest.mark.usefixtures("on_production")
def test_a_reset_asked_for_on_production_is_mailed_by_development(
    settings: Settings, customer: User
) -> None:
    client, csrf = with_csrf(f"{AUTH}/session")

    accepted = post(client, RESET, {"email": customer.get_username(), "language": "de"}, csrf)

    assert accepted.status_code == 202
    assert accepted.json() == {"detail": "mail_requested"}
    assert django_mail.outbox == [], "the public host sends nothing"
    assert not AccountActionToken.objects.exists(), "and issues nothing"
    item = OutboxItem.objects.get()
    assert (item.kind, item.organisation_id, item.user_id) == ("password_reset_request", 0, 0)

    development_pulls(settings)

    assert len(django_mail.outbox) == 1
    assert AccountActionToken.objects.get().kind == Kind.PASSWORD_RESET
    settled = OutboxItem.objects.get()
    assert (settled.status, settled.result_code) == ("done", "requested")


@pytest.mark.usefixtures("on_production")
def test_an_unknown_address_looks_exactly_the_same_on_production(
    settings: Settings, customer: User
) -> None:
    """Production does not look the address up at all, so there is nothing to time (ADR 0054 §2)."""
    client, csrf = with_csrf(f"{AUTH}/session")

    known = post(client, RESET, {"email": customer.get_username()}, csrf)
    unknown = post(client, RESET, {"email": "nobody@example.invalid"}, csrf)

    assert known.status_code == unknown.status_code == 202
    assert known.content == unknown.content
    assert OutboxItem.objects.count() == 2, "both are accepted; development decides"

    development_pulls(settings)

    assert len(django_mail.outbox) == 1
    assert [row.result_code for row in OutboxItem.objects.all()] == ["requested", "requested"]


@pytest.mark.usefixtures("on_production")
def test_the_new_password_is_hashed_on_production_and_only_the_hash_travels(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    # Development mails the link…
    with as_development(settings):
        account_actions.request_password_reset(customer.get_username(), language="de")
    token = mailed_token()
    # …and production learns its digest from the accounts document.
    replicate(settings, keys)
    signed_in = Client()
    assert signed_in.login(username=customer.get_username(), password=PASSWORD)
    session_key = str(signed_in.cookies["sessionid"].value)

    state = Client().get(f"{RESET}/{token}")
    assert state.status_code == 200
    assert state.json()["email"] == customer.get_username()

    client, csrf = with_csrf(f"{RESET}/{token}")
    accepted = post(client, f"{RESET}/{token}", {"password": NEW_PASSWORD}, csrf)

    assert accepted.status_code == 202
    body = accepted.json()
    assert body["pending"] is True and body["submission_id"].startswith("sub_")
    item = OutboxItem.objects.get()
    assert item.kind == "password_reset_set"
    assert NEW_PASSWORD not in item.payload, "only the hash travels"
    assert token not in item.payload, "the token stays in the reader's mail"
    customer.refresh_from_db()
    assert customer.check_password(PASSWORD), "production changed nothing"
    # The session is gone on the public host at once, not in fifteen minutes (ADR 0054 §3).
    assert not Session.objects.filter(session_key=session_key).exists()
    # And the link is finished here, before development has seen anything.
    assert Client().get(f"{RESET}/{token}").status_code == 404

    development_pulls(settings)

    customer.refresh_from_db()
    assert customer.check_password(NEW_PASSWORD)
    settled = OutboxItem.objects.get()
    assert (settled.status, settled.result_code) == ("done", "password_set")
    assert settled.payload == "", "the hash leaves production as soon as it is applied"
    assert InboxItem.objects.get().acknowledged_at is not None


@pytest.mark.usefixtures("on_production")
def test_a_second_submission_enqueues_nothing(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    with as_development(settings):
        account_actions.request_password_reset(customer.get_username())
    token = mailed_token()
    replicate(settings, keys)
    client, csrf = with_csrf(f"{RESET}/{token}")
    assert post(client, f"{RESET}/{token}", {"password": NEW_PASSWORD}, csrf).status_code == 202

    second = post(client, f"{RESET}/{token}", {"password": "a-third-long-passphrase"}, csrf)

    assert second.status_code == 404
    assert OutboxItem.objects.count() == 1


@pytest.mark.usefixtures("on_production")
def test_production_refuses_a_reset_while_the_outbox_is_off(
    settings: Settings, customer: User
) -> None:
    settings.OUTBOX_ENABLED = False
    client, csrf = with_csrf(f"{AUTH}/session")

    answer = post(client, RESET, {"email": customer.get_username()}, csrf)

    assert answer.status_code == 503
    assert answer.json()["detail"] == "account_action_unavailable"
    assert not OutboxItem.objects.exists()


# --- verification across the hosts ----------------------------------------------------------------


@pytest.mark.usefixtures("on_production")
def test_a_confirmation_on_production_is_applied_by_development(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    with as_development(settings):
        account_actions.request_verification(customer, language="en")
    token = mailed_token()
    replicate(settings, keys)

    client, csrf = with_csrf(f"{VERIFY}/{token}")
    accepted = post(client, f"{VERIFY}/{token}", {}, csrf)

    assert accepted.status_code == 202
    assert accepted.json()["pending"] is True
    assert EmailVerification.objects.get(user=customer).verified_at is None

    development_pulls(settings)

    assert EmailVerification.objects.get(user=customer).verified_at is not None
    assert OutboxItem.objects.get().result_code == "verified"


@pytest.mark.usefixtures("on_production")
def test_a_resend_asked_for_on_production_is_sent_by_development(
    settings: Settings, customer: User
) -> None:
    signed_in = Client()
    signed_in.force_login(customer)
    signed_in.get(f"{APP}/me")
    csrf = str(signed_in.cookies["csrftoken"].value)

    accepted = post(signed_in, VERIFY, {"language": "de"}, csrf)

    assert accepted.status_code == 202
    assert django_mail.outbox == []
    item = OutboxItem.objects.get()
    assert (item.kind, item.user_id) == ("email_verify_resend", customer.pk)

    development_pulls(settings)

    assert len(django_mail.outbox) == 1
    assert OutboxItem.objects.get().result_code == "requested"


# --- the signed accounts document ----------------------------------------------------------------


def test_the_document_carries_digests_and_only_while_the_flag_is_on(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    account_actions.request_password_reset(customer.get_username())
    token = mailed_token()
    now = timezone.now()

    payload = export.accounts_payload(now=now)

    [row] = payload["action_tokens"]
    assert row == {
        "token_hash": action_tokens.token_hash(token),
        "kind": "password_reset",
        "user_id": customer.pk,
        "expires_at": AccountActionToken.objects.get().expires_at,
    }
    assert token not in json.dumps(payload, default=str), "never the token itself"
    assert payload["users"][0]["email_verified_at"] is None

    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    off = export.accounts_payload(now=now)
    assert "action_tokens" not in off, "an older production forbids a field it does not know"
    assert "email_verified_at" not in off["users"][0]


def test_a_used_or_expired_link_leaves_the_document_and_stops_working(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    account_actions.request_password_reset(customer.get_username())
    token = mailed_token()
    settings.MARCELINE_ROLE, settings.MAILERS = "prod", PROD_MAILERS
    replicate(settings, keys)
    assert AccountActionToken.objects.count() == 1
    assert Client().get(f"{RESET}/{token}").status_code == 200

    AccountActionToken.objects.update(used_at=timezone.now())
    replicate(settings, keys)

    assert not AccountActionToken.objects.exists(), "production drops what left the document"
    assert Client().get(f"{RESET}/{token}").status_code == 404


def test_the_verification_state_replicates(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    """The **moment**, not a flag, so the public host holds the fact development holds.

    One database plays both machines here, so the document is signed while development's row exists
    and applied after it is dropped — which is what a real production, with no row of its own, does.
    """
    moment = timezone.now() - timedelta(days=1)
    EmailVerification.objects.create(user=customer, verified_at=moment)
    now = timezone.now()
    wire = export.signed_accounts(now=now)
    EmailVerification.objects.all().delete()

    settings.MARCELINE_ROLE, settings.MAILERS = "prod", PROD_MAILERS
    apply_accounts(verify_accounts(wire, keys[0].private.public_key(), now=now), now=now)

    assert EmailVerification.objects.get(user=customer).verified_at == moment
    assert account_actions.verified(customer) is True


def test_a_link_for_an_account_the_document_does_not_carry_is_refused(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    """A token row cannot exist without its user, and a document that implies one is not applied."""
    account_actions.request_password_reset(customer.get_username())
    now = timezone.now()
    payload = export.accounts_payload(now=now)
    payload["users"] = []

    from delivery.envelope import EnvelopeError, seal

    wire = seal(payload, keys[0].private)
    with pytest.raises(EnvelopeError, match="unknown_account"):
        verify_accounts(wire, keys[0].private.public_key(), now=now)


# --- what issue #153 was: the predicate, and the row that should not exist ----------------------


@pytest.mark.usefixtures("on_production")
def test_a_host_that_cannot_mail_still_answers_every_link(settings: Settings) -> None:
    """The defect, as a predicate (issue #153).

    Production is refused a mail transport by design, so asking it for one made `available()` false
    there and **every** mailed link answer the neutral 404 with the flag on. The transport is
    development's requirement; production's is the flag alone.
    """
    assert action_tokens.mail_configured() is False, "production has no transport, by design"
    assert action_tokens.available() is True, "and does not need one: it relays (ADR 0054 §1)"
    assert action_tokens.mails_here() is False, "and may never hand a mail to a transport"

    with as_development(settings):
        assert action_tokens.mail_configured() is True
        assert action_tokens.available() is True
        assert action_tokens.mails_here() is True

    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    assert action_tokens.available() is False, "the flag still decides on both hosts"


@pytest.mark.usefixtures("on_production")
def test_no_production_path_asks_whether_this_host_can_mail(
    settings: Settings,
    customer: User,
    keys: tuple[KeyPair, KeyPair],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard, not an assertion about an answer: on production, nothing may consult the mailer.

    `mail_configured()` raises here, so any code path that reaches for it fails this test instead
    of quietly refusing a creator — which is how the defect survived eleven passing tests.
    `mails_here()` may ask (it is the sender's own gate) and answers False by role first.
    """

    def refuse() -> bool:
        raise AssertionError("a production code path asked whether this host can mail (issue #153)")

    with as_development(settings):
        account_actions.request_password_reset(customer.get_username())
        _, verification_token = account_actions.issue(customer, Kind.EMAIL_VERIFICATION)
    reset_token = mailed_token()
    replicate(settings, keys)
    monkeypatch.setattr(action_tokens, "mail_configured", refuse)

    # Every route a creator can reach on the public host, in one pass.
    client, csrf = with_csrf(f"{AUTH}/session")
    assert post(client, RESET, {"email": customer.get_username()}, csrf).status_code == 202
    assert Client().get(f"{RESET}/{reset_token}").status_code == 200
    assert Client().get(f"{VERIFY}/{verification_token}").status_code == 200
    signed_in = Client()
    signed_in.force_login(customer)
    signed_in.get(f"{APP}/me")
    resend_csrf = str(signed_in.cookies["csrftoken"].value)
    assert post(signed_in, VERIFY, {"language": "en"}, resend_csrf).status_code == 202
    assert signed_in.get(f"{APP}/me").json()["can_verify_email"] is True
    setter, set_csrf = with_csrf(f"{RESET}/{reset_token}")
    setting = post(setter, f"{RESET}/{reset_token}", {"password": NEW_PASSWORD}, set_csrf)
    assert setting.status_code == 202


@pytest.mark.usefixtures("on_production")
def test_the_flag_off_writes_no_outbox_row_at_all(settings: Settings, customer: User) -> None:
    """ "Off" means nothing happens — the second half of issue #153.

    The request route used to go straight to `outbox.enqueue` without asking the flag, so anybody
    could put rows into production's queue while the feature was off. The answer is the **same**
    neutral 202 either way (ADR 0054 §2's enumeration rule owns that sentence); what changes is
    that there is no row, no token and no mail anywhere.
    """
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    client, csrf = with_csrf(f"{AUTH}/session")

    off = post(client, RESET, {"email": customer.get_username()}, csrf)

    assert off.status_code == 202
    assert off.json() == {"detail": "mail_requested"}
    assert not OutboxItem.objects.exists(), "nothing was queued"
    assert not AccountActionToken.objects.exists()
    assert django_mail.outbox == []

    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = True
    on = post(client, RESET, {"email": customer.get_username()}, csrf)
    assert on.content == off.content, "the same sentence, with a row behind it"
    assert OutboxItem.objects.count() == 1


@pytest.mark.usefixtures("on_production")
def test_the_flag_off_refuses_every_link_on_production_too(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    with as_development(settings):
        account_actions.request_password_reset(customer.get_username())
    token = mailed_token()
    replicate(settings, keys)
    assert Client().get(f"{RESET}/{token}").status_code == 200

    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False

    refused = Client().get(f"{RESET}/{token}")
    assert refused.status_code == 404
    assert refused.json() == {"detail": "link_not_available"}


@pytest.mark.usefixtures("on_production")
def test_the_whole_reset_round_trip_on_a_host_that_cannot_mail(
    settings: Settings, customer: User, keys: tuple[KeyPair, KeyPair]
) -> None:
    """The journey issue #153 broke, from one end to the other, with production shaped as it is.

    Request on the public host → an outbox row → development applies it and **mails** → the digest
    reaches production in the signed accounts document → the link works **there** → the new password
    is hashed there and only the hash travels → development applies it → every session is gone.
    """
    signed_in = Client()
    assert signed_in.login(username=customer.get_username(), password=PASSWORD)
    session_key = str(signed_in.cookies["sessionid"].value)

    # 1. the request, on the host with no transport
    client, csrf = with_csrf(f"{AUTH}/session")
    accepted = post(client, RESET, {"email": customer.get_username(), "language": "de"}, csrf)
    assert accepted.status_code == 202
    assert django_mail.outbox == [], "the public host sends nothing"
    assert not AccountActionToken.objects.exists(), "and mints nothing"
    assert OutboxItem.objects.get().kind == "password_reset_request"

    # 2. development pulls it, mints the token and sends the mail
    development_pulls(settings)
    assert len(django_mail.outbox) == 1
    token = mailed_token()
    row = AccountActionToken.objects.get()
    assert row.token_hash == action_tokens.token_hash(token)

    # 3. the digest travels in the accounts document
    with as_development(settings):
        document = export.accounts_payload(now=timezone.now())
    assert [entry["token_hash"] for entry in document["action_tokens"]] == [row.token_hash]
    assert token not in json.dumps(document, default=str)
    replicate(settings, keys)

    # 4. the link works on production — this is the line that would have failed
    state = Client().get(f"{RESET}/{token}")
    assert state.status_code == 200, "the defect of issue #153 answered 404 here"
    assert state.json()["email"] == customer.get_username()

    # 5. the new password is hashed there; only the hash travels, and the session dies at once
    setter, set_csrf = with_csrf(f"{RESET}/{token}")
    done = post(setter, f"{RESET}/{token}", {"password": NEW_PASSWORD}, set_csrf)
    assert done.status_code == 202 and done.json()["pending"] is True
    item = OutboxItem.objects.get(kind="password_reset_set")
    assert NEW_PASSWORD not in item.payload and token not in item.payload
    customer.refresh_from_db()
    assert customer.check_password(PASSWORD), "production changed no password"
    assert not Session.objects.filter(session_key=session_key).exists()
    assert Client().get(f"{RESET}/{token}").status_code == 404, "the link is finished"

    # 6. development applies it
    development_pulls(settings)
    customer.refresh_from_db()
    assert customer.check_password(NEW_PASSWORD)
    assert OutboxItem.objects.get(kind="password_reset_set").result_code == "password_set"
    assert AccountActionToken.objects.get().used_at is not None
