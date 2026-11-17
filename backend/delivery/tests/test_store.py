"""The cache rules on a fake clock: fresh for 15 minutes, stale while development fails for up to
72 hours, then nothing; one fetch per organisation however many requests arrive."""

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from django.db import connection
from pytest_django.fixtures import Settings

from delivery import client, store
from delivery.envelope import EnvelopeError
from delivery.models import CachedBundle, CachedDocument, CacheTombstone
from delivery.store import CUSTOMER_CACHE_MAX_STALE, CUSTOMER_CACHE_TTL, Unavailable
from delivery.tests.helpers import NOW, ORIGINAL, KeyPair, bundle_wire, make_keys, minutes

ORG = 41


@pytest.fixture
def keys(tmp_path: Path, settings: Settings) -> KeyPair:
    pair = make_keys(tmp_path)
    settings.CACHE_VERIFY_KEY_PATH = str(pair.public_path)
    return pair


class FakeDevelopment:
    """Development as production sees it: renders at the fake clock, or is down."""

    def __init__(self, keys: KeyPair) -> None:
        self.keys = keys
        self.calls = 0
        self.down = False
        self.render_time = NOW

    def __call__(self, organisation_id: int) -> bytes:
        self.calls += 1
        if self.down:
            raise client.DeliveryError("connection_failed")
        return bundle_wire(self.keys.private, organisation_id, as_of=self.render_time)


@pytest.fixture
def dev(keys: KeyPair) -> FakeDevelopment:
    return FakeDevelopment(keys)


def read(dev: FakeDevelopment, now: datetime, key: str = "originals") -> store.Document:
    return store.document_for(ORG, key, fallback_key="originals/*", now=now, fetch=dev)


@pytest.mark.django_db
def test_a_miss_fetches_once_and_then_serves_from_the_cache(dev: FakeDevelopment) -> None:
    first = read(dev, NOW)
    second = read(dev, NOW + minutes(14))

    assert dev.calls == 1
    assert first == second
    assert first.as_of == NOW
    assert '"org": 41' in first.body


@pytest.mark.django_db
def test_an_unknown_id_answers_the_bundles_own_404(dev: FakeDevelopment) -> None:
    found = read(dev, NOW, key="originals/orig_" + "9" * 32)

    assert (found.status, found.body) == (404, '{"detail": "Original not found"}')
    assert read(dev, NOW, key="originals/" + ORIGINAL).status == 200


@pytest.mark.django_db
def test_after_the_ttl_the_next_request_refetches(dev: FakeDevelopment) -> None:
    read(dev, NOW)
    dev.render_time = NOW + CUSTOMER_CACHE_TTL

    refreshed = read(dev, NOW + CUSTOMER_CACHE_TTL)

    assert dev.calls == 2
    assert refreshed.as_of == NOW + CUSTOMER_CACHE_TTL


@pytest.mark.django_db
def test_development_down_serves_stale_and_records_the_failure(dev: FakeDevelopment) -> None:
    read(dev, NOW)
    dev.down = True
    later = NOW + timedelta(hours=71)

    stale = read(dev, later)

    assert stale.as_of == NOW, "X-Data-As-Of keeps the render time, not the fetch time"
    bundle = CachedBundle.objects.get(organisation_id=ORG)
    assert (bundle.last_failure, bundle.last_failure_at) == ("connection_failed", later)


@pytest.mark.django_db
def test_past_72_hours_stale_data_is_never_shown(dev: FakeDevelopment) -> None:
    read(dev, NOW)
    dev.down = True

    with pytest.raises(Unavailable):
        read(dev, NOW + CUSTOMER_CACHE_MAX_STALE + timedelta(seconds=1))


@pytest.mark.django_db
def test_a_cold_cache_with_development_down_is_unavailable_not_empty(
    dev: FakeDevelopment,
) -> None:
    dev.down = True

    with pytest.raises(Unavailable):
        read(dev, NOW)
    assert not CachedBundle.objects.exists()


@pytest.mark.django_db
def test_a_bundle_that_fails_verification_is_not_stored_and_stale_is_served(
    dev: FakeDevelopment, tmp_path: Path
) -> None:
    read(dev, NOW)
    impostor = make_keys(tmp_path, "impostor")
    later = NOW + minutes(20)

    stale = store.document_for(
        ORG,
        "originals",
        now=later,
        fetch=lambda org: bundle_wire(impostor.private, org, as_of=later),
    )

    assert stale.as_of == NOW
    assert CachedBundle.objects.get(organisation_id=ORG).last_failure == "bad_signature"


