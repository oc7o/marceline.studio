"""Purges: development sends them after a safety flag or a decision; production accepts only signed
ones, only on its Tailscale listener (ADR 0028)."""

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from delivery import client as delivery_client
from delivery import purge, store
from delivery.envelope import FORMAT_PURGE, open_envelope
from delivery.models import CachedBundle, PurgeOutbox
from delivery.tests.helpers import KeyPair, bundle_wire, make_keys, purge_wire
from index.models import Page
from index.services import set_safety_status
from marceline import cache_purge
from marceline.review.references import apply_pair_decision
from marceline.tests.app_helpers import add_member, make_organisation, make_reference
from matching.models import Reference, ReferenceLeak

PURGE = "/internal/v1/cache/purge"


@pytest.fixture
def keys(tmp_path: Path, settings: Settings) -> KeyPair:
    pair = make_keys(tmp_path)
    settings.CACHE_SIGNING_KEY_PATH = str(pair.private_path)
    settings.CACHE_VERIFY_KEY_PATH = str(pair.public_path)
    return pair


class Production:
    """Production's purge listener as development's HTTP client meets it."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == PURGE
        self.received.append(request.content)
        return httpx.Response(self.status, json={"mode": "delete", "bundles": 0})


@pytest.fixture
def production(
    settings: Settings, keys: KeyPair, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Production]:
    prod = Production()
    settings.CACHE_PURGE_BASE_URL = "http://marceline-prod.test:8081"
    monkeypatch.setattr(delivery_client, "transport", httpx.MockTransport(prod))
    monkeypatch.setattr(cache_purge, "sleep", lambda seconds: None)
    yield prod


def opened(keys: KeyPair, wire: bytes) -> dict[str, object]:
    return open_envelope(wire, keys.private.public_key(), expected_format=FORMAT_PURGE)


@pytest.fixture
def owned(db: None) -> tuple[Organisation, User, Reference]:
    organisation = make_organisation("Alpha Studio")
    owner = add_member(organisation, "alpha-owner")
    return organisation, owner, make_reference(owner)


# --- development: when purges are sent ---------------------------------------------------------


def test_a_safety_block_purges_the_page_after_commit(
    production: Production,
    keys: KeyPair,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    page = make_page("soon-blocked")

    with django_capture_on_commit_callbacks(execute=True) as callbacks:  # type: ignore[attr-defined]
        set_safety_status(page, Page.SafetyStatus.BLOCKED, note="test")
        assert production.received == [], "nothing leaves before the block commits"

    assert len(callbacks) == 1
    [wire] = production.received
    payload = opened(keys, wire)
    assert (payload["mode"], payload["page_ids"], payload["organisation_ids"]) == (
        "delete",
        [page.pk],
        [],
    )
    row = PurgeOutbox.objects.get()
    assert row.sent_at is not None and row.reason == "safety_flag"


def test_saving_an_already_flagged_page_again_sends_nothing(
    production: Production,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    page = make_page("flagged")
    Page.objects.filter(pk=page.pk).update(safety_status=Page.SafetyStatus.REVIEW)
    page.refresh_from_db()

    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        set_safety_status(page, Page.SafetyStatus.BLOCKED, note="test")
        page.title = "renamed"
        page.save()

    assert production.received == []
    assert not PurgeOutbox.objects.exists()


def test_nothing_is_recorded_without_a_production_to_purge(
    settings: Settings, keys: KeyPair, make_page: Callable[[str], Page]
) -> None:
    settings.CACHE_PURGE_BASE_URL = ""
    set_safety_status(make_page("blocked"), Page.SafetyStatus.BLOCKED, note="test")

    assert not PurgeOutbox.objects.exists()


def test_a_process_without_the_key_records_and_another_one_delivers(
    settings: Settings,
    production: Production,
    keys: KeyPair,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    # The development web process: outbox on, no URL, no key.
    settings.CACHE_PURGE_BASE_URL = ""
    settings.CACHE_SIGNING_KEY_PATH = ""
    settings.CACHE_PURGE_OUTBOX = True
    page = make_page("blocked")

    with django_capture_on_commit_callbacks(execute=True) as callbacks:  # type: ignore[attr-defined]
        set_safety_status(page, Page.SafetyStatus.BLOCKED, note="test")

    assert callbacks == [] and production.received == []
    assert PurgeOutbox.objects.get().sent_at is None

    # The export container: key and URL, run from cron.
    settings.CACHE_PURGE_BASE_URL = "http://marceline-prod.test:8081"
    settings.CACHE_SIGNING_KEY_PATH = str(keys.private_path)
    assert cache_purge.deliver_pending() == (1, 0)
    assert opened(keys, production.received[0])["page_ids"] == [page.pk]


def test_a_decision_expires_the_owning_organisation_only(
    production: Production,
    keys: KeyPair,
    owned: tuple[Organisation, User, Reference],
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    organisation, _, reference = owned
    add_member(make_organisation("Bravo Creator"), "bravo-owner")
    page = make_page("leak")
    ReferenceLeak.objects.create(reference=reference, page=page)

    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        apply_pair_decision({(reference.pk, page.pk)}, "confirm")

    [wire] = production.received
    payload = opened(keys, wire)
    assert (payload["mode"], payload["organisation_ids"]) == ("expire", [organisation.pk])


def test_a_decision_on_an_unowned_original_sends_nothing(
    production: Production,
    reference: Reference,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    page = make_page("leak")
    ReferenceLeak.objects.create(reference=reference, page=page)

    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        apply_pair_decision({(reference.pk, page.pk)}, "confirm")

    assert production.received == []


def test_an_undeliverable_purge_retries_then_stays_pending_with_a_code(
    production: Production,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    production.status = 503

    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        set_safety_status(make_page("blocked"), Page.SafetyStatus.BLOCKED, note="test")

    row = PurgeOutbox.objects.get()
    assert (row.sent_at, row.attempts, row.last_error) == (None, 3, "status_503")
    assert len(production.received) == 3
    assert "marceline-prod" not in caplog.text and "http" not in caplog.text
    assert "stays pending" in caplog.text

    production.status = 200
    assert cache_purge.deliver_pending() == (1, 0)
    assert PurgeOutbox.objects.get().sent_at is not None


def test_the_sweep_catches_flags_written_by_bulk_updates(
    production: Production,
    keys: KeyPair,
    owned: tuple[Organisation, User, Reference],
    make_page: Callable[[str], Page],
) -> None:
    _, _, reference = owned
    linked, unlinked = make_page("linked"), make_page("unlinked")
    ReferenceLeak.objects.create(reference=reference, page=linked, status="confirmed")
    now = timezone.now()
    Page.objects.filter(pk__in=[linked.pk, unlinked.pk]).update(
        safety_status=Page.SafetyStatus.BLOCKED, safety_flagged_at=now
    )

    rows = cache_purge.sweep_flagged_pages(within=timedelta(minutes=10), now=now)

    assert [row.page_ids for row in rows] == [[linked.pk]]
    assert cache_purge.deliver_pending() == (1, 0)
    assert opened(keys, production.received[0])["page_ids"] == [linked.pk]


def test_the_sweep_catches_a_page_the_media_gate_dropped_while_it_stayed_clear(
    production: Production,
    keys: KeyPair,
    owned: tuple[Organisation, User, Reference],
    make_page: Callable[[str], Page],
) -> None:
    """A rescore below `SAFETY_GATE_MIN_SCORE` hides a page from every customer view (ADR 0018)
    without touching `safety_status`, and `safety_scan`/`rescore_page` write it with a bulk
    update, so no model signal sees it either. Production must still be told to forget it."""
    _, _, reference = owned
    gated, still_safe = make_page("gated-by-score"), make_page("still-safe")
    ReferenceLeak.objects.create(reference=reference, page=gated, status="confirmed")
    ReferenceLeak.objects.create(reference=reference, page=still_safe, status="confirmed")
    now = timezone.now()
    Page.objects.filter(pk=gated.pk).update(safety_score=0.2, safety_scored_at=now)
    Page.objects.filter(pk=still_safe.pk).update(safety_score=0.95, safety_scored_at=now)

    rows = cache_purge.sweep_flagged_pages(within=timedelta(minutes=10), now=now)

    assert [row.page_ids for row in rows] == [[gated.pk]]
    assert cache_purge.deliver_pending() == (1, 0)
    assert opened(keys, production.received[0])["page_ids"] == [gated.pk]


def test_a_score_drop_saved_through_the_orm_purges_the_page(
    production: Production,
    keys: KeyPair,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    """A re-crawl (`store_parse_result`) saves the page through the ORM with the same clear status
    and a lower score; the page leaves the customer's view, so the purge must fire."""
    page = make_page("rescored-down")

    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        page.safety_score = 0.2
        page.save(update_fields=["safety_score"])

    [wire] = production.received
    payload = opened(keys, wire)
    assert (payload["mode"], payload["page_ids"]) == ("delete", [page.pk])
    assert PurgeOutbox.objects.get().reason == "safety_flag"


