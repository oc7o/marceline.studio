"""The creator's own data requests (ADR 0056, issue #133): `/api/v1/app/data-requests`.

Development and the public host in one database, the way `test_app_verification.py` does it:
`MARCELINE_ROLE` says which side a request is on, and production's fetch is wired straight into
development's export code. No network, no real keys.

What is pinned here: the switch closes every route, one open request per kind per organisation,
another organisation's request is invisible rather than forbidden, a write needs CSRF, a deletion
needs the typed word on **both** machines, production takes the writes into the outbox and applying
one twice changes nothing, a bundle carries this organisation's requests and nobody else's, and —
the rule the whole feature rests on — **a request neither exports nor deletes anything**.
"""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from delivery import client as delivery_client
from delivery.inbox import Outcome, PulledItem
from delivery.models import OutboxItem, OutboxPullState
from delivery.tests.helpers import KeyPair, make_keys
from marceline import export
from marceline.customer_writes import HANDLERS
from marceline.deletion import requests as data_requests
from marceline.deletion.models import CustomerDataRequest, OrganisationEnding
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation

pytestmark = pytest.mark.django_db

REQUESTS = f"{APP}/data-requests"
EXPORT = f"{REQUESTS}/export"
DELETION = f"{REQUESTS}/deletion"
JSON = "application/json"
PHRASE = {"confirm": data_requests.DELETION_PHRASE}


@pytest.fixture(autouse=True)
def _on(settings: Settings) -> None:
    settings.CUSTOMER_DATA_REQUESTS_ENABLED = True


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
    settings.CUSTOMER_DATA_REQUESTS_ENABLED = False

    assert customer.get(REQUESTS).status_code == 404
    assert customer.post(EXPORT).status_code == 404
    assert customer.post(DELETION, PHRASE, content_type=JSON).status_code == 404
    assert customer.post(f"{EXPORT}/withdraw").status_code == 404
    assert customer.post(f"{DELETION}/withdraw").status_code == 404
    assert CustomerDataRequest.objects.count() == 0


def test_a_signed_out_caller_gets_401(client: Client) -> None:
    assert client.get(REQUESTS).status_code == 401


def test_the_account_says_whether_asking_is_offered(settings: Settings, customer: Client) -> None:
    assert customer.get(f"{APP}/me").json()["can_request_data"] is True

    settings.CUSTOMER_DATA_REQUESTS_ENABLED = False

    assert customer.get(f"{APP}/me").json()["can_request_data"] is False


# --- asking ---------------------------------------------------------------------------------------


def test_the_status_carries_the_address_the_word_and_nothing_asked_for_yet(
    customer: Client,
) -> None:
    body = customer.get(REQUESTS).json()

    assert body["requests"] == []
    assert body["contact_email"] == data_requests.CONTACT_EMAIL
    # Language-independent on purpose: the document a public host serves from the bundle is too.
    assert body["deletion_phrase"] == data_requests.DELETION_PHRASE


def test_asking_for_a_copy_records_the_request_and_its_deadline(customer: Client) -> None:
    response = customer.post(EXPORT)

    assert response.status_code == 200, response.content
    row = response.json()["requests"][0]
    assert row["kind"] == "export"
    assert row["status"] == "requested"
    assert row["open"] is True
    assert row["settled_at"] is None
    stored = CustomerDataRequest.objects.get()
    due = datetime.fromisoformat(row["due_at"])
    assert abs((due - (stored.requested_at + data_requests.DEADLINE)).total_seconds()) < 1


def test_asking_exports_nothing_and_deletes_nothing(customer: Client) -> None:
    """The rule the feature rests on (ADR 0045, ADR 0056): a request is a request.

    No ending is marked, so no retention clock starts, and nothing here renders an export document.
    """
    customer.post(EXPORT)
    customer.post(DELETION, PHRASE, content_type=JSON)

    assert CustomerDataRequest.objects.count() == 2
    assert not OrganisationEnding.objects.exists()


def test_a_deletion_needs_the_word_typed_exactly(customer: Client) -> None:
    refused = customer.post(DELETION, {"confirm": "delete please"}, content_type=JSON)

    assert refused.status_code == 409
    assert refused.json() == {"detail": "confirmation_mismatch"}
    assert CustomerDataRequest.objects.count() == 0

    accepted = customer.post(DELETION, {"confirm": f" {PHRASE['confirm']} "}, content_type=JSON)

    assert accepted.status_code == 200, "surrounding space is a typing artefact, not a mismatch"