@pytest.mark.django_db
def test_an_expire_flag_refetches_but_keeps_serving_stale_on_failure(
    dev: FakeDevelopment,
) -> None:
    read(dev, NOW)
    CachedBundle.objects.update(expired=True)
    dev.down = True

    assert read(dev, NOW + minutes(1)).as_of == NOW
    assert dev.calls == 2


@pytest.mark.parametrize(
    ("overrides", "organisation_id", "code"),
    [
        ({}, ORG + 1, "wrong_organisation"),
        ({"as_of": NOW + minutes(10)}, ORG, "from_the_future"),
        ({"as_of": NOW - timedelta(hours=73)}, ORG, "expired"),
        ({"expires_at": NOW + timedelta(days=30)}, ORG, "validity_too_long"),
        (
            {"documents": {"../admin": {"status": 200, "content_type": "x", "body": ""}}},
            ORG,
            "invalid_document_key",
        ),
        (
            # Customer URLs name originals by public id only (ADR 0033), never by primary key.
            {"documents": {"originals/7": {"status": 200, "content_type": "x", "body": ""}}},
            ORG,
            "invalid_document_key",
        ),
        (
            {"documents": {"originals": {"status": 500, "content_type": "x", "body": ""}}},
            ORG,
            "invalid_payload",
        ),
        ({"surprise": True}, ORG, "invalid_payload"),
    ],
)
def test_verification_refuses(
    keys: KeyPair, overrides: dict[str, object], organisation_id: int, code: str
) -> None:
    as_of = overrides.pop("as_of", NOW)
    wire = bundle_wire(keys.private, organisation_id, as_of=as_of, **overrides)  # type: ignore[arg-type]

    with pytest.raises(EnvelopeError) as refused:
        store.verify_bundle(wire, keys.private.public_key(), organisation_id=ORG, now=NOW)

    assert refused.value.code == code


@pytest.mark.django_db
def test_an_older_bundle_never_replaces_a_newer_one(keys: KeyPair) -> None:
    public = keys.private.public_key()
    newer = store.verify_bundle(
        bundle_wire(keys.private, ORG), public, organisation_id=ORG, now=NOW
    )
    store.store_bundle(newer, now=NOW)
    replay = store.verify_bundle(
        bundle_wire(keys.private, ORG, as_of=NOW - minutes(30)),
        public,
        organisation_id=ORG,
        now=NOW,
    )

    with pytest.raises(EnvelopeError) as refused:
        store.store_bundle(replay, now=NOW)

    assert refused.value.code == "as_of_went_backwards"


@pytest.mark.django_db
def test_a_bundle_rendered_before_a_safety_purge_cannot_bring_the_page_back(
    keys: KeyPair,
) -> None:
    CacheTombstone.objects.create(page_id=555, purged_as_of=NOW)
    raced = store.verify_bundle(
        bundle_wire(keys.private, ORG, as_of=NOW - minutes(1), page_ids=[555]),
        keys.private.public_key(),
        organisation_id=ORG,
        now=NOW,
    )
    clean = store.verify_bundle(
        bundle_wire(keys.private, ORG, as_of=NOW + minutes(1), page_ids=[]),
        keys.private.public_key(),
        organisation_id=ORG,
        now=NOW,
    )

    with pytest.raises(EnvelopeError) as refused:
        store.store_bundle(raced, now=NOW)
    store.store_bundle(clean, now=NOW)

    assert refused.value.code == "contains_purged_page"
    assert CachedBundle.objects.get().as_of == NOW + minutes(1)


@pytest.mark.django_db
def test_a_bundle_rendered_before_an_expire_purge_is_stored_expired(keys: KeyPair) -> None:
    """A fetch that raced an `expire` purge must not undo it.

    An `expire` purge says "what you hold is behind a decision development has published; fetch
    again". Storing a bundle rendered *before* that purge cleared the flag, so production served
    pre-decision data as **fresh** for the whole TTL — the purge arrived, was applied, and was
    then thrown away. `store_bundle` guards the `delete` case with tombstones; this is the same
    guard for `expire`.
    """
    public = keys.private.public_key()
    stored = store.verify_bundle(
        bundle_wire(keys.private, ORG, as_of=NOW), public, organisation_id=ORG, now=NOW
    )
    store.store_bundle(stored, now=NOW)
    CachedBundle.objects.filter(organisation_id=ORG).update(
        expired=True, expire_requested_as_of=NOW + minutes(2)
    )
    raced = store.verify_bundle(
        bundle_wire(keys.private, ORG, as_of=NOW + minutes(1)),
        public,
        organisation_id=ORG,
        now=NOW + minutes(3),
    )
    after = store.verify_bundle(
        bundle_wire(keys.private, ORG, as_of=NOW + minutes(4)),
        public,
        organisation_id=ORG,
        now=NOW + minutes(4),
    )

    store.store_bundle(raced, now=NOW + minutes(3))
    assert CachedBundle.objects.get().expired is True, "still behind the purge"
    assert not store.is_fresh(CachedBundle.objects.get(), NOW + minutes(3))

    store.store_bundle(after, now=NOW + minutes(4))
    assert CachedBundle.objects.get().expired is False, "rendered after the purge"
    assert store.is_fresh(CachedBundle.objects.get(), NOW + minutes(4))