def test_saving_a_page_that_is_still_below_the_gate_sends_nothing(
    production: Production,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    """Only the transition purges: a page already hidden by its score is not purged again."""
    page = make_page("already-gated")
    Page.objects.filter(pk=page.pk).update(safety_score=0.2)
    page.refresh_from_db()

    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        page.safety_score = 0.1
        page.save(update_fields=["safety_score"])

    assert production.received == []
    assert not PurgeOutbox.objects.exists()


# --- production: who may purge -----------------------------------------------------------------


@pytest.fixture
def prod_internal(settings: Settings, keys: KeyPair) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.PUBLIC_SURFACE = "internal"


def cached(keys: KeyPair, organisation_id: int, page_ids: list[int]) -> None:
    now = timezone.now()
    payload = store.verify_bundle(
        bundle_wire(keys.private, organisation_id, as_of=now, page_ids=page_ids),
        keys.private.public_key(),
        organisation_id=organisation_id,
        now=now,
    )
    store.store_bundle(payload, now=now)


@pytest.mark.django_db
@pytest.mark.usefixtures("prod_internal")
def test_a_signed_purge_is_applied_on_the_internal_listener(keys: KeyPair) -> None:
    cached(keys, 1, [10])

    response = Client().post(
        PURGE,
        purge_wire(keys.private, page_ids=(10,), issued_at=timezone.now()),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "delete", "bundles": 1}
    assert not CachedBundle.objects.exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("prod_internal")
def test_an_unsigned_forged_or_replayed_purge_is_401_and_changes_nothing(
    keys: KeyPair, tmp_path: Path
) -> None:
    cached(keys, 1, [10])
    impostor = make_keys(tmp_path, "impostor")
    now = timezone.now()
    genuine = purge_wire(keys.private, page_ids=(99,), issued_at=now)

    answers = [
        Client().post(PURGE, b"{}", content_type="application/json"),
        Client().post(
            PURGE,
            purge_wire(impostor.private, organisation_ids=(1,), issued_at=now),
            content_type="application/json",
        ),
        Client().post(PURGE, genuine, content_type="application/json"),
        Client().post(PURGE, genuine, content_type="application/json"),
    ]

    assert [answer.status_code for answer in answers] == [401, 401, 200, 401]
    assert answers[0].json() == answers[1].json() == answers[3].json() == {"detail": "rejected"}
    assert CachedBundle.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "surface"), [("prod", "customer"), ("dev", "full"), ("dev", "export")]
)
def test_the_purge_endpoint_is_404_off_the_internal_listener(
    settings: Settings, keys: KeyPair, role: str, surface: str
) -> None:
    settings.MARCELINE_ROLE = role
    settings.PUBLIC_SURFACE = surface

    response = Client().post(
        PURGE,
        purge_wire(keys.private, page_ids=(1,), issued_at=timezone.now()),
        content_type="application/json",
    )

    assert response.status_code == 404
    assert response.content == b""


