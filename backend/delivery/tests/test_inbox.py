"""Development's side of the outbox: pull, apply once and in order, quarantine, acknowledge (ADR
0028 amendment "The production write path"). Both machines share this test database: the pull and
the acknowledgement are wired straight into production's functions, with each side's own keys."""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from delivery import inbox, outbox
from delivery.client import DeliveryError
from delivery.envelope import EnvelopeError, seal
from delivery.inbox import Outcome, PulledItem
from delivery.models import InboxItem, OutboxItem
from delivery.tests.helpers import NOW, KeyPair, make_keys, minutes

pytestmark = pytest.mark.django_db


@pytest.fixture
def dev(tmp_path: Path) -> KeyPair:
    return make_keys(tmp_path, "dev")


@pytest.fixture
def prod(tmp_path: Path) -> KeyPair:
    return make_keys(tmp_path, "prod")


def enqueue(organisation_id: int, name: str) -> str:
    item, _ = outbox.enqueue(
        kind=OutboxItem.Kind.ADD_ORIGINAL,
        organisation_id=organisation_id,
        user_id=organisation_id * 10,
        payload={"name": name},
        now=NOW,
        title=name,
    )
    return item.public_id


class Production:
    """Production's internal listener, in process: verifies with development's public key, signs
    batches with production's own private key."""

    def __init__(self, dev: KeyPair, prod: KeyPair, clock: Callable[[], datetime]) -> None:
        self.dev, self.prod, self.clock = dev, prod, clock
        self.acks = 0

    def pull(self, wire: bytes) -> bytes:
        pull = outbox.verify_pull(wire, self.dev.private.public_key(), now=self.clock())
        return outbox.signed_batch(pull, self.prod.private, now=self.clock())

    def ack(self, wire: bytes) -> None:
        self.acks += 1
        payload = outbox.verify_ack(wire, self.dev.private.public_key(), now=self.clock())
        outbox.apply_ack(payload, now=self.clock())


