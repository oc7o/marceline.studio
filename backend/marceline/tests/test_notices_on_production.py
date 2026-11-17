"""Notice drafts on the public host (ADR 0046): reads from the signed bundle, writes through the
outbox, and nothing that sends anything.

One database plays both machines, as in `test_cache_roundtrip.py` and `test_customer_writes.py`:
`MARCELINE_ROLE` says which side a request is on, and the fetch production makes is wired straight
to development's export code. No network, no real keys.
"""

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from delivery import client as delivery_client
from delivery import store
from delivery.inbox import Outcome, PulledItem
from delivery.models import CachedBundle, OutboxItem, OutboxPullState, PurgeOutbox
from delivery.tests.helpers import KeyPair, make_keys
from index.models import Page
from marceline import export
from marceline.app import notices as notices_api
from marceline.customer_writes import HANDLERS
from marceline.notices import export as notice_export
from marceline.notices.models import NoticeDraft, NoticeEvent, NoticeEventKind, NoticeStatus
from marceline.notices.services import unknown_notice_public_id
from marceline.rights import services as rights_services
from marceline.tests.app_helpers import (
    APP,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from marceline.tests.notices_helpers import make_hosting_record, playable, rights_ready
from marceline.tests.test_app_notices import _confirmations, _create
from marceline.tests.test_app_originals import match
from matching.models import Reference

pytestmark = pytest.mark.django_db

NOTICES = f"{APP}/notices"
UNKNOWN_NOTICE = unknown_notice_public_id()


@pytest.fixture
def keys(tmp_path: Path, settings: Settings) -> KeyPair:
    pair = make_keys(tmp_path)
    settings.CACHE_SIGNING_KEY_PATH = str(pair.private_path)
    settings.CACHE_VERIFY_KEY_PATH = str(pair.public_path)
    return pair


@pytest.fixture(autouse=True)
def direct_fetch(keys: KeyPair, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Production's fetch, wired straight into development's export code (no HTTP)."""
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
def staff(db: None) -> User:
    return User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)


@pytest.fixture
def customer(member: User) -> Client:
    return client_for(member)


@pytest.fixture
def ready_original(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> tuple[Reference, Page]:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    match(
        reference,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    rights_ready(organisation, reference.sha256, member, staff)
    return reference, page


@pytest.fixture
def approved(customer: Client, ready_original: tuple[Reference, Page]) -> dict[str, Any]:
    """One approved draft, prepared on development the ordinary way."""
    reference, page = ready_original
    draft = _create(customer, public_id_of(reference), page.public_id, page.site.domain)
    response = customer.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )
    assert response.status_code == 200, response.content
    return cast(dict[str, Any], response.json())


def go_production(settings: Settings) -> None:
    """Move this process to the public host, once the draft under test already exists.

    Called in the body rather than as a fixture: the drafts the tests act on are prepared on
    development the ordinary way, and a fixture would switch the role before that happened.
    """
    settings.MARCELINE_ROLE = "prod"
    settings.OUTBOX_ENABLED = True
    OutboxPullState.objects.get_or_create(defaults={"last_pull_at": timezone.now()})


def notice_paths(approved: dict[str, Any], original: Reference) -> list[str]:
    notice_id = approved["id"]
    return [
        NOTICES,
        f"{NOTICES}/{notice_id}",
        f"{NOTICES}/{UNKNOWN_NOTICE}",
        f"{NOTICES}/candidates/{public_id_of(original)}",
        f"{NOTICES}/candidates/not-an-id",
        *(f"{NOTICES}/{notice_id}/export/{fmt}" for fmt in notice_export.FORMATS),
        f"{NOTICES}/{UNKNOWN_NOTICE}/export/txt",
        f"{NOTICES}/{notice_id}/export/pdf",
    ]


# --- reads ---------------------------------------------------------------------------------------


def test_production_answers_every_notice_read_byte_identical_to_development(
    settings: Settings,
    customer: Client,
    approved: dict[str, Any],
    ready_original: tuple[Reference, Page],
    direct_fetch: list[int],
) -> None:
    reference, _ = ready_original
    paths = notice_paths(approved, reference)
    on_dev = {path: customer.get(path) for path in paths}

    settings.MARCELINE_ROLE = "prod"
    on_prod = {path: customer.get(path) for path in paths}

    for path, dev_response in on_dev.items():
        prod = on_prod[path]
        assert prod.status_code == dev_response.status_code, path
        assert prod.content == dev_response.content, path
        assert prod["Content-Type"] == dev_response["Content-Type"], path
        assert prod.get("Content-Disposition") == dev_response.get("Content-Disposition"), path
        assert prod["Cache-Control"] == dev_response["Cache-Control"] == "private, no-store", path
    assert (
        on_prod[f"{NOTICES}/{approved['id']}/export/txt"].content.decode()
        == (approved["approved_text"])
    )
    assert len(set(direct_fetch)) == 1, "one bundle serves every notice screen"


def test_the_bundle_carries_a_download_for_every_visible_draft(
    keys: KeyPair, organisation: Organisation, approved: dict[str, Any]
) -> None:
    payload = export.bundle_payload(organisation, now=timezone.now())

    assert payload is not None
    notice_id = approved["id"]
    for fmt in notice_export.FORMATS:
        document = payload["documents"][f"notices/{notice_id}/export/{fmt}"]
        assert document["status"] == 200, fmt
    assert payload["documents"][f"notices/{notice_id}"]["status"] == 200
    assert payload["documents"]["notices/*"]["status"] == 404
    assert all(store.DOCUMENT_KEY.match(key) for key in payload["documents"])


def test_rendering_a_bundle_is_not_a_download_and_records_no_export_event(
    organisation: Organisation, approved: dict[str, Any]
) -> None:
    """A cache entry is not a customer taking their notice away, so the append-only log stays as
    it was — otherwise every refetch would invent an export the customer never made."""
    before = NoticeEvent.objects.filter(kind=NoticeEventKind.EXPORTED).count()

    export.bundle_payload(organisation, now=timezone.now())

    assert NoticeEvent.objects.filter(kind=NoticeEventKind.EXPORTED).count() == before


def test_an_unapproved_draft_answers_the_same_409_on_both_machines(
    settings: Settings, customer: Client, ready_original: tuple[Reference, Page]
) -> None:
    reference, page = ready_original
    draft = _create(customer, public_id_of(reference), page.public_id, page.site.domain)
    path = f"{NOTICES}/{draft['id']}/export/txt"
    on_dev = customer.get(path)

    settings.MARCELINE_ROLE = "prod"
    on_prod = customer.get(path)

    assert on_dev.status_code == on_prod.status_code == 409
    assert on_dev.content == on_prod.content
    assert on_prod.json() == {"detail": "not_approved"}


def test_a_download_on_production_records_the_export_through_the_outbox(
    settings: Settings, customer: Client, approved: dict[str, Any]
) -> None:
    go_production(settings)

    response = customer.get(f"{NOTICES}/{approved['id']}/export/txt")

    assert response.status_code == 200
    assert NoticeEvent.objects.filter(kind=NoticeEventKind.EXPORTED).count() == 0
    item = OutboxItem.objects.get(kind=OutboxItem.Kind.NOTICE_EXPORTED)
    assert json.loads(item.payload) == {"notice_id": approved["id"], "format": "txt"}

    development_applies(settings, item)

    event = NoticeEvent.objects.get(kind=NoticeEventKind.EXPORTED)
    assert event.detail["format"] == "txt"
    assert event.detail["sha256"] == NoticeDraft.objects.get().approved_text_sha256


def test_a_safety_flagged_page_takes_its_draft_out_of_the_bundle(
    organisation: Organisation, approved: dict[str, Any], ready_original: tuple[Reference, Page]
) -> None:
    """ADR 0011/0018 through the cache: the documents go, and the bundle names the page, so a
    `delete` purge finds and drops it on production too."""
    _, page = ready_original
    before = export.bundle_payload(organisation, now=timezone.now())
    assert before is not None and page.pk in before["page_ids"]

    page.safety_status = Page.SafetyStatus.BLOCKED
    page.save(update_fields=["safety_status"])
    after = export.bundle_payload(organisation, now=timezone.now())

    assert after is not None
    notice_id = approved["id"]
    assert f"notices/{notice_id}" not in after["documents"]
    assert f"notices/{notice_id}/export/txt" not in after["documents"]
    assert after["documents"]["notices"]["body"] == "[]"
    assert page.pk not in after["page_ids"]


# --- writes --------------------------------------------------------------------------------------


def test_production_accepts_a_draft_and_development_prepares_it(
    settings: Settings,
    customer: Client,
    organisation: Organisation,
    ready_original: tuple[Reference, Page],
) -> None:
    reference, page = ready_original
    go_production(settings)

    accepted = customer.post(
        NOTICES,
        _draft_body(public_id_of(reference), page.public_id, page.site.domain),
        content_type="application/json",
    )

    assert accepted.status_code == 202, accepted.content
    assert accepted.json()["pending"] is True
    assert accepted.json()["submission_id"].startswith("sub_")
    assert not NoticeDraft.objects.exists(), "production prepares no draft of its own"
    [submission] = customer.get(f"{NOTICES}/submissions").json()
    assert (submission["kind"], submission["status"]) == ("notice_create", "processing")

    item = OutboxItem.objects.get(kind=OutboxItem.Kind.NOTICE_CREATE)
    outcome = development_applies(settings, item)

    assert (outcome.outcome, outcome.code) == ("done", "created")
    draft = NoticeDraft.objects.get()
    assert draft.organisation == organisation
    assert outcome.original_id == draft.public_id


def test_a_double_submit_while_the_first_waits_is_one_write(
    settings: Settings, customer: Client, ready_original: tuple[Reference, Page]
) -> None:
    reference, page = ready_original
    go_production(settings)
    body = _draft_body(public_id_of(reference), page.public_id, page.site.domain)

    first = customer.post(NOTICES, body, content_type="application/json")
    second = customer.post(NOTICES, body, content_type="application/json")

    assert first.json()["submission_id"] == second.json()["submission_id"]
    assert OutboxItem.objects.filter(kind=OutboxItem.Kind.NOTICE_CREATE).count() == 1


def test_a_write_production_accepted_is_still_refused_by_the_gate_on_development(
    settings: Settings,
    customer: Client,
    approved: dict[str, Any],
    ready_original: tuple[Reference, Page],
) -> None:
    """The point of re-validating: production cannot see the safety state, the gate or the
    draft's own status, so a write it accepted is judged here — and the customer is told."""
    _, page = ready_original
    go_production(settings)
    accepted = customer.post(
        f"{NOTICES}/{approved['id']}/approve", _confirmations(), content_type="application/json"
    )
    assert accepted.status_code == 202
    page.safety_status = Page.SafetyStatus.BLOCKED
    page.save(update_fields=["safety_status"])

    item = OutboxItem.objects.get(kind=OutboxItem.Kind.NOTICE_APPROVE)
    outcome = development_applies(settings, item)

    assert (outcome.outcome, outcome.code) == ("refused", "notice_not_found")
    item.refresh_from_db()
    item.status = OutboxItem.Status.REFUSED
    item.result_code = outcome.code
    item.save(update_fields=["status", "result_code"])
    [submission] = customer.get(f"{NOTICES}/submissions").json()
    assert (submission["status"], submission["code"]) == ("refused", "notice_not_found")


def test_an_availability_only_refusal_after_acceptance_reads_finding_unavailable(
    settings: Settings, customer: Client, ready_original: tuple[Reference, Page]
) -> None:
    """`finding_no_longer_eligible` folds every blocker of every page into one exception
    (`notices/CLAUDE.md`); the submissions screen needs more than that to be worth reading. A
    refusal whose only blocker is the page's own availability is narrowed to a code the customer
    can act on: there is nothing to do (issue #86, ADR 0046 §7)."""
    reference, page = ready_original
    draft = _create(customer, public_id_of(reference), page.public_id, page.site.domain)
    go_production(settings)

    accepted = customer.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )
    assert accepted.status_code == 202, accepted.content
    page.availability = Page.Availability.REMOVED
    page.save(update_fields=["availability"])

    item = OutboxItem.objects.get(kind=OutboxItem.Kind.NOTICE_APPROVE)
    outcome = development_applies(settings, item)

    assert (outcome.outcome, outcome.code) == ("refused", "finding_unavailable")


def test_a_rights_only_refusal_after_acceptance_reads_finding_rights_blocked(
    settings: Settings,
    customer: Client,
    organisation: Organisation,
    ready_original: tuple[Reference, Page],
) -> None:
    """Same narrowing, the other family: a refusal whose only blocker is on the rights side
    points the customer at the rights page instead of the generic "reload" sentence."""
    from marceline.rights.models import RightsStatus

    reference, page = ready_original
    go_production(settings)

    accepted = customer.post(
        NOTICES,
        _draft_body(public_id_of(reference), page.public_id, page.site.domain),
        content_type="application/json",
    )
    assert accepted.status_code == 202, accepted.content
    work = rights_services.current_work(organisation, reference.sha256)
    assert work is not None
    work.status = RightsStatus.REVOKED
    work.save(update_fields=["status"])

    item = OutboxItem.objects.get(kind=OutboxItem.Kind.NOTICE_CREATE)
    outcome = development_applies(settings, item)

    assert (outcome.outcome, outcome.code) == ("refused", "finding_rights_blocked")


def test_a_mixed_refusal_keeps_the_generic_code(
    settings: Settings, customer: Client, ready_original: tuple[Reference, Page]
) -> None:
    """A refusal spanning more than one family (here: availability and no-longer-confirmed) is
    not narrowed — the generic code already says the true, if unspecific, thing: reload."""
    reference, page = ready_original
    draft = _create(customer, public_id_of(reference), page.public_id, page.site.domain)
    go_production(settings)

    accepted = customer.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )
    assert accepted.status_code == 202, accepted.content
    page.availability = Page.Availability.REMOVED
    page.save(update_fields=["availability"])
    from matching.models import ReferenceMatch

    ReferenceMatch.objects.filter(reference=reference, page=page).update(
        status=ReferenceMatch.Status.PROPOSED
    )

    item = OutboxItem.objects.get(kind=OutboxItem.Kind.NOTICE_APPROVE)
    outcome = development_applies(settings, item)

    assert (outcome.outcome, outcome.code) == ("refused", "finding_no_longer_eligible")


