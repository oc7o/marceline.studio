"""Customer writes on production reach development and come back (ADR 0028 amendment "The
production write path").

One database plays both machines, as in `test_cache_roundtrip.py`: `MARCELINE_ROLE` says which side
a request is on. Development's pull goes over HTTP — `delivery.client` with an
`httpx.MockTransport` that hands the request to production's internal listener (Django's test
client, switched to `MARCELINE_ROLE=prod` / `DJANGO_PUBLIC_SURFACE=internal` for that one request).
Each machine has its own throwaway key pair; no network.
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Membership, Organisation
from delivery import client as delivery_client
from delivery.models import CachedBundle, InboxItem, OutboxItem, OutboxPullState, PurgeOutbox
from delivery.tests.helpers import KeyPair, make_keys
from marceline import export
from marceline.app import fingerprints
from marceline.app.ownership import public_ids_for
from marceline.models import OriginalOwnership, UploadAttempt
from marceline.rights import services as rights_services
from marceline.rights.declarations import (
    CURRENT_AUTHORITY_DECLARATION,
    CURRENT_WORK_DECLARATION,
)
from marceline.rights.models import RightsClaimant, RightsEvent, WorkRights
from marceline.tests.app_helpers import (
    APP,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from marceline.tests.test_app_fingerprints import body, document
from matching.models import ClientEvent, Reference

pytestmark = pytest.mark.django_db

URL = f"{APP}/fingerprints"
SUBMISSIONS = f"{URL}/submissions"
RIGHTS = f"{APP}/rights"
PROD_INTERNAL = "http://marceline-prod.test:8081"


@pytest.fixture
def keys(tmp_path: Path, settings: Settings) -> tuple[KeyPair, KeyPair]:
    dev = make_keys(tmp_path, "dev")
    prod = make_keys(tmp_path, "prod")
    # Development: its private key, production's public key, production's internal listener.
    settings.CACHE_SIGNING_KEY_PATH = str(dev.private_path)
    settings.OUTBOX_VERIFY_KEY_PATH = str(prod.public_path)
    settings.CACHE_PURGE_BASE_URL = PROD_INTERNAL
    # Production: development's public key, its own private key, the outbox switched on.
    settings.CACHE_VERIFY_KEY_PATH = str(dev.public_path)
    settings.OUTBOX_SIGNING_KEY_PATH = str(prod.private_path)
    settings.OUTBOX_ENABLED = True
    return dev, prod


@pytest.fixture
def studio() -> Organisation:
    return make_organisation("Novak Studio")


@pytest.fixture
def member(studio: Organisation) -> User:
    return add_member(studio, "member@studio.test")


@pytest.fixture
def customer(member: User) -> Client:
    return client_for(member)


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
        return httpx.Response(reply.status_code, content=reply.content)

    monkeypatch.setattr(delivery_client, "transport", httpx.MockTransport(answer))
    yield


def development_pulls(settings: Settings) -> None:
    role = settings.MARCELINE_ROLE
    settings.MARCELINE_ROLE = "dev"
    try:
        call_command("pull_customer_writes")
    finally:
        settings.MARCELINE_ROLE = role


def post(client: Client, payload: dict[str, Any], path: str = URL) -> Any:
    return client.post(path, data=payload, content_type="application/json")


@pytest.mark.usefixtures("on_production")
def test_production_accepts_an_original_and_development_adds_it(
    settings: Settings, customer: Client, studio: Organisation
) -> None:
    assert customer.get(f"{APP}/me").json()["can_add_originals"] is True

    accepted = post(customer, body())

    assert accepted.status_code == 202, accepted.content
    submission = accepted.json()["submission"]
    assert submission["id"].startswith("sub_")
    assert (submission["status"], submission["code"], submission["title"]) == (
        "processing",
        "",
        "Beach day",
    )
    assert submission["original_id"] is None
    assert not Reference.objects.exists(), "production writes no originals"
    assert UploadAttempt.objects.count() == 1, "rate limited on production"
    assert customer.get(SUBMISSIONS).json() == [submission]

    development_pulls(settings)

    reference = Reference.objects.get()
    assert OriginalOwnership.objects.get(reference=reference).organisation == studio
    assert reference.rights_confirmation.language == "en"
    [settled] = customer.get(SUBMISSIONS).json()
    assert (settled["id"], settled["status"], settled["code"]) == (
        submission["id"],
        "done",
        "created",
    )
    assert settled["original_id"] is not None and settled["original_id"].startswith("orig_")
    item = OutboxItem.objects.get()
    assert item.payload == "", "hashes leave production as soon as the write settled"
    assert InboxItem.objects.get().acknowledged_at is not None
    assert PurgeOutbox.objects.filter(reason="customer_write", mode="expire").exists()


@pytest.mark.usefixtures("on_production")
def test_the_new_original_shows_up_through_the_cache_at_once(
    settings: Settings,
    customer: Client,
    studio: Organisation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        delivery_client,
        "fetch_bundle",
        lambda organisation_id: export.signed_bundle(organisation_id, now=timezone.now()),
    )
    assert customer.get(f"{APP}/originals").json() == []
    assert CachedBundle.objects.get(organisation_id=studio.pk).expired is False

    post(customer, body())
    development_pulls(settings)

    assert CachedBundle.objects.get(organisation_id=studio.pk).expired is True
    originals = customer.get(f"{APP}/originals").json()
    [settled] = customer.get(SUBMISSIONS).json()
    assert [original["id"] for original in originals] == [settled["original_id"]]


@pytest.mark.usefixtures("on_production")
def test_a_claim_on_another_organisations_file_comes_back_as_refused(
    settings: Settings, customer: Client
) -> None:
    rival = client_for(add_member(make_organisation("Rival"), "member@rival.test"))
    settings.MARCELINE_ROLE = "dev"
    assert post(rival, body()).status_code == 201
    settings.MARCELINE_ROLE = "prod"

    assert post(customer, body()).status_code == 202
    development_pulls(settings)

    [refused] = customer.get(SUBMISSIONS).json()
    assert (refused["status"], refused["code"], refused["original_id"]) == (
        "refused",
        "claim_needs_review",
        None,
    )
    assert Reference.objects.count() == 1


@pytest.mark.usefixtures("on_production")
def test_a_member_who_left_before_development_applied_adds_nothing(
    settings: Settings, customer: Client, member: User
) -> None:
    first = post(customer, body()).json()["submission"]["id"]
    second = post(customer, body(document(sha256="cd" * 32, first_hash_byte=90))).json()
    Membership.objects.filter(user=member).update(organisation=make_organisation("Elsewhere"))

    development_pulls(settings)

    codes = dict(OutboxItem.objects.values_list("public_id", "result_code"))
    assert codes == {first: "no_organisation", second["submission"]["id"]: "no_organisation"}
    assert not Reference.objects.exists()


@pytest.mark.usefixtures("on_production")
def test_an_outdated_declaration_is_refused_on_development(
    settings: Settings, customer: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    post(customer, body())
    monkeypatch.setattr(fingerprints, "CURRENT_VERSION", "2099-01-01")

    development_pulls(settings)

    assert OutboxItem.objects.get().result_code == "rights_confirmation_required"
    assert not Reference.objects.exists()


@pytest.mark.usefixtures("on_production")
def test_writes_are_refused_while_development_has_not_pulled(
    settings: Settings, customer: Client
) -> None:
    OutboxPullState.objects.update(last_pull_at=timezone.now() - timedelta(hours=25))

    response = post(customer, body())
    event = post(customer, {"kind": "client_failed"}, f"{URL}/client-events")

    assert response.status_code == 503
    assert response.json()["detail"] == "processing_paused"
    assert response["Retry-After"] == "3600"
    assert event.status_code == 503
    assert not OutboxItem.objects.exists()
    assert not UploadAttempt.objects.exists()


def test_production_without_the_outbox_takes_no_writes(
    settings: Settings, customer: Client
) -> None:
    settings.MARCELINE_ROLE = "prod"

    assert customer.get(f"{APP}/me").json()["can_add_originals"] is False
    assert post(customer, body()).json()["detail"] == "adding_originals_unavailable"
    assert not OutboxItem.objects.exists()


@pytest.mark.usefixtures("on_production")
def test_client_events_travel_the_same_way(
    settings: Settings, customer: Client, member: User
) -> None:
    response = post(
        customer,
        {"kind": "codec_unsupported", "container": "video/quicktime"},
        f"{URL}/client-events",
    )

    assert response.json() == {"recorded": True}
    assert not ClientEvent.objects.exists()
    assert customer.get(SUBMISSIONS).json() == [], "counters are not submissions"
    development_pulls(settings)
    event = ClientEvent.objects.get()
    assert (event.kind, event.user, event.container) == (
        "codec_unsupported",
        member,
        "video/quicktime",
    )


@pytest.mark.usefixtures("on_production")
def test_submissions_are_scoped_to_the_organisation(customer: Client) -> None:
    post(customer, body())
    rival = client_for(add_member(make_organisation("Rival"), "member@rival.test"))
    loner = client_for(User.objects.create_user("loner", password="secret-pass-123"))

    assert len(customer.get(SUBMISSIONS).json()) == 1
    assert rival.get(SUBMISSIONS).json() == []
    assert loner.get(SUBMISSIONS).json() == []
    assert Client().get(SUBMISSIONS).status_code == 401


# --- rights writes (ADR 0034) --------------------------------------------------------------------


def _declaration(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "capacity": "owner",
        "asserted_right": "film_producer_right",
        "declaration_version": CURRENT_WORK_DECLARATION.version,
        "language": "en",
        "confirmed": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.usefixtures("on_production")
def test_production_accepts_a_claimant_type_choice_and_development_applies_it(
    settings: Settings, customer: Client, studio: Organisation
) -> None:
    accepted = customer.put(
        f"{RIGHTS}/claimant",
        {"claimant_type": "studio_producer"},
        content_type="application/json",
    )

    assert accepted.status_code == 202, accepted.content
    assert accepted.json() == {"pending": True}
    assert not RightsClaimant.objects.filter(claimant_type="studio_producer").exists()
    assert UploadAttempt.objects.count() == 1, "rate limited like fingerprint uploads"

    development_pulls(settings)

    claimant = RightsClaimant.objects.get(organisation=studio)
    assert claimant.claimant_type == "studio_producer"
    item = OutboxItem.objects.get()
    assert (item.status, item.result_code) == ("done", "claimant_type_set")
    assert item.payload == "", "the write leaves production as soon as it settled"
    assert InboxItem.objects.get().acknowledged_at is not None


@pytest.mark.usefixtures("on_production")
def test_production_accepts_a_self_declaration_and_development_applies_it(
    settings: Settings, customer: Client, member: User
) -> None:
    reference = make_reference(member, title="Scene one")
    original_id = public_id_of(reference)

    accepted = customer.post(
        f"{RIGHTS}/works/{original_id}/declaration",
        _declaration(),
        content_type="application/json",
    )

    assert accepted.status_code == 202, accepted.content
    assert accepted.json() == {"pending": True}
    assert not WorkRights.objects.filter(sha256=reference.sha256).exists()

    development_pulls(settings)

    work = WorkRights.objects.get(sha256=reference.sha256)
    assert (work.status, work.capacity, work.asserted_right) == (
        "self_declared",
        "owner",
        "film_producer_right",
    )
    assert OutboxItem.objects.get().result_code == "declared"


@pytest.mark.usefixtures("on_production")
def test_production_accepts_an_authority_declaration_and_development_applies_it(
    settings: Settings, customer: Client, studio: Organisation, member: User
) -> None:
    """The customer's statement about their documents travels like every other rights write: into
    the outbox, out again, applied by the same `rights.services` call (ADR 0028 write path)."""
    reference = make_reference(member, title="Scene two")
    staff_actor = rights_services.Actor.staff(None)
    work = rights_services.open_record(studio, reference.sha256, staff_actor)
    work = rights_services.transition(
        work, "documents_received", staff_actor, reason="excerpt received"
    )
    rights_services.transition(work, "documents_complete", staff_actor, reason="fields present")

    accepted = customer.post(
        f"{RIGHTS}/works/{public_id_of(reference)}/authority",
        {
            "declaration_version": CURRENT_AUTHORITY_DECLARATION.version,
            "language": "en",
            "confirmed": True,
        },
        content_type="application/json",
    )

    assert accepted.status_code == 202, accepted.content
    assert WorkRights.objects.get(sha256=reference.sha256).status == "documents_complete"

    development_pulls(settings)

    work = WorkRights.objects.get(sha256=reference.sha256)
    assert work.status == "authority_declared"
    assert work.authority_declared_by == member, "recorded as the customer's own statement"
    assert OutboxItem.objects.get().result_code == "authority_declared"


@pytest.mark.usefixtures("on_production")
def test_an_upload_and_a_declaration_of_the_same_work_apply_in_order(
    settings: Settings, customer: Client, studio: Organisation
) -> None:
    """The customer's own declaration must not be applied before the upload created the work.

    The original's public id is deterministic per (organisation, sha256) (ADR 0033), so the test
    can address it before development has even created the original — a real customer cannot do
    that (they only see it once the upload settles), but this is exactly what makes the ordering
    guarantee worth checking: if the two items applied out of order, the declaration would find no
    original yet and come back `original_not_found` instead of `declared`.
    """
    upload_body = body()
    sha256 = upload_body["fingerprint"]["sha256"]
    original_id = public_ids_for(studio, [sha256])[sha256]

    upload = post(customer, upload_body)
    declaration = customer.post(
        f"{RIGHTS}/works/{original_id}/declaration", _declaration(), content_type="application/json"
    )

    assert upload.status_code == declaration.status_code == 202

    development_pulls(settings)

    reference = Reference.objects.get(sha256=sha256)
    work = WorkRights.objects.get(sha256=sha256)
    assert (work.status, work.capacity) == ("self_declared", "owner"), "the declaration applied"
    assert reference.rights_confirmation.work_rights == work
    codes = list(OutboxItem.objects.order_by("pk").values_list("result_code", flat=True))
    assert codes == ["created", "declared"], "applied in acceptance order, not reordered"


@pytest.mark.usefixtures("on_production")
def test_a_rights_write_is_applied_once_even_when_pulled_twice(
    settings: Settings, customer: Client, studio: Organisation
) -> None:
    customer.put(
        f"{RIGHTS}/claimant", {"claimant_type": "studio_producer"}, content_type="application/json"
    )
    development_pulls(settings)
    assert RightsClaimant.objects.get(organisation=studio).claimant_type == "studio_producer"
    events_after_first_apply = RightsEvent.objects.count()

    # Simulate production's acknowledgement never arriving (as in delivery/tests/test_inbox.py's
    # "lost" scenario): its own copy is still pending, and development has not told it otherwise
    # yet, so the next pull resends the very item development already applied.
    OutboxItem.objects.update(status=OutboxItem.Status.PENDING)
    InboxItem.objects.update(acknowledged_at=None)
    development_pulls(settings)

    assert RightsEvent.objects.count() == events_after_first_apply, "not applied a second time"
    assert OutboxItem.objects.get().status == "done"


@pytest.mark.usefixtures("on_production")
def test_proof_uploads_are_still_refused_on_production(customer: Client) -> None:
    response = customer.post(f"{RIGHTS}/proofs")

    assert response.status_code == 404
    assert not OutboxItem.objects.exists()


# --- the internal listener ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/internal/v1/outbox/pull", "/internal/v1/outbox/ack"])
@pytest.mark.parametrize(
    ("role", "surface", "enabled"),
    [("prod", "customer", True), ("dev", "full", True), ("prod", "internal", False)],
)
def test_the_outbox_is_404_off_production_s_internal_listener(
    settings: Settings,
    keys: tuple[KeyPair, KeyPair],
    path: str,
    role: str,
    surface: str,
    enabled: bool,
) -> None:
    settings.MARCELINE_ROLE, settings.PUBLIC_SURFACE, settings.OUTBOX_ENABLED = (
        role,
        surface,
        enabled,
    )

    response = Client().post(path, b"{}", content_type="application/json")

    assert (response.status_code, response.content) == (404, b"")


@pytest.mark.parametrize("path", ["/internal/v1/outbox/pull", "/internal/v1/outbox/ack"])
def test_an_unsigned_outbox_call_is_401(
    settings: Settings, keys: tuple[KeyPair, KeyPair], path: str
) -> None:
    settings.MARCELINE_ROLE, settings.PUBLIC_SURFACE = "prod", "internal"
    OutboxItem.objects.create(
        organisation_id=1, user_id=1, kind="add_original", payload="{}", received_at=timezone.now()
    )

    response = Client().post(path, b"{}", content_type="application/json")

    assert response.status_code == 401
    assert response.json() == {"detail": "rejected"}
    assert b"add_original" not in response.content


def test_development_refuses_a_batch_production_did_not_sign(
    settings: Settings, keys: tuple[KeyPair, KeyPair], tmp_path: Path
) -> None:
    impostor = make_keys(tmp_path, "impostor")
    settings.OUTBOX_SIGNING_KEY_PATH = str(impostor.private_path)
    OutboxItem.objects.create(
        organisation_id=1, user_id=1, kind="add_original", payload="{}", received_at=timezone.now()
    )

    with pytest.raises(Exception, match="bad_signature"):
        call_command("pull_customer_writes")
    assert not InboxItem.objects.exists()