def test_only_one_request_per_kind_is_open_at_a_time(customer: Client) -> None:
    assert customer.post(EXPORT).status_code == 200

    again = customer.post(EXPORT)

    assert again.status_code == 409
    assert again.json() == {"detail": "already_open"}
    assert CustomerDataRequest.objects.count() == 1
    # The other kind is its own throttle, not the same one.
    assert customer.post(DELETION, PHRASE, content_type=JSON).status_code == 200
    assert CustomerDataRequest.objects.count() == 2


def test_a_settled_request_can_be_made_again(customer: Client, member: User) -> None:
    customer.post(EXPORT)
    data_requests.settle(CustomerDataRequest.objects.get(), note="sent by hand")

    assert customer.post(EXPORT).status_code == 200
    assert CustomerDataRequest.objects.count() == 2, "the answered one stays as the record"


# --- withdrawing ----------------------------------------------------------------------------------


def test_an_open_request_can_be_taken_back(customer: Client) -> None:
    customer.post(DELETION, PHRASE, content_type=JSON)

    response = customer.post(f"{DELETION}/withdraw")

    assert response.status_code == 200
    row = response.json()["requests"][0]
    assert row["status"] == "withdrawn"
    assert row["open"] is False
    assert row["settled_at"] is not None
    assert customer.post(DELETION, PHRASE, content_type=JSON).status_code == 200, "and asked again"


def test_withdrawing_nothing_is_a_409_not_a_silent_success(customer: Client) -> None:
    response = customer.post(f"{EXPORT}/withdraw")

    assert response.status_code == 409
    assert response.json() == {"detail": "not_open"}


def test_an_answered_request_is_never_withdrawn(customer: Client) -> None:
    customer.post(EXPORT)
    data_requests.settle(CustomerDataRequest.objects.get())

    assert customer.post(f"{EXPORT}/withdraw").status_code == 409
    assert CustomerDataRequest.objects.get().status == CustomerDataRequest.Status.DONE


# --- tenancy and CSRF -----------------------------------------------------------------------------


def test_another_organisations_request_is_invisible_and_untouchable(customer: Client) -> None:
    other = make_organisation("Beta Films")
    other_member = add_member(other, "beta-owner")
    theirs = data_requests.open_request(other, CustomerDataRequest.Kind.DELETION, by=other_member)

    body = customer.get(REQUESTS).json()
    withdrawn = customer.post(f"{DELETION}/withdraw")
    unknown = customer.post(f"{EXPORT}/withdraw")

    assert body["requests"] == []
    # The same answer as "there is nothing of mine open", byte for byte: the paths name a kind, so
    # there is no id a caller could point at somebody else's row with, and nothing says it exists.
    assert withdrawn.status_code == unknown.status_code == 409
    assert withdrawn.content == unknown.content
    theirs.refresh_from_db()
    assert theirs.status == CustomerDataRequest.Status.REQUESTED


def test_a_write_without_a_csrf_token_is_refused(member: User) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(member)

    assert client.post(EXPORT).status_code == 403
    assert client.post(DELETION, PHRASE, content_type=JSON).status_code == 403
    assert client.post(f"{EXPORT}/withdraw").status_code == 403
    assert CustomerDataRequest.objects.count() == 0


# --- the public host ------------------------------------------------------------------------------


def test_production_answers_the_status_from_the_signed_bundle(
    settings: Settings, customer: Client, direct_fetch: list[int], organisation: Organisation
) -> None:
    customer.post(EXPORT)
    on_dev = customer.get(REQUESTS)

    go_production(settings)
    on_prod = customer.get(REQUESTS)

    assert on_prod.status_code == 200
    assert on_prod.content == on_dev.content
    assert on_prod["Cache-Control"] == "private, no-store"
    assert direct_fetch == [organisation.pk]


def test_the_bundle_carries_this_organisations_requests_and_no_others(
    organisation: Organisation, customer: Client
) -> None:
    other = make_organisation("Beta Films")
    other_member = add_member(other, "beta-owner")
    data_requests.open_request(other, CustomerDataRequest.Kind.EXPORT, by=other_member)
    customer.post(DELETION, PHRASE, content_type=JSON)

    payload = export.bundle_payload(organisation, now=timezone.now())

    assert payload is not None
    document = payload["documents"]["data-requests"]
    assert document["status"] == 200
    assert document["body"].count('"kind"') == 1
    assert '"deletion"' in document["body"]
    assert '"export"' not in document["body"], "and not a trace of the other organisation's"