def test_the_whole_flow_is_accepted_and_applied_in_order(
    settings: Settings, customer: Client, approved: dict[str, Any]
) -> None:
    go_production(settings)
    notice_id = approved["id"]
    steps: tuple[tuple[str, dict[str, Any], str], ...] = (
        (f"{NOTICES}/{notice_id}/mark-sent", {}, OutboxItem.Kind.NOTICE_MARK_SENT),
        (
            f"{NOTICES}/{notice_id}/response",
            {"outcome": "removed"},
            OutboxItem.Kind.NOTICE_RESPONSE,
        ),
    )
    for path, body, kind in steps:
        accepted = customer.post(path, body, content_type="application/json")
        assert accepted.status_code == 202, (path, accepted.content)
        item = OutboxItem.objects.get(kind=kind)
        outcome = development_applies(settings, item)
        assert outcome.outcome == "done", (path, outcome.code)

    draft = NoticeDraft.objects.get()
    assert draft.status == NoticeStatus.PROVIDER_RESPONDED
    assert draft.outcome == "removed"


def test_a_notice_write_asks_production_to_refetch(
    settings: Settings,
    customer: Client,
    organisation: Organisation,
    approved: dict[str, Any],
) -> None:
    """The withdrawal has to reach the screen: development records an `expire` purge for the
    organisation, exactly as a rights write does."""
    go_production(settings)
    customer.get(NOTICES)
    assert CachedBundle.objects.filter(organisation_id=organisation.pk).exists()

    customer.post(
        f"{NOTICES}/{approved['id']}/withdraw",
        {"reason": "mistaken_match"},
        content_type="application/json",
    )
    item = OutboxItem.objects.get(kind=OutboxItem.Kind.NOTICE_WITHDRAW)
    development_applies(settings, item)

    assert NoticeDraft.objects.get().status == NoticeStatus.WITHDRAWN
    assert PurgeOutbox.objects.filter(
        reason="customer_write", mode=PurgeOutbox.Mode.EXPIRE
    ).exists()


