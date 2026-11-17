"""Receiving purges: only development's signature, only once, only while fresh."""

from datetime import timedelta
from pathlib import Path

import pytest

from delivery import purge, store
from delivery.envelope import EnvelopeError
from delivery.models import CachedBundle, CacheTombstone, PurgeNonce
from delivery.tests.helpers import NOW, KeyPair, bundle_wire, make_keys, minutes, purge_wire


@pytest.fixture
def keys(tmp_path: Path) -> KeyPair:
    return make_keys(tmp_path)


def cache(keys: KeyPair, organisation_id: int, page_ids: list[int]) -> None:
    payload = store.verify_bundle(
        bundle_wire(keys.private, organisation_id, page_ids=page_ids),
        keys.private.public_key(),
        organisation_id=organisation_id,
        now=NOW,
    )
    store.store_bundle(payload, now=NOW)


@pytest.mark.django_db
def test_a_page_purge_deletes_every_bundle_containing_the_page_and_nothing_else(
    keys: KeyPair,
) -> None:
    cache(keys, 1, [10, 11])
    cache(keys, 2, [11])
    cache(keys, 3, [12])

    result = purge.receive_purge(
        purge_wire(keys.private, page_ids=(11,)), keys.private.public_key(), now=NOW
    )

    assert (result.mode, result.bundles) == ("delete", 2)
    assert list(CachedBundle.objects.values_list("organisation_id", flat=True)) == [3]
    assert CacheTombstone.objects.get().page_id == 11


@pytest.mark.django_db
def test_an_expire_purge_keeps_the_bundle_for_stale_serving(keys: KeyPair) -> None:
    cache(keys, 1, [10])
    cache(keys, 2, [20])

    result = purge.receive_purge(
        purge_wire(keys.private, mode="expire", organisation_ids=(1,)),
        keys.private.public_key(),
        now=NOW,
    )

    assert result.bundles == 1
    assert dict(CachedBundle.objects.values_list("organisation_id", "expired")) == {
        1: True,
        2: False,
    }
    assert not CacheTombstone.objects.exists()
    # The issue time is kept, so a bundle rendered before this purge cannot undo it (`store`).
    assert CachedBundle.objects.get(organisation_id=1).expire_requested_as_of == NOW
    assert CachedBundle.objects.get(organisation_id=2).expire_requested_as_of is None


@pytest.mark.django_db
def test_a_purge_signed_by_anyone_else_changes_nothing(keys: KeyPair, tmp_path: Path) -> None:
    cache(keys, 1, [10])
    impostor = make_keys(tmp_path, "impostor")

    with pytest.raises(EnvelopeError) as refused:
        purge.receive_purge(
            purge_wire(impostor.private, organisation_ids=(1,)), keys.private.public_key(), now=NOW
        )

    assert refused.value.code == "bad_signature"
    assert CachedBundle.objects.count() == 1
    assert not PurgeNonce.objects.exists()


@pytest.mark.django_db
def test_a_replayed_purge_is_refused(keys: KeyPair) -> None:
    wire = purge_wire(keys.private, organisation_ids=(1,))
    purge.receive_purge(wire, keys.private.public_key(), now=NOW)

    with pytest.raises(EnvelopeError) as refused:
        purge.receive_purge(wire, keys.private.public_key(), now=NOW + minutes(1))

    assert refused.value.code == "replayed"


@pytest.mark.parametrize("offset", [minutes(-6), minutes(6), timedelta(days=1)])
@pytest.mark.django_db
def test_a_purge_outside_the_time_window_is_refused(keys: KeyPair, offset: timedelta) -> None:
    wire = purge_wire(keys.private, organisation_ids=(1,), issued_at=NOW + offset)

    with pytest.raises(EnvelopeError) as refused:
        purge.receive_purge(wire, keys.private.public_key(), now=NOW)

    assert refused.value.code == "outside_time_window"


@pytest.mark.django_db
def test_an_empty_or_malformed_purge_is_refused(keys: KeyPair) -> None:
    with pytest.raises(EnvelopeError) as empty:
        purge.receive_purge(purge_wire(keys.private), keys.private.public_key(), now=NOW)
    with pytest.raises(EnvelopeError) as bad_nonce:
        purge.receive_purge(
            purge_wire(keys.private, page_ids=(1,), nonce="short"),
            keys.private.public_key(),
            now=NOW,
        )

    assert empty.value.code == "empty_purge"
    assert bad_nonce.value.code == "invalid_payload"
