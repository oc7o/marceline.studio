"""Production's outbox: accepting, handing out signed batches, settling, housekeeping (ADR 0028
amendment "The production write path"). Throwaway keys, fake clock, no network."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_django.fixtures import Settings

from delivery import outbox, store
from delivery.envelope import (
    FORMAT_OUTBOX_ACK,
    FORMAT_OUTBOX_BATCH,
    FORMAT_OUTBOX_PULL,
    SCHEMA_VERSION,
    EnvelopeError,
    open_envelope,
    seal,
)
from delivery.models import CachedBundle, OutboxItem, OutboxPullState
from delivery.tests.helpers import NOW, KeyPair, bundle_wire, make_keys, minutes

pytestmark = pytest.mark.django_db


@pytest.fixture
def dev(tmp_path: Path) -> KeyPair:
    return make_keys(tmp_path, "dev")


@pytest.fixture
def prod(tmp_path: Path) -> KeyPair:
    return make_keys(tmp_path, "prod")


def enqueue(organisation_id: int = 1, *, size: int = 10, key: str = "", at: datetime = NOW) -> str:
    item, _ = outbox.enqueue(
        kind=OutboxItem.Kind.ADD_ORIGINAL,
        organisation_id=organisation_id,
        user_id=organisation_id * 10,
        payload={"fingerprint": "x" * size},
        now=at,
        title="Beach day",
        dedupe_key=key,
    )
    return item.public_id


def pull_wire(key: KeyPair, *, nonce: str = "pull-nonce-0123456789", at: datetime = NOW) -> bytes:
    payload = {
        "format": FORMAT_OUTBOX_PULL,
        "schema_version": SCHEMA_VERSION,
        "issued_at": at,
        "nonce": nonce,
    }
    return seal(payload, key.private)


def ack_wire(
    key: KeyPair,
    results: list[dict[str, Any]],
    *,
    nonce: str = "ack-nonce-0123456789",
    at: datetime = NOW,
) -> bytes:
    payload = {
        "format": FORMAT_OUTBOX_ACK,
        "schema_version": SCHEMA_VERSION,
        "issued_at": at,
        "nonce": nonce,
        "results": results,
    }
    return seal(payload, key.private)


def result(item_id: str, outcome: str = "done", **overrides: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "organisation_id": 1,
        "outcome": outcome,
        "code": "created" if outcome == "done" else "claim_needs_review",
        "original_id": "orig_" + "a" * 32 if outcome == "done" else "",
        "applied_at": NOW,
        **overrides,
    }


# --- accepting ----------------------------------------------------------------------------------


def test_a_double_submit_while_pending_is_one_item() -> None:
    first = enqueue(key="ab" * 32)
    again = enqueue(key="ab" * 32)
    other_organisation = enqueue(2, key="ab" * 32)

    assert first == again != other_organisation
    item = OutboxItem.objects.get(public_id=first)
    assert (item.status, item.title) == ("pending", "Beach day")
    assert json.loads(item.payload) == {"fingerprint": "x" * 10}
    assert item.payload_bytes == len(item.payload)
    assert first.startswith("sub_")


def test_writes_are_refused_until_development_pulls_and_again_when_it_stops(
    settings: Settings,
) -> None:
    settings.OUTBOX_PAUSE_AFTER_HOURS = 24

    assert outbox.paused(NOW), "never pulled"
    OutboxPullState.objects.create(last_pull_at=NOW - timedelta(hours=23))
    assert not outbox.paused(NOW)
    assert outbox.paused(NOW + timedelta(hours=2))


def test_only_production_with_the_flag_takes_writes(settings: Settings) -> None:
    settings.OUTBOX_ENABLED = True
    assert not outbox.writes_enabled(), "development applies writes itself"
    settings.MARCELINE_ROLE = "prod"
    assert outbox.writes_enabled()
    settings.OUTBOX_ENABLED = False
    assert not outbox.writes_enabled()


# --- the pull -----------------------------------------------------------------------------------


def test_a_batch_carries_pending_items_in_order_signed_by_production(
    dev: KeyPair, prod: KeyPair
) -> None:
    ids = [enqueue(1), enqueue(2), enqueue(1)]

    pull = outbox.verify_pull(pull_wire(dev), dev.private.public_key(), now=NOW)
    wire = outbox.signed_batch(pull, prod.private, now=NOW)
    batch = open_envelope(wire, prod.private.public_key(), expected_format=FORMAT_OUTBOX_BATCH)

    assert [item["id"] for item in batch["items"]] == ids
    assert batch["request_nonce"] == pull.nonce
    assert batch["more"] is False
    assert json.loads(batch["items"][0]["payload"]) == {"fingerprint": "x" * 10}
    assert OutboxPullState.objects.get().last_pull_at == NOW
    assert set(OutboxItem.objects.values_list("status", "pulls")) == {("pending", 1)}
    with pytest.raises(EnvelopeError, match="bad_signature"):
        open_envelope(wire, dev.private.public_key(), expected_format=FORMAT_OUTBOX_BATCH)


def test_a_batch_stops_at_its_byte_budget_but_always_carries_one_item(
    dev: KeyPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox, "BATCH_BYTES", 100)
    first = enqueue(size=500)
    enqueue(size=10)

    pull = outbox.verify_pull(pull_wire(dev), dev.private.public_key(), now=NOW)
    batch = outbox.batch_payload(pull, now=NOW)

    assert [item["id"] for item in batch["items"]] == [first]
    assert batch["more"] is True


@pytest.mark.parametrize(
    ("make_wire", "code"),
    [
        (lambda dev, prod: pull_wire(prod), "bad_signature"),
        (lambda dev, prod: pull_wire(dev, at=NOW - minutes(6)), "outside_time_window"),
        (lambda dev, prod: ack_wire(dev, []), "wrong_format"),
        (lambda dev, prod: b"{}", "malformed"),
    ],
)
def test_a_pull_needs_developments_signature_fresh_and_of_its_own_format(
    dev: KeyPair, prod: KeyPair, make_wire: Any, code: str
) -> None:
    enqueue()
    with pytest.raises(EnvelopeError, match=code):
        outbox.verify_pull(make_wire(dev, prod), dev.private.public_key(), now=NOW)
    assert not OutboxPullState.objects.exists()


def test_a_replayed_pull_is_refused(dev: KeyPair) -> None:
    wire = pull_wire(dev)
    outbox.verify_pull(wire, dev.private.public_key(), now=NOW)

    with pytest.raises(EnvelopeError, match="replayed"):
        outbox.verify_pull(wire, dev.private.public_key(), now=NOW + minutes(1))


# --- settling -----------------------------------------------------------------------------------


def test_an_acknowledgement_settles_empties_and_expires_the_bundle(
    dev: KeyPair, prod: KeyPair
) -> None:
    added, refused, other = enqueue(), enqueue(), enqueue(2)
    bundle = store.verify_bundle(
        bundle_wire(dev.private, 1, as_of=NOW - minutes(1)),
        dev.private.public_key(),
        organisation_id=1,
        now=NOW,
    )
    store.store_bundle(bundle, now=NOW)

    payload = outbox.verify_ack(
        ack_wire(dev, [result(added), result(refused, "refused")]),
        dev.private.public_key(),
        now=NOW,
    )
    applied = outbox.apply_ack(payload, now=NOW)

    assert (applied.settled, applied.ignored) == (2, 0)
    rows = {row.public_id: row for row in OutboxItem.objects.all()}
    assert (rows[added].status, rows[added].result_code, rows[added].payload) == (
        "done",
        "created",
        "",
    )
    assert rows[added].original_id == "orig_" + "a" * 32
    assert (rows[refused].status, rows[refused].result_code) == ("refused", "claim_needs_review")
    assert rows[other].status == "pending" and rows[other].payload
    assert CachedBundle.objects.get(organisation_id=1).expired is True


def test_settled_is_final_but_a_quarantined_write_may_still_be_added(dev: KeyPair) -> None:
    done, poison = enqueue(), enqueue()
    outbox.apply_ack(
        outbox.verify_ack(
            ack_wire(
                dev,
                [result(done), result(poison, "quarantined", code="processing_failed")],
            ),
            dev.private.public_key(),
            now=NOW,
        ),
        now=NOW,
    )

    later = outbox.apply_ack(
        outbox.verify_ack(
            ack_wire(
                dev,
                [result(done, "refused"), result(poison)],
                nonce="ack-nonce-second-0123",
            ),
            dev.private.public_key(),
            now=NOW,
        ),
        now=NOW,
    )

    assert (later.settled, later.ignored) == (1, 1)
    assert dict(OutboxItem.objects.values_list("public_id", "status")) == {
        done: "done",
        poison: "done",
    }


def test_an_acknowledgement_for_another_organisation_settles_nothing(dev: KeyPair) -> None:
    item = enqueue(1)

    applied = outbox.apply_ack(
        outbox.verify_ack(
            ack_wire(dev, [result(item, organisation_id=2)]), dev.private.public_key(), now=NOW
        ),
        now=NOW,
    )

    assert applied.settled == 0
    assert OutboxItem.objects.get().status == "pending"


def test_an_acknowledgement_production_signed_itself_is_refused(
    dev: KeyPair, prod: KeyPair
) -> None:
    item = enqueue()

    with pytest.raises(EnvelopeError, match="bad_signature"):
        outbox.verify_ack(ack_wire(prod, [result(item)]), dev.private.public_key(), now=NOW)


def test_a_bundle_rendered_before_an_applied_write_is_served_but_refetched(
    dev: KeyPair,
) -> None:
    item = enqueue()
    outbox.apply_ack(
        outbox.verify_ack(
            ack_wire(dev, [result(item, applied_at=NOW)]), dev.private.public_key(), now=NOW
        ),
        now=NOW,
    )

    raced = store.verify_bundle(
        bundle_wire(dev.private, 1, as_of=NOW - minutes(1)),
        dev.private.public_key(),
        organisation_id=1,
        now=NOW,
    )
    assert store.store_bundle(raced, now=NOW).expired is True
    fresh = store.verify_bundle(
        bundle_wire(dev.private, 1, as_of=NOW + minutes(1)),
        dev.private.public_key(),
        organisation_id=1,
        now=NOW + minutes(1),
    )
    assert store.store_bundle(fresh, now=NOW + minutes(1)).expired is False


# --- housekeeping -------------------------------------------------------------------------------


def test_stale_writes_are_reported_once_and_settled_ones_deleted_after_a_week(
    dev: KeyPair, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.OUTBOX_ENABLED = True
    settings.OUTBOX_STALE_ALERT_HOURS = 2
    OutboxPullState.objects.create(last_pull_at=NOW)
    old = enqueue(at=NOW - timedelta(hours=3))
    enqueue(at=NOW - timedelta(hours=1))
    settled = enqueue(at=NOW - timedelta(days=9))
    outbox.apply_ack(
        outbox.verify_ack(
            ack_wire(dev, [result(settled)], at=NOW - timedelta(days=8)),
            dev.private.public_key(),
            now=NOW - timedelta(days=8),
        ),
        now=NOW - timedelta(days=8),
    )

    with caplog.at_level(logging.ERROR, logger="marceline.outbox"):
        first = outbox.housekeeping(now=NOW)
        second = outbox.housekeeping(now=NOW + minutes(15))

    assert (first.stale_reported, first.deleted, first.paused) == (1, 1, False)
    assert (second.stale_reported, second.deleted) == (0, 0)
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1 and "pending longer than" in errors[0].getMessage()
    assert "Beach day" not in errors[0].getMessage()
    assert OutboxItem.objects.get(public_id=old).alerted_at == NOW
    assert not OutboxItem.objects.filter(public_id=settled).exists()


def test_a_paused_outbox_is_reported(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.OUTBOX_ENABLED = True
    OutboxPullState.objects.create(last_pull_at=NOW - timedelta(hours=30))

    with caplog.at_level(logging.ERROR, logger="marceline.outbox"):
        assert outbox.housekeeping(now=NOW).paused is True

    assert any("new writes are refused" in record.getMessage() for record in caplog.records)