def test_the_sandbox_send_does_not_exist_on_production(
    settings: Settings, customer: Client, approved: dict[str, Any]
) -> None:
    go_production(settings)

    response = customer.post(
        f"{NOTICES}/sandbox-send/{approved['id']}",
        {"recipient_email": "sandbox@marceline.test"},
        content_type="application/json",
    )

    assert response.status_code == 404
    assert mail.outbox == []


def test_no_new_path_on_production_sends_any_mail(
    settings: Settings,
    customer: Client,
    approved: dict[str, Any],
    ready_original: tuple[Reference, Page],
) -> None:
    """ADR 0035's rule, asserted over everything this change added: the customer sends the notice
    from their own account, and nothing here hands a draft to a mail transport."""
    reference, page = ready_original
    go_production(settings)
    notice_id = approved["id"]
    for path in notice_paths(approved, reference):
        customer.get(path)
    writes: tuple[tuple[str, Any], ...] = (
        (NOTICES, _draft_body(public_id_of(reference), page.public_id, page.site.domain)),
        (f"{NOTICES}/{notice_id}/approve", _confirmations()),
        (f"{NOTICES}/{notice_id}/mark-sent", {}),
        (f"{NOTICES}/{notice_id}/response", {"outcome": "removed"}),
        (f"{NOTICES}/{notice_id}/withdraw", {"reason": "mistaken_match"}),
        (f"{NOTICES}/{notice_id}/correct", {"reason": "mistaken_match"}),
    )
    for path, body in writes:
        customer.post(path, body, content_type="application/json")
    for item in OutboxItem.objects.all():
        development_applies(settings, item)

    assert mail.outbox == []


