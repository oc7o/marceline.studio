"""The customer's half of account-control verification (ADR 0055): `/api/v1/app/verification`.

Development and the public host, in one database, the way `test_notices_on_production.py` does it:
`MARCELINE_ROLE` says which side a request is on, and production's fetch is wired straight into
development's export code. No network, no real keys.

What is pinned here: the switch closes every route, another organisation's claim is the *same* 404
as an unknown one, a write needs CSRF, production takes the two writes into the outbox and
development applies them once, and a bundle carries this organisation's claims and nobody else's.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from core import public_ids
from delivery import client as delivery_client
from delivery.inbox import Outcome, PulledItem
from delivery.models import OutboxItem, OutboxPullState
from delivery.tests.helpers import KeyPair, make_keys
from marceline import export
from marceline.customer_writes import HANDLERS
from marceline.rights import account_control, services
from marceline.rights import account_control_services as verification
from marceline.rights.models import (
    AccountControlChallenge,
    VerificationMethod,
    VerificationState,
)
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation
from marceline.tests.rights_helpers import staff_user

pytestmark = pytest.mark.django_db

VERIFICATION = f"{APP}/verification"
ACCOUNTS = f"{VERIFICATION}/accounts"
UNKNOWN_ACCOUNT = public_ids.generate("chal")
JSON = "application/json"


@pytest.fixture(autouse=True)
def _on(settings: Settings) -> None:
    settings.ACCOUNT_CONTROL_VERIFICATION_ENABLED = True


@pytest.fixture
def keys(tmp_path: Path, settings: Settings) -> KeyPair:
    pair = make_keys(tmp_path)
    settings.CACHE_SIGNING_KEY_PATH = str(pair.private_path)
    settings.CACHE_VERIFY_KEY_PATH = str(pair.public_path)
    return pair


@pytest.fixture(autouse=True)
def direct_fetch(keys: KeyPair, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Production's fetch, wired into development's export code (no HTTP)."""
    fetched: list[int] = []

    def fetch(organisation_id: int) -> bytes | None:
        fetched.append(organisation_id)
        return export.signed_bundle(organisation_id, now=timezone.now())

    monkeypatch.setattr(delivery_client, "fetch_bundle", fetch)
    return fetched


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network in tests")

    monkeypatch.setattr(delivery_client, "transport", httpx.MockTransport(refuse))
    yield


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def customer(member: User) -> Client:
    return client_for(member)


@pytest.fixture
def staff(db: None) -> User:
    return staff_user()


def claim_body(handle: str = "alpha.creator", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "platform": "fanhub",
        "handle": handle,
        "profile_url": f"https://fanhub.example/{handle}",
    }
    body.update(overrides)
    return body


def post_claim(client: Client, **overrides: Any) -> Any:
    return client.post(ACCOUNTS, claim_body(**overrides), content_type=JSON)


def go_production(settings: Settings) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.OUTBOX_ENABLED = True
    OutboxPullState.objects.get_or_create(defaults={"last_pull_at": timezone.now()})


def development_applies(settings: Settings, item: OutboxItem) -> Outcome:
    role = settings.MARCELINE_ROLE
    settings.MARCELINE_ROLE = "dev"
    settings.CACHE_PURGE_OUTBOX = True
    try:
        return HANDLERS[item.kind](
            PulledItem(
                id=item.public_id,
                organisation_id=item.organisation_id,
                user_id=item.user_id,
                kind=item.kind,
                payload=item.payload,
                received_at=item.received_at,
            )
        )
    finally:
        settings.MARCELINE_ROLE = role


# --- the switch -----------------------------------------------------------------------------------


def test_with_the_switch_off_every_route_is_404(settings: Settings, customer: Client) -> None:
    settings.ACCOUNT_CONTROL_VERIFICATION_ENABLED = False

    assert customer.get(VERIFICATION).status_code == 404
    assert post_claim(customer).status_code == 404
    assert customer.post(f"{ACCOUNTS}/{UNKNOWN_ACCOUNT}/placed").status_code == 404


def test_a_signed_out_caller_gets_401(client: Client) -> None:
    assert client.get(VERIFICATION).status_code == 401


# --- the flow -------------------------------------------------------------------------------------


