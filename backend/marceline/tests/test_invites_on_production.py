"""An invite is redeemed on the public host and the account appears on development (ADR 0052 §9).

Slice 2 of ADR 0052, the one that makes the private beta possible: a creator who is invited is not
on the tailnet, so the link has to work on `dash.marceline.studio`. Nothing about ADR 0028 is
loosened for it — the public host still creates no account. It takes the redemption into the outbox
and answers 202; development applies it with the same `complete()` its own endpoint runs.

One database plays both machines, as in `test_customer_writes.py`: `MARCELINE_ROLE` says which
side a request is on, and development's pull goes through an `httpx.MockTransport` into
production's internal listener. No network.

What each test is here to pin:

- the whole round trip, including that **production creates nothing**;
- the one neutral 404, on production too, for every reason a token does not work — including a
  redemption already handed over, which is what makes single use hold across the two hosts;
- the accounts document's `invites` section: digests, never tokens; only while the flag is on;
- the apply is idempotent, and a tampered batch applies nothing at all.
"""

import base64
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts import invites, onboarding
from accounts.declarations import CURRENT_AGE_DECLARATION, CURRENT_BUSINESS_STATUS_DECLARATION
from accounts.models import BusinessStatus, CustomerInvite, DocumentAcceptance, Organisation
from delivery import client as delivery_client
from delivery.envelope import SIGNING_DOMAIN, EnvelopeError, generate_private_key
from delivery.inbox import PulledItem
from delivery.models import InboxItem, OutboxItem, OutboxPullState
from delivery.tests.helpers import KeyPair, make_keys
from marceline import customer_writes, export
from marceline.accounts_replica import apply_accounts, verify_accounts
from marceline.tests.test_app_invites import (
    DECLARATIONS,
    DOCUMENTS,
    INVITE,
    PASSWORD,
    post,
)

pytestmark = pytest.mark.django_db

PROD_INTERNAL = "http://marceline-prod.test:8081"
EMAIL = "creator@example.invalid"
#: One test rewrites production's signed batch on the wire; every other test leaves it alone.
TAMPER = {"on": False}


def resign(wire: bytes, key: Ed25519PrivateKey) -> bytes:
    """The same envelope, signed with another key: what a spoofed production node could send.

    Not a flipped byte in the JSON wrapper — that is caught as `malformed` before any signature is
    checked, which would make the test pass without exercising the signature at all.
    """
    outer = json.loads(wire)
    body = base64.b64decode(outer["payload"])
    outer["signature"] = base64.b64encode(key.sign(SIGNING_DOMAIN + body)).decode()
    return json.dumps(outer).encode()


@pytest.fixture(autouse=True)
def fast_password_hashing() -> Iterator[None]:
    """The redemption hashes a real password twice over; PBKDF2 would dominate this file."""
    with override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]):
        yield


@pytest.fixture(autouse=True)
def enabled(settings: Settings) -> None:
    settings.ONBOARDING_INVITES_ENABLED = True
    settings.ONBOARDING_DOCUMENTS = DOCUMENTS


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
def issued() -> tuple[CustomerInvite, str]:
    """An invite as development issues it — before production is ever in the picture."""
    return invites.issue(email=EMAIL, organisation_name="A New Studio")


@pytest.fixture
def on_production(settings: Settings, keys: tuple[KeyPair, KeyPair]) -> None:
    settings.MARCELINE_ROLE = "prod"
    OutboxPullState.objects.create(last_pull_at=timezone.now())