def test_writes_are_refused_while_development_is_not_pulling(
    settings: Settings, customer: Client, approved: dict[str, Any]
) -> None:
    """A customer should hear it at once rather than watch a notice wait for days (ADR 0028)."""
    go_production(settings)
    OutboxPullState.objects.update(
        last_pull_at=timezone.now() - timedelta(hours=settings.OUTBOX_PAUSE_AFTER_HOURS + 1)
    )

    response = customer.post(
        f"{NOTICES}/{approved['id']}/mark-sent", {}, content_type="application/json"
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "processing_paused"}
    assert not OutboxItem.objects.exists()


def test_writes_are_refused_while_the_outbox_is_off(
    settings: Settings, customer: Client, approved: dict[str, Any]
) -> None:
    go_production(settings)
    settings.OUTBOX_ENABLED = False

    response = customer.post(
        f"{NOTICES}/{approved['id']}/mark-sent", {}, content_type="application/json"
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "notice_write_unavailable"}


def test_every_notice_write_kind_has_a_handler() -> None:
    """A kind production can accept that development cannot apply would quarantine every time."""
    for kind in (*notices_api.SUBMITTED_KINDS, OutboxItem.Kind.NOTICE_EXPORTED):
        assert kind in HANDLERS, kind


def _draft_body(original_id: str, page_id: str, hostname: str) -> dict[str, Any]:
    return {
        "original_id": original_id,
        "page_ids": [page_id],
        "recipient": {
            "hostname": hostname,
            "provider_name": "Example Hosting",
            "channel_kind": "abuse_email",
            "email": "abuse@hosting.example",
        },
        "variant": "dsa_art16",
        "language": "en",
        "claimant": {"name": "Alpha Studio", "email": "rights@alpha.example"},
    }


def development_applies(settings: Settings, item: OutboxItem) -> Outcome:
    """Development pulls one accepted write and applies it, as `pull_customer_writes` does.

    The role really moves back for it: `marceline.cache_purge` only records an `expire` purge on
    development, so applying a write with the role left on `prod` would silently skip the very
    refetch the customer is waiting for.
    """
    role = settings.MARCELINE_ROLE
    settings.MARCELINE_ROLE = "dev"
    settings.CACHE_PURGE_OUTBOX = True
    try:
        return HANDLERS[item.kind](_pulled(item))
    finally:
        settings.MARCELINE_ROLE = role


def _pulled(item: OutboxItem) -> PulledItem:
    """One accepted write, as development's pull hands it to a handler."""
    return PulledItem(
        id=item.public_id,
        organisation_id=item.organisation_id,
        user_id=item.user_id,
        kind=item.kind,
        payload=item.payload,
        received_at=item.received_at,
    )