def test_production_takes_both_writes_into_the_outbox(
    settings: Settings, customer: Client, organisation: Organisation
) -> None:
    go_production(settings)

    asked = customer.post(DELETION, PHRASE, content_type=JSON)

    assert asked.status_code == 202
    assert asked.json() == {"pending": True}
    assert CustomerDataRequest.objects.count() == 0, "production stores no request of its own"
    item = OutboxItem.objects.get()
    assert item.kind == OutboxItem.Kind.DATA_REQUEST_OPEN
    assert item.organisation_id == organisation.pk

    assert development_applies(settings, item) == Outcome("done", "deletion_requested")
    row = CustomerDataRequest.objects.get()
    assert row.kind == CustomerDataRequest.Kind.DELETION
    assert row.open

    taken_back = customer.post(f"{DELETION}/withdraw")

    assert taken_back.status_code == 202
    second = OutboxItem.objects.exclude(pk=item.pk).get()
    assert second.kind == OutboxItem.Kind.DATA_REQUEST_WITHDRAW
    assert development_applies(settings, second) == Outcome("done", "deletion_withdrawn")
    row.refresh_from_db()
    assert row.status == CustomerDataRequest.Status.WITHDRAWN


def test_applying_the_same_write_twice_leaves_one_request(
    settings: Settings, customer: Client
) -> None:
    """The inbox applies an item once (`InboxItem` in the applying transaction); a handler run twice
    by hand must still leave one request, which the partial unique index guarantees."""
    go_production(settings)
    customer.post(EXPORT)
    item = OutboxItem.objects.get()

    first = development_applies(settings, item)
    second = development_applies(settings, item)

    assert first == Outcome("done", "export_requested")
    assert second == Outcome("refused", "already_open")
    assert CustomerDataRequest.objects.count() == 1


def test_a_double_press_on_production_is_one_write(settings: Settings, customer: Client) -> None:
    go_production(settings)

    assert customer.post(EXPORT).status_code == 202
    assert customer.post(EXPORT).status_code == 202

    assert OutboxItem.objects.count() == 1, "the dedupe key is the request kind"


def test_development_re_checks_the_typed_word_when_it_applies(
    settings: Settings, organisation: Organisation, member: User
) -> None:
    """A write that travels between two machines is judged again where it is acted on.

    Production checked the word, but production is the public host: a deletion request is the last
    write in the product to take on trust.
    """
    go_production(settings)
    item = OutboxItem.objects.create(
        kind=OutboxItem.Kind.DATA_REQUEST_OPEN,
        organisation_id=organisation.pk,
        user_id=member.pk,
        payload='{"kind": "deletion", "confirm": "nope"}',
        payload_bytes=40,
        received_at=timezone.now(),
    )

    assert development_applies(settings, item) == Outcome("refused", "confirmation_mismatch")
    assert CustomerDataRequest.objects.count() == 0


def test_a_write_pulled_while_the_switch_is_off_is_refused_not_applied(
    settings: Settings, customer: Client
) -> None:
    go_production(settings)
    customer.post(EXPORT)
    item = OutboxItem.objects.get()
    settings.CUSTOMER_DATA_REQUESTS_ENABLED = False

    assert development_applies(settings, item) == Outcome("refused", "data_requests_unavailable")
    assert CustomerDataRequest.objects.count() == 0


def test_with_the_outbox_off_production_saves_nothing(settings: Settings, customer: Client) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.OUTBOX_ENABLED = False

    response = customer.post(EXPORT)

    assert response.status_code == 503
    assert response.json() == {"detail": "data_request_write_unavailable"}
    assert OutboxItem.objects.count() == 0


# --- what staff and the report see ----------------------------------------------------------------


def test_the_waiting_count_is_what_the_daily_report_reads(customer: Client) -> None:
    now = timezone.now()
    assert data_requests.waiting(now) == data_requests.Waiting(waiting=0, oldest_days=None)

    customer.post(EXPORT)
    row = CustomerDataRequest.objects.get()
    row.requested_at = now - data_requests.DEADLINE
    row.save(update_fields=("requested_at",))

    counted = data_requests.waiting(now)

    assert counted.waiting == 1
    assert counted.oldest_days == data_requests.DEADLINE.days
    # A settled one is not waiting any more.
    data_requests.settle(row, now=now)
    assert data_requests.waiting(now).waiting == 0


def test_the_export_document_carries_the_requests(
    customer: Client, organisation: Organisation
) -> None:
    from marceline.deletion.export import data_requests_of

    customer.post(EXPORT)

    assert [row["kind"] for row in data_requests_of(organisation)] == ["export"]