@pytest.fixture(autouse=True)
def production_listener(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Production's Tailscale-only listener, reached by development's `delivery.client`."""
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
        content = reply.content
        if TAMPER["on"] and content:
            content = resign(content, generate_private_key())
        return httpx.Response(reply.status_code, content=content)

    TAMPER["on"] = False
    monkeypatch.setattr(delivery_client, "transport", httpx.MockTransport(answer))
    yield
    TAMPER["on"] = False


def development_pulls(settings: Settings) -> None:
    role = settings.MARCELINE_ROLE
    settings.MARCELINE_ROLE = "dev"
    try:
        call_command("pull_customer_writes")
    finally:
        settings.MARCELINE_ROLE = role


def redeem(token: str, *, country: str = "DE") -> Any:
    """The whole redemption as a browser does it, with a real CSRF token; the last answer."""
    return browser_redeems(token, country=country)[2]


def browser_redeems(token: str, *, country: str = "DE") -> tuple[Client, str, Any]:
    """`(browser, csrf token, the last answer)` — so a test can press the last button twice."""
    client = Client(enforce_csrf_checks=True)
    state = client.get(f"{INVITE}/{token}")
    assert state.status_code == 200, state.content
    csrf = state.cookies["csrftoken"].value
    set_password(client, token, csrf)
    assert post(client, f"{INVITE}/{token}/declaration", DECLARATIONS, csrf).status_code == 200
    documents = {"accepted": list(onboarding.REQUIRED_DOCUMENTS), "language": "de"}
    assert post(client, f"{INVITE}/{token}/documents", documents, csrf).status_code == 200
    assert post(client, f"{INVITE}/{token}/country", {"country": country}, csrf).status_code == 200
    return client, csrf, post(client, f"{INVITE}/{token}/complete", {}, csrf)


def set_password(client: Client, token: str, csrf: str) -> None:
    answer = post(client, f"{INVITE}/{token}/password", {"password": PASSWORD}, csrf)
    assert answer.status_code == 200, answer.content


# --- the round trip -----------------------------------------------------------------------------


@pytest.mark.usefixtures("on_production")
def test_production_accepts_a_redemption_and_development_creates_the_account(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    invite, token = issued

    accepted = redeem(token)

    assert accepted.status_code == 202, accepted.content
    body = accepted.json()
    assert body["pending"] is True
    assert body["submission_id"].startswith("sub_")
    assert not accepted.cookies.get("sessionid"), "nobody is signed in: there is no account yet"
    # Production created nothing at all — that is the whole point of ADR 0028.
    assert not User.objects.exists()
    assert not Organisation.objects.exists()
    item = OutboxItem.objects.get()
    assert (item.kind, item.organisation_id, item.user_id) == ("invite_redeem", 0, 0)
    assert invite.token_hash in item.payload, "the invite is named by its digest"
    assert token not in item.payload, "the token itself never travels"
    assert EMAIL not in item.payload, "nor the address: development's own row holds it"
    assert PASSWORD not in item.payload, "the password travels as a hash only"

    development_pulls(settings)

    user = User.objects.get()
    assert user.username == EMAIL
    assert user.check_password(PASSWORD), "the hash production made is the hash of the password"
    membership = user.memberships.get()
    assert membership.organisation.name == "A New Studio"
    status = BusinessStatus.objects.get(organisation=membership.organisation)
    assert status.country == "DE"
    assert status.declaration_version and status.age_declaration_version
    assert {row.document_id for row in DocumentAcceptance.objects.all()} == set(
        onboarding.REQUIRED_DOCUMENTS
    )
    assert DocumentAcceptance.objects.filter(user=user).count() == 2
    redeemed = CustomerInvite.objects.get(pk=invite.pk)
    assert redeemed.redeemed_at is not None
    assert redeemed.password_hash == "", "the hash lives on the user row now"
    settled = OutboxItem.objects.get()
    assert (settled.status, settled.result_code) == ("done", "account_created")
    assert settled.payload == "", "the password hash leaves production as soon as it is applied"
    assert InboxItem.objects.get().acknowledged_at is not None


@pytest.mark.usefixtures("on_production")
def test_the_link_is_finished_the_moment_the_redemption_is_handed_over(
    issued: tuple[CustomerInvite, str],
) -> None:
    """Single use has to hold on the host that cannot see `redeemed_at` yet (the brief's rule).

    A second submission is refused **locally**, before development has seen anything, and it is
    refused with the same neutral answer an unknown token gets.
    """
    invite, token = issued
    assert redeem(token).status_code == 202

    again = Client().get(f"{INVITE}/{token}")
    unknown = Client().get(f"{INVITE}/{onboarding.new_token()}")

    assert again.status_code == 404
    assert again.content == unknown.content
    handed = CustomerInvite.objects.get(pk=invite.pk)
    assert handed.handed_over_at is not None
    assert handed.submission_id.startswith("sub_")
    assert handed.password_hash == "", "production keeps no credential after handing it over"


@pytest.mark.usefixtures("on_production")
def test_a_second_complete_enqueues_nothing(issued: tuple[CustomerInvite, str]) -> None:
    """The same button pressed twice: one item, one account, and the second answer says nothing."""
    _, token = issued
    client, csrf, first = browser_redeems(token)
    assert first.status_code == 202

    second = post(client, f"{INVITE}/{token}/complete", {}, csrf)

    assert second.status_code == 404
    assert OutboxItem.objects.count() == 1


@pytest.mark.usefixtures("on_production")
def test_every_reason_a_token_fails_answers_identically_on_production(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    _, good = issued
    client = Client()
    _, expired = invites.issue(
        email="b@example.invalid",
        organisation_name="B",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    revoked_row, revoked = invites.issue(email="c@example.invalid", organisation_name="C")
    invites.revoke(revoked_row)
    handed_row, handed = invites.issue(email="d@example.invalid", organisation_name="D")
    invites.mark_handed_over(handed_row, submission_id="sub_" + "0" * 32)

    answers = [
        client.get(f"{INVITE}/{onboarding.new_token()}"),
        client.get(f"{INVITE}/not-a-valid-token-at-all-%21"),
        client.get(f"{INVITE}/{expired}"),
        client.get(f"{INVITE}/{revoked}"),
        client.get(f"{INVITE}/{handed}"),
    ]
    for answer in answers:
        assert answer.status_code == 404, answer.content
        assert answer.content == answers[0].content

    settings.ONBOARDING_INVITES_ENABLED = False
    off = client.get(f"{INVITE}/{good}")
    assert off.status_code == 404
    assert off.content == answers[0].content


@pytest.mark.usefixtures("on_production")
def test_the_accept_step_still_refuses_while_no_document_is_published(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    """`ONBOARDING_DOCUMENTS` empty refuses on the public host too — it reads the same setting."""
    settings.ONBOARDING_DOCUMENTS = ""
    _, token = issued
    client = Client(enforce_csrf_checks=True)
    state = client.get(f"{INVITE}/{token}")
    csrf = state.cookies["csrftoken"].value
    assert state.json()["documents_published"] is False
    set_password(client, token, csrf)
    assert post(client, f"{INVITE}/{token}/declaration", DECLARATIONS, csrf).status_code == 200

    refused = post(
        client,
        f"{INVITE}/{token}/documents",
        {"accepted": list(onboarding.REQUIRED_DOCUMENTS), "language": "de"},
        csrf,
    )

    assert refused.status_code == 400
    assert refused.json()["problems"] == ["documents_not_published"]
    assert not OutboxItem.objects.exists()


@pytest.mark.usefixtures("on_production")
def test_production_refuses_the_redemption_while_the_outbox_is_off(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    settings.OUTBOX_ENABLED = False
    _, token = issued

    answer = redeem(token)

    assert answer.status_code == 503
    assert answer.json()["detail"] == "invite_write_unavailable"
    assert not OutboxItem.objects.exists()
    assert not User.objects.exists()


def test_production_can_never_create_an_account_itself(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    """The role check sits in `complete()`, not only in the view (ADR 0028)."""
    invite, token = issued
    client = Client(enforce_csrf_checks=True)
    csrf = client.get(f"{INVITE}/{token}").cookies["csrftoken"].value
    set_password(client, token, csrf)
    settings.MARCELINE_ROLE = "prod"

    with pytest.raises(invites.InviteRefused):
        invites.complete(CustomerInvite.objects.get(pk=invite.pk))

    assert not User.objects.exists()


# --- applying it on development -----------------------------------------------------------------


@pytest.mark.usefixtures("on_production")
def test_applying_the_same_redemption_twice_changes_nothing(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    """A replayed item is `done` with no effect — the same person pressed twice (ADR 0052 §9)."""
    _, token = issued
    assert redeem(token).status_code == 202
    item = OutboxItem.objects.get()
    payload = item.payload
    development_pulls(settings)
    settings.MARCELINE_ROLE = "dev"

    replay = customer_writes.apply(
        PulledItem(
            id=item.public_id,
            organisation_id=0,
            user_id=0,
            kind=OutboxItem.Kind.INVITE_REDEEM,
            received_at=timezone.now(),
            payload=payload,
        )
    )

    assert (replay.outcome, replay.code) == ("done", "already_redeemed")
    assert User.objects.count() == 1
    assert Organisation.objects.count() == 1


@pytest.mark.usefixtures("on_production")
def test_a_second_pull_applies_nothing(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    _, token = issued
    assert redeem(token).status_code == 202

    development_pulls(settings)
    development_pulls(settings)

    assert User.objects.count() == 1
    assert InboxItem.objects.count() == 1


@pytest.mark.usefixtures("on_production")
def test_a_tampered_batch_applies_nothing(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    """A batch signed by anybody else is refused whole: no account, and the item stays pending."""
    _, token = issued
    assert redeem(token).status_code == 202
    TAMPER["on"] = True

    with pytest.raises(CommandError, match="bad_signature"):
        development_pulls(settings)

    assert not User.objects.exists()
    assert OutboxItem.objects.get().status == "pending"
    assert not InboxItem.objects.exists()


@pytest.mark.usefixtures("on_production")
def test_a_redemption_of_an_invite_revoked_in_the_meantime_is_refused(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    """The stale window ADR 0052 §9 names: revocation is operational, not a security control."""
    invite, token = issued
    assert redeem(token).status_code == 202
    invites.revoke(CustomerInvite.objects.get(pk=invite.pk), reason="asked us to")

    development_pulls(settings)

    assert not User.objects.exists()
    item = OutboxItem.objects.get()
    assert (item.status, item.result_code) == ("refused", onboarding.REFUSED)


@pytest.mark.usefixtures("on_production")
def test_a_declaration_whose_wording_moved_on_is_refused(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    """Development judges the redemption against today's wording, never production's word for it."""
    _, token = issued
    assert redeem(token).status_code == 202
    item = OutboxItem.objects.get()
    settings.MARCELINE_ROLE = "dev"
    tampered = item.payload.replace('"declaration_version":"', '"declaration_version":"x')

    outcome = customer_writes.apply(
        PulledItem(
            id=item.public_id,
            organisation_id=0,
            user_id=0,
            kind=OutboxItem.Kind.INVITE_REDEEM,
            received_at=timezone.now(),
            payload=tampered,
        )
    )

    assert (outcome.outcome, outcome.code) == ("refused", "declaration_version_outdated")
    assert not User.objects.exists()


def test_a_password_hash_django_does_not_recognise_is_refused(
    settings: Settings, issued: tuple[CustomerInvite, str]
) -> None:
    """`User.password` is set from the payload, so only one of Django's own hashes is accepted."""
    invite, _ = issued
    redemption = invites.Redemption(
        token_hash=invite.token_hash,
        password_hash="not-a-django-hash",
        declaration_version=CURRENT_BUSINESS_STATUS_DECLARATION.version,
        age_declaration_version=CURRENT_AGE_DECLARATION.version,
        declared_at=timezone.now(),
        age_declared_at=timezone.now(),
        country="DE",
        language="de",
        accepted_at=timezone.now(),
        documents=tuple(
            invites.AcceptedDocument(
                document_id=document.document_id,
                version=document.version,
                sha256=document.sha256,
            )
            for document in onboarding.published_documents()
        ),
    )

    outcome = customer_writes.apply_invite_redeem(
        PulledItem(
            id="sub_" + "0" * 32,
            organisation_id=0,
            user_id=0,
            kind=OutboxItem.Kind.INVITE_REDEEM,
            received_at=timezone.now(),
            payload=json.dumps(redemption.as_payload(), separators=(",", ":")),
        )
    )

    assert (outcome.outcome, outcome.code) == ("refused", "invalid_payload")
    assert not User.objects.exists()


# --- the accounts document carries the open invites ----------------------------------------------


def test_the_document_carries_digests_and_only_while_the_flag_is_on(
    settings: Settings, issued: tuple[CustomerInvite, str], keys: tuple[KeyPair, KeyPair]
) -> None:
    invite, token = issued
    invite.note = "met at a shoot"
    invite.save(update_fields=["note"])
    now = timezone.now()

    payload = export.accounts_payload(now=now)

    assert [row["token_hash"] for row in payload["invites"]] == [invite.token_hash]
    assert payload["invites"][0]["email"] == EMAIL
    assert payload["invites"][0]["organisation_name"] == "A New Studio"
    assert payload["invites"][0]["organisation_id"] == 0
    serialised = str(payload["invites"])
    assert token not in serialised, "the token is not ours to carry: only its digest"
    assert "met at a shoot" not in serialised, "the staff note stays on development"

    settings.ONBOARDING_INVITES_ENABLED = False
    assert "invites" not in export.accounts_payload(now=now), (
        "a production still running the previous image must not meet a field it forbids"
    )


def test_a_used_or_expired_invite_leaves_the_document(
    issued: tuple[CustomerInvite, str], keys: tuple[KeyPair, KeyPair]
) -> None:
    invite, _ = issued
    invites.issue(
        email="b@example.invalid",
        organisation_name="B",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    revoked, _ = invites.issue(email="c@example.invalid", organisation_name="C")
    invites.revoke(revoked)

    payload = export.accounts_payload(now=timezone.now())

    assert [row["token_hash"] for row in payload["invites"]] == [invite.token_hash]


def test_replication_keeps_a_half_finished_redemption_and_drops_what_left_the_document(
    settings: Settings, issued: tuple[CustomerInvite, str], keys: tuple[KeyPair, KeyPair]
) -> None:
    """Production's row is the same table, filled from the document — and never overwritten there.

    What a creator has already answered on the public host (their password hash, the declarations,
    the acceptances) survives every pull; an invite that left the document is deleted, so the link
    answers the neutral page.
    """
    invite, _ = issued
    _, other = invites.issue(email="b@example.invalid", organisation_name="B")
    now = timezone.now()
    document = verify_accounts(
        export.signed_accounts(now=now), keys[0].private.public_key(), now=now
    )
    assert len(document.invites) == 2
    settings.MARCELINE_ROLE = "prod"
    apply_accounts(document, now=now)
    # The creator gets halfway through on the public host.
    local = CustomerInvite.objects.get(token_hash=invite.token_hash)
    invites.set_password(local, PASSWORD)
    invites.record_declarations(local)

    # …and staff revoke the other invite on development, so the next document drops it.
    invites.revoke(CustomerInvite.objects.get(token_hash=onboarding.token_hash(other)))
    later = now + timedelta(minutes=1)
    settings.MARCELINE_ROLE = "dev"
    second = verify_accounts(
        export.signed_accounts(now=later), keys[0].private.public_key(), now=later
    )
    settings.MARCELINE_ROLE = "prod"
    apply_accounts(second, now=later)

    kept = CustomerInvite.objects.get(token_hash=invite.token_hash)
    assert kept.password_hash, "a pull must not undo what the creator already answered"
    assert kept.declaration_version
    assert invites.step_of(kept) is onboarding.Step.DOCUMENTS
    assert CustomerInvite.objects.count() == 1, "the revoked invite is gone from production"


def test_a_tampered_accounts_document_is_refused(
    issued: tuple[CustomerInvite, str], keys: tuple[KeyPair, KeyPair]
) -> None:
    now = timezone.now()
    wire = export.signed_accounts(now=now)
    tampered = resign(wire, generate_private_key())

    with pytest.raises(EnvelopeError, match="bad_signature"):
        verify_accounts(tampered, keys[0].private.public_key(), now=now)