def test_the_status_lists_the_platforms_and_nothing_claimed_yet(customer: Client) -> None:
    body = customer.get(VERIFICATION).json()

    assert body["identity_status"] == "unverified"
    assert body["method"] is None
    assert body["code_ttl_hours"] == 72
    assert body["accounts"] == []
    assert {platform["code"] for platform in body["platforms"]} == set(
        account_control.PLATFORM_CODES
    )
    fanhub = next(p for p in body["platforms"] if p["code"] == "fanhub")
    assert fanhub["hosts"] == ["fanhub.example"]
    assert fanhub["code_accepted"] is True
    # ADR 0055 (operator review 2026-09-19): nothing checked who the creator is before they
    # published on their own site, so no code is offered for it.
    own = next(p for p in body["platforms"] if p["code"] == "own_website")
    assert own["hosts"] == [] and own["code_accepted"] is False


def test_claiming_an_account_answers_the_code_to_place(customer: Client) -> None:
    response = post_claim(customer)

    assert response.status_code == 200, response.content
    account = response.json()["accounts"][0]
    assert account["state"] == "pending"
    assert account_control.looks_like_code(account["code"])
    assert account["handle"] == "alpha.creator"
    assert account["id"].startswith("chal_")
    assert account["method"] is None


def test_a_handle_or_link_we_will_not_store_is_a_409_with_a_code(customer: Client) -> None:
    refused = post_claim(customer, handle="a b", profile_url="")
    assert refused.status_code == 409
    assert refused.json() == {"detail": "invalid_handle"}

    elsewhere = customer.post(
        ACCOUNTS,
        claim_body(profile_url="https://fanhub.example.evil.example/alpha.creator"),
        content_type=JSON,
    )
    assert elsewhere.status_code == 409
    assert elsewhere.json() == {"detail": "host_not_allowed"}


def test_a_video_call_platform_is_claimed_without_a_code(customer: Client) -> None:
    response = customer.post(
        ACCOUNTS,
        {"platform": "own_website", "handle": "alpha-site", "profile_url": ""},
        content_type=JSON,
    )

    assert response.status_code == 200, response.content
    account = response.json()["accounts"][0]
    assert account["code"] is None
    assert account["state"] == "pending"

    placed = customer.post(f"{ACCOUNTS}/{account['id']}/placed")

    assert placed.status_code == 409
    assert placed.json() == {"detail": "video_call_required"}


def test_the_creator_says_the_code_is_up(customer: Client) -> None:
    account = post_claim(customer).json()["accounts"][0]

    response = customer.post(f"{ACCOUNTS}/{account['id']}/placed")

    assert response.status_code == 200, response.content
    assert response.json()["accounts"][0]["state"] == "placed"
    assert response.json()["accounts"][0]["placed_at"] is not None


def test_the_status_reports_the_method_once_staff_recorded_it(
    customer: Client, organisation: Organisation, staff: User
) -> None:
    post_claim(customer)
    challenge = AccountControlChallenge.objects.get()
    verification.verify(
        challenge, method=VerificationMethod.BIO_CODE, actor=services.Actor.staff(staff)
    )

    body = customer.get(VERIFICATION).json()

    assert body["identity_status"] == "verified"
    assert body["method"] == "bio_code"
    assert body["accounts"][0]["state"] == "verified"
    assert body["accounts"][0]["code"] is None, "a settled claim shows no code"
    assert body["accounts"][0]["method"] == "bio_code"


# --- tenancy and CSRF -----------------------------------------------------------------------------


def test_another_organisations_claim_is_the_same_404_as_an_unknown_one(
    customer: Client, staff: User
) -> None:
    other = make_organisation("Beta Films")
    other_member = add_member(other, "beta-owner")
    theirs = verification.claim_account(
        other,
        verification.ClaimInput(platform="fanbase", handle="beta"),
        services.Actor.customer(other_member),
    )

    foreign = customer.post(f"{ACCOUNTS}/{theirs.public_id}/placed")
    unknown = customer.post(f"{ACCOUNTS}/{UNKNOWN_ACCOUNT}/placed")
    malformed = customer.post(f"{ACCOUNTS}/not-an-id/placed")

    assert foreign.status_code == unknown.status_code == malformed.status_code == 404
    assert foreign.content == unknown.content == malformed.content
    theirs.refresh_from_db()
    assert theirs.state == VerificationState.PENDING, "nothing of theirs moved"


def test_the_status_never_carries_another_organisations_claim(
    customer: Client, staff: User
) -> None:
    other = make_organisation("Beta Films")
    other_member = add_member(other, "beta-owner")
    theirs = verification.claim_account(
        other,
        verification.ClaimInput(platform="fanbase", handle="beta"),
        services.Actor.customer(other_member),
    )
    post_claim(customer)

    body = customer.get(VERIFICATION).content.decode()

    assert theirs.public_id not in body
    assert theirs.code not in body
    assert "beta" not in body