class Recorder:
    """A stand-in for the project's handlers: records what it applied, fails on request."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.failing: set[str] = set()
        self.refusing: set[str] = set()
        #: Marker -> the outcome code to answer with, for codes outside the ack's alphabet.
        self.odd_codes: dict[str, str] = {}

    def __call__(self, item: PulledItem) -> Outcome:
        name = item.payload
        for marker, code in self.odd_codes.items():
            if marker in name:
                self.applied.append(item.id)
                return Outcome("refused", code)
        if any(marker in name for marker in self.failing):
            InboxItem.objects.create(  # a partial write that must be rolled back
                item_id="partial-" + item.id,
                organisation_id=0,
                user_id=0,
                kind="x",
                received_at=NOW,
                status="applied",
                first_pulled_at=NOW,
            )
            raise RuntimeError("boom with the payload " + name)
        self.applied.append(item.id)
        if any(marker in name for marker in self.refusing):
            return Outcome("refused", "claim_needs_review")
        return Outcome("done", "created", original_id="orig_" + "b" * 32)


def run(
    recorder: Recorder, dev: KeyPair, prod: KeyPair, production: Production | None = None
) -> inbox.RunReport:
    production = production or Production(dev, prod, lambda: NOW)
    return inbox.run_once(
        recorder,
        signing_key=dev.private,
        verify_key=prod.private.public_key(),
        now=lambda: NOW,
        post_pull=production.pull,
        post_ack=production.ack,
    )


def statuses() -> dict[str, str]:
    return dict(OutboxItem.objects.values_list("public_id", "status"))


def test_a_run_applies_in_order_acknowledges_and_production_settles(
    dev: KeyPair, prod: KeyPair
) -> None:
    first, refused, second = enqueue(1, "a"), enqueue(1, "refuse-b"), enqueue(2, "c")
    recorder = Recorder()
    recorder.refusing.add("refuse")

    report = run(recorder, dev, prod)

    assert recorder.applied == [first, refused, second]
    assert (report.pulled, report.settled, report.acknowledged, report.more) == (3, 3, 3, False)
    assert statuses() == {first: "done", refused: "refused", second: "done"}
    assert set(InboxItem.objects.values_list("acknowledged_at", flat=True)) == {NOW}
    assert OutboxItem.objects.get(public_id=first).original_id == "orig_" + "b" * 32


def test_an_item_is_applied_once_even_when_the_acknowledgement_is_lost(
    dev: KeyPair, prod: KeyPair
) -> None:
    item = enqueue(1, "a")
    recorder = Recorder()
    production = Production(dev, prod, lambda: NOW)

    def lost(wire: bytes) -> None:
        raise DeliveryError("timeout")

    with pytest.raises(DeliveryError):
        inbox.run_once(
            recorder,
            signing_key=dev.private,
            verify_key=prod.private.public_key(),
            now=lambda: NOW,
            post_pull=production.pull,
            post_ack=lost,
        )
    assert statuses() == {item: "pending"}
    assert InboxItem.objects.get().acknowledged_at is None

    report = run(recorder, dev, prod, production)

    assert recorder.applied == [item], "pulled twice, applied once"
    assert (report.settled, report.acknowledged) == (1, 1)
    assert statuses() == {item: "done"}


def test_a_failure_holds_back_that_organisations_later_writes_only(
    dev: KeyPair, prod: KeyPair
) -> None:
    broken, later, other = enqueue(1, "fail-a"), enqueue(1, "b"), enqueue(2, "c")
    recorder = Recorder()
    recorder.failing.add("fail")

    report = run(recorder, dev, prod)

    assert recorder.applied == [other]
    assert (report.failed, report.waiting, report.settled, report.more) == (1, 1, 1, False)
    assert statuses() == {broken: "pending", later: "pending", other: "done"}
    row = InboxItem.objects.get(item_id=broken)
    assert (row.status, row.attempts, row.reason, row.payload) == ("failed", 1, "RuntimeError", "")
    assert not InboxItem.objects.filter(item_id__startswith="partial-").exists(), "rolled back"

    recorder.failing.clear()
    run(recorder, dev, prod)

    assert recorder.applied == [other, broken, later], "still in order once it works"
    assert statuses() == {broken: "done", later: "done", other: "done"}


def test_a_poison_write_is_quarantined_with_its_reason_and_retried_after_a_fix(
    dev: KeyPair, prod: KeyPair, caplog: pytest.LogCaptureFixture
) -> None:
    poison, later = enqueue(1, "fail-a"), enqueue(1, "b")
    recorder = Recorder()
    recorder.failing.add("fail")

    for _ in range(inbox.MAX_APPLY_ATTEMPTS):
        run(recorder, dev, prod)

    row = InboxItem.objects.get(item_id=poison)
    assert (row.status, row.result_code, row.reason) == (
        "quarantined",
        "processing_failed",
        "RuntimeError",
    )
    assert row.payload, "kept for a retry"
    assert statuses() == {poison: "quarantined", later: "done"}, "no longer holds anyone back"
    quarantine_lines = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(quarantine_lines) == 1 and "fail-a" not in quarantine_lines[0]

    recorder.failing.clear()
    retried = inbox.retry(poison, recorder, now=NOW)
    assert (retried.status, retried.payload, retried.acknowledged_at) == ("applied", "", None)
    run(recorder, dev, prod)
    assert statuses() == {poison: "done", later: "done"}


def test_a_batch_signed_with_developments_own_key_is_refused(dev: KeyPair, prod: KeyPair) -> None:
    enqueue(1, "a")
    impostor = Production(dev, dev, lambda: NOW)

    with pytest.raises(EnvelopeError, match="bad_signature"):
        run(Recorder(), dev, prod, impostor)
    assert not InboxItem.objects.exists()


def test_a_replayed_batch_does_not_answer_a_new_pull(dev: KeyPair, prod: KeyPair) -> None:
    enqueue(1, "a")
    production = Production(dev, prod, lambda: NOW)
    old_batch = production.pull(seal(inbox.pull_payload(nonce="n" * 20, now=NOW), dev.private))

    with pytest.raises(EnvelopeError, match="nonce_mismatch"):
        inbox.run_once(
            Recorder(),
            signing_key=dev.private,
            verify_key=prod.private.public_key(),
            now=lambda: NOW,
            post_pull=lambda wire: old_batch,
        )
    with pytest.raises(EnvelopeError, match="outside_time_window"):
        inbox.verify_batch(
            old_batch, prod.private.public_key(), nonce="n" * 20, now=NOW + minutes(10)
        )


def test_acknowledged_records_go_after_a_week_quarantined_ones_stay(
    dev: KeyPair, prod: KeyPair
) -> None:
    done, poison = enqueue(1, "a"), enqueue(2, "fail-b")
    recorder = Recorder()
    recorder.failing.add("fail")
    for _ in range(inbox.MAX_APPLY_ATTEMPTS):
        run(recorder, dev, prod)

    assert inbox.housekeeping(now=NOW + timedelta(days=6)) == 0
    assert inbox.housekeeping(now=NOW + timedelta(days=8)) == 1
    assert list(InboxItem.objects.values_list("item_id", flat=True)) == [poison]
    assert done not in InboxItem.objects.values_list("item_id", flat=True)


def test_an_outcome_code_outside_the_acknowledgements_alphabet_settles_anyway(
    dev: KeyPair, prod: KeyPair
) -> None:
    """One odd code must not stop every organisation's writes being settled, for ever.

    Production parses the whole acknowledgement with `AckPayload` and refuses it as a unit, and
    development re-sends *every* settled, unacknowledged outcome on every run. So a single result
    code outside `ResultIn`'s alphabet (a handler that answers `Claim-Needs-Review` rather than
    `claim_needs_review`) made the acknowledgement unparseable for good: nothing on production
    ever settled again, its writes stayed pending until they tripped the stale alert and paused
    the outbox, and no customer could add anything.
    """
    odd, other = enqueue(1, "odd-code"), enqueue(2, "c")
    recorder = Recorder()
    recorder.odd_codes["odd-code"] = "Claim-Needs-Review"

    report = run(recorder, dev, prod)

    assert report.acknowledged == 2
    assert statuses() == {odd: "refused", other: "done"}
    assert InboxItem.objects.get(item_id=odd).result_code == "Claim-Needs-Review", "kept here"
    assert OutboxItem.objects.get(public_id=odd).result_code == "", "not on the wire"


def test_an_original_id_the_acknowledgement_cannot_carry_settles_anyway(
    dev: KeyPair, prod: KeyPair
) -> None:
    """The same for the original's public id: what is stored must be what can be sent."""
    item = enqueue(1, "a")

    class Odd(Recorder):
        def __call__(self, pulled: PulledItem) -> Outcome:
            self.applied.append(pulled.id)
            return Outcome("done", "created", original_id="ORIG-7")

    report = run(Odd(), dev, prod)

    assert report.acknowledged == 1
    assert statuses() == {item: "done"}
    assert OutboxItem.objects.get(public_id=item).original_id == ""