@pytest.mark.django_db
def test_refreshing_replaces_every_document(dev: FakeDevelopment, keys: KeyPair) -> None:
    read(dev, NOW)
    later = NOW + minutes(16)
    store.document_for(
        ORG,
        "originals",
        now=later,
        fetch=lambda org: bundle_wire(
            keys.private,
            org,
            as_of=later,
            documents={"originals": {"status": 200, "content_type": "a/b", "body": "[]"}},
        ),
    )

    assert list(CachedDocument.objects.values_list("key", flat=True)) == ["originals"]


@pytest.mark.django_db(transaction=True)
def test_a_burst_of_requests_for_one_organisation_fetches_once(keys: KeyPair) -> None:
    calls: list[int] = []
    guard = threading.Lock()

    def slow_development(organisation_id: int) -> bytes:
        with guard:
            calls.append(organisation_id)
        time.sleep(0.4)
        return bundle_wire(keys.private, organisation_id, as_of=NOW)

    results: list[str] = []
    errors: list[BaseException] = []

    def request() -> None:
        try:
            found = store.document_for(ORG, "originals", now=NOW, fetch=slow_development)
            with guard:
                results.append(found.body)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=request) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert calls == [ORG], "one fetch for the whole burst"
    assert len(results) == 8 and len(set(results)) == 1


@pytest.mark.django_db(transaction=True)
def test_while_one_request_refreshes_a_stale_bundle_the_others_answer_stale_at_once(
    keys: KeyPair,
) -> None:
    public = keys.private.public_key()
    store.store_bundle(
        store.verify_bundle(bundle_wire(keys.private, ORG), public, organisation_id=ORG, now=NOW),
        now=NOW,
    )
    later = NOW + minutes(20)
    started = threading.Event()

    def slow_development(organisation_id: int) -> bytes:
        started.set()
        time.sleep(0.5)
        return bundle_wire(keys.private, organisation_id, as_of=later)

    def refresher() -> None:
        try:
            store.document_for(ORG, "originals", now=later, fetch=slow_development)
        finally:
            connection.close()

    thread = threading.Thread(target=refresher)
    thread.start()
    assert started.wait(5)
    fetch_again: Callable[[int], bytes] = lambda org: pytest.fail("must not fetch")  # noqa: E731
    began = time.monotonic()
    stale = store.document_for(ORG, "originals", now=later, fetch=fetch_again)
    waited = time.monotonic() - began
    thread.join()

    assert stale.as_of == NOW
    assert waited < 0.4
    assert CachedBundle.objects.get().as_of == later


@pytest.mark.django_db
def test_an_unusable_verification_key_is_unavailable_not_a_crash(
    dev: FakeDevelopment, tmp_path: Path, settings: Settings
) -> None:
    """A key file that is not a PEM key at all must end in the 503 discipline, not a 500.

    ADR 0028 allows exactly two answers on production: the cached bytes, or 503
    `data_temporarily_unavailable`. `verification_key` translated a *missing* file into an
    `EnvelopeError`, which `refresh` records as a failure — but `cryptography` raises `ValueError`
    for a file that exists and is not a key (a truncated copy, a stray text file, `/dev/null`),
    and that escaped `document_for` as an unhandled exception.
    """
    rubbish = tmp_path / "not-a-key.pub"
    rubbish.write_text("ssh-ed25519 AAAA nobody@example\n")
    settings.CACHE_VERIFY_KEY_PATH = str(rubbish)

    with pytest.raises(Unavailable):
        read(dev, NOW)
    with pytest.raises(EnvelopeError, match="verify_key_unreadable"):
        store.verification_key()