def test_a_write_without_a_csrf_token_is_refused(member: User) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(member)

    assert client.post(ACCOUNTS, claim_body(), content_type=JSON).status_code == 403
    assert client.post(f"{ACCOUNTS}/{UNKNOWN_ACCOUNT}/placed").status_code == 403
    assert AccountControlChallenge.objects.count() == 0


# --- the public host ------------------------------------------------------------------------------


def test_production_answers_the_status_from_the_signed_bundle(
    settings: Settings, customer: Client, direct_fetch: list[int], organisation: Organisation
) -> None:
    post_claim(customer)
    on_dev = customer.get(VERIFICATION)

    go_production(settings)
    on_prod = customer.get(VERIFICATION)

    assert on_prod.status_code == 200
    assert on_prod.content == on_dev.content
    assert on_prod["Cache-Control"] == "private, no-store"
    assert direct_fetch == [organisation.pk]


def test_the_bundle_carries_this_organisations_claims_and_no_others(
    organisation: Organisation, member: User, customer: Client
) -> None:
    other = make_organisation("Beta Films")
    other_member = add_member(other, "beta-owner")
    theirs = verification.claim_account(
        other,
        verification.ClaimInput(platform="fanbase", handle="beta"),
        services.Actor.customer(other_member),
    )
    mine = post_claim(customer).json()["accounts"][0]

    payload = export.bundle_payload(organisation, now=timezone.now())

    assert payload is not None
    document = payload["documents"]["verification"]
    assert document["status"] == 200
    assert mine["id"] in document["body"]
    assert theirs.public_id not in document["body"]
    assert theirs.code not in document["body"]


def test_production_takes_both_writes_into_the_outbox(
    settings: Settings, customer: Client, organisation: Organisation, member: User
) -> None:
    go_production(settings)

    claimed = post_claim(customer)

    assert claimed.status_code == 202
    assert claimed.json() == {"pending": True}
    assert AccountControlChallenge.objects.count() == 0, "production stores no challenge of its own"
    item = OutboxItem.objects.get()
    assert item.kind == OutboxItem.Kind.VERIFICATION_CLAIM
    assert item.organisation_id == organisation.pk

    outcome = development_applies(settings, item)

    assert outcome.outcome == "done"
    challenge = AccountControlChallenge.objects.get()
    assert outcome.original_id == challenge.public_id
    assert account_control.looks_like_code(challenge.code), "the code is made where staff read it"

    placed = customer.post(f"{ACCOUNTS}/{challenge.public_id}/placed")

    assert placed.status_code == 202
    second = OutboxItem.objects.exclude(pk=item.pk).get()
    assert second.kind == OutboxItem.Kind.VERIFICATION_PLACED
    assert development_applies(settings, second).outcome == "done"
    challenge.refresh_from_db()
    assert challenge.state == VerificationState.PLACED


def test_applying_the_same_write_twice_changes_nothing(
    settings: Settings, customer: Client, organisation: Organisation
) -> None:
    """The inbox applies an item once (`InboxItem` in the applying transaction); a handler run twice
    by hand must still leave one row and one live code."""
    go_production(settings)
    post_claim(customer)
    item = OutboxItem.objects.get()

    first = development_applies(settings, item)
    challenge = AccountControlChallenge.objects.get()
    code = challenge.code
    second = development_applies(settings, item)

    assert first.outcome == second.outcome == "done"
    assert AccountControlChallenge.objects.count() == 1
    challenge.refresh_from_db()
    assert challenge.issue_count == 2, "a repeat is a reissue, never a second claim"
    assert challenge.code != code, "and the code it replaced no longer works"


def test_a_write_naming_another_organisations_claim_is_refused_when_applied(
    settings: Settings, customer: Client, organisation: Organisation, member: User
) -> None:
    other = make_organisation("Beta Films")
    other_member = add_member(other, "beta-owner")
    theirs = verification.claim_account(
        other,
        verification.ClaimInput(platform="fanbase", handle="beta"),
        services.Actor.customer(other_member),
    )
    go_production(settings)

    accepted = customer.post(f"{ACCOUNTS}/{theirs.public_id}/placed")
    assert accepted.status_code == 202, "production holds no rows, so it cannot judge this"

    outcome = development_applies(settings, OutboxItem.objects.get())

    assert outcome == Outcome("refused", "account_not_found")
    theirs.refresh_from_db()
    assert theirs.state == VerificationState.PENDING


def test_with_the_outbox_off_production_saves_nothing(settings: Settings, customer: Client) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.OUTBOX_ENABLED = False

    response = post_claim(customer)

    assert response.status_code == 503
    assert response.json() == {"detail": "verification_write_unavailable"}
    assert OutboxItem.objects.count() == 0