@pytest.mark.django_db
@pytest.mark.usefixtures("prod_internal")
def test_the_internal_listener_answers_nothing_but_the_purge() -> None:
    for path in ("/api/v1/health", "/api/v1/app/me", "/admin/", "/internal/v1/export/accounts"):
        assert Client().get(path).status_code == 404, path


def test_purge_payloads_carry_a_fresh_nonce_each_time(db: None) -> None:
    row = PurgeOutbox(mode="delete", page_ids=[1], organisation_ids=[], reason="test")
    now = timezone.now()

    first, second = cache_purge.purge_payload(row, now=now), cache_purge.purge_payload(row, now=now)

    assert first["nonce"] != second["nonce"]
    assert json.dumps(first, default=str)


def test_a_sweep_of_more_pages_than_one_purge_may_carry_is_split(
    production: Production, keys: KeyPair, db: None
) -> None:
    """A purge is refused whole when it names more ids than `purge.MAX_IDS`.

    `flagged_pages_since` is unbounded — a `safety_scan` that rescores a large site can hide
    thousands of linked pages at once — while production's `PurgePayload` caps both id lists at
    10,000. One oversized purge was recorded, refused on arrival as `invalid_payload` (a 401, so
    `deliver` reads it as `unauthorised`) and stayed pending for ever; and since the cron sweep
    queues another one every few minutes, `deliver_pending`'s oldest-first window would fill with
    them until no later safety purge was delivered at all.
    """
    rows = cache_purge.request_purge(
        PurgeOutbox.Mode.DELETE, reason="safety_sweep", page_ids=range(1, purge.MAX_IDS + 6)
    )

    assert [len(row.page_ids) for row in rows] == [purge.MAX_IDS, 5]
    assert sorted(page for row in rows for page in row.page_ids) == list(
        range(1, purge.MAX_IDS + 6)
    )
    assert cache_purge.deliver_pending() == (2, 0)
    for wire in production.received:
        purge.verify_purge(wire, keys.private.public_key(), now=timezone.now())
