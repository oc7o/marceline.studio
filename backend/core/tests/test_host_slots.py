"""Database-backed host slots (ADR 0030 § 2): one request per host per delay across threads,
processes and lanes; refusals take no slot; penalties and leases are host-wide."""

import itertools
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from core.host_slots import DbHostGate, SlotWait
from core.http import (
    BudgetExhaustedError,
    HostBusyError,
    PoliteClient,
    RequestAttempt,
    RequestDeadlineError,
    RequestRefusedError,
    host_key,
)
from core.models import HostSlot

UA = "MarcelineBot/0.1"


class Starts:
    """Records when each request reached the transport, and the most requests in flight at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.at: list[tuple[str, float]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def handler(self, hold_s: float = 0.0) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            with self.lock:
                self.at.append((request.url.path, time.monotonic()))
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                if hold_s:
                    time.sleep(hold_s)
                return httpx.Response(200, text="ok")
            finally:
                with self.lock:
                    self.in_flight -= 1

        return handle

    def gaps(self) -> list[float]:
        times = sorted(moment for _, moment in self.at)
        return [later - earlier for earlier, later in itertools.pairwise(times)]


class NoBudget:
    """An observer whose budget is used up."""

    def admit(self, attempt: RequestAttempt) -> None:
        raise BudgetExhaustedError(
            "budget used up", scope="origin:page", host=attempt.host, resume_at=datetime.now(UTC)
        )

    def record(self, attempt: RequestAttempt, outcome: str, size: int) -> None:
        return None

    def flush(self) -> None:
        return None


def client_for(
    gate: DbHostGate, handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
) -> PoliteClient:
    return PoliteClient(UA, transport=httpx.MockTransport(handler), gate=gate, **kwargs)  # type: ignore[arg-type]


def slot(key: str) -> HostSlot:
    return HostSlot.objects.get(pk=key)


@pytest.mark.parametrize(
    "value,key",
    [
        ("https://www.site22.example/videos/1", "site22.example"),
        ("site22.example", "site22.example"),
        ("WWW.site22.example:8443", "site22.example"),
        ("http://cdn2.media-cdn.example:8080/a.jpg", "cdn2.media-cdn.example"),
        ("https://www.com/", "www.com"),
        ("https://bücher.example/x", "bücher.example"),
        ("https://xn--bcher-kva.example/x", "xn--bcher-kva.example"),
        ("https://[2001:db8::1]:443/x", "2001:db8::1"),
        ("site11.example.", "site11.example"),
    ],
)
def test_host_key(value: str, key: str) -> None:
    assert host_key(value) == key


@pytest.mark.django_db(transaction=True)
def test_pages_and_origin_media_share_one_slot_across_lanes() -> None:
    starts = Starts()
    pages = client_for(DbHostGate("pages"), starts.handler())
    media = client_for(DbHostGate("media"), starts.handler())

    pages.get("https://site.test/video/1", min_delay_s=0.4)
    media.get("https://www.site.test/posters/1.jpg", min_delay_s=0.4)
    pages.get("https://site.test/video/2", min_delay_s=0.4)

    assert [path for path, _ in starts.at] == ["/video/1", "/posters/1.jpg", "/video/2"]
    assert all(gap >= 0.35 for gap in starts.gaps())
    assert list(HostSlot.objects.values_list("key", flat=True)) == ["site.test"]
    assert slot("site.test").leased_until is None


@pytest.mark.django_db(transaction=True)
def test_a_cdn_host_has_its_own_slot() -> None:
    starts = Starts()
    pages = client_for(DbHostGate("pages"), starts.handler())
    media = client_for(DbHostGate("media"), starts.handler())

    pages.get("https://site.test/video/1", min_delay_s=5.0)
    began = time.monotonic()
    media.get("https://cdn.example/posters/1.jpg", min_delay_s=1.0)

    assert time.monotonic() - began < 1.0  # not behind the site's 5 s
    assert set(HostSlot.objects.values_list("key", flat=True)) == {"site.test", "cdn.example"}


@pytest.mark.django_db(transaction=True)
def test_threads_with_their_own_connections_never_overlap_or_undercut_the_delay() -> None:
    starts = Starts()
    errors: list[BaseException] = []

    def work(lane: str, count: int) -> None:
        try:
            client = client_for(DbHostGate(lane), starts.handler(hold_s=0.05))
            for index in range(count):
                client.get(f"https://site.test/{lane}/{index}", min_delay_s=0.25)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=work, args=(lane, 3)) for lane in ("pages", "media", "x")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(starts.at) == 9
    assert starts.max_in_flight == 1
    assert min(starts.gaps()) >= 0.2


@pytest.mark.django_db(transaction=True)
def test_the_stricter_delay_of_two_callers_wins() -> None:
    starts = Starts()
    slow = client_for(DbHostGate("pages"), starts.handler())
    fast = client_for(DbHostGate("media"), starts.handler())

    slow.get("https://site.test/page", min_delay_s=0.6)
    fast.get("https://site.test/poster.jpg", min_delay_s=0.05)

    assert starts.gaps()[0] >= 0.53


@pytest.mark.django_db(transaction=True)
def test_the_operator_floor_raises_every_delay() -> None:
    starts = Starts()
    client = client_for(DbHostGate("media"), starts.handler())
    client.get("https://cdn.example/1.jpg", min_delay_s=0.0)
    HostSlot.objects.filter(pk="cdn.example").update(min_delay_s=0.5)
    client.get("https://cdn.example/2.jpg", min_delay_s=0.0)
    assert starts.gaps()[0] >= 0.43


@pytest.mark.django_db(transaction=True)
def test_a_refused_check_leaves_the_slot_untouched() -> None:
    starts = Starts()
    client = client_for(DbHostGate("media"), starts.handler())
    client.get("https://site.test/first.jpg", min_delay_s=0.0)
    before = slot("site.test")

    def deny(url: str) -> None:
        raise RequestRefusedError("page blocked")

    with pytest.raises(RequestRefusedError):
        client.get("https://site.test/second.jpg", min_delay_s=30.0, before_request=deny)

    after = slot("site.test")
    assert len(starts.at) == 1
    assert after.next_allowed_at == before.next_allowed_at
    assert after.last_started_at == before.last_started_at
    assert after.leased_until is None and after.lease_owner == ""


@pytest.mark.django_db(transaction=True)
def test_an_exhausted_budget_takes_no_slot() -> None:
    starts = Starts()
    client = client_for(DbHostGate("pages"), starts.handler(), observer=NoBudget())

    with pytest.raises(BudgetExhaustedError):
        client.get("https://site.test/page", min_delay_s=60.0)

    row = slot("site.test")
    assert starts.at == []
    assert row.last_started_at is None and row.leased_until is None
    # The next caller is not held back by a request that was never sent.
    other = client_for(DbHostGate("media"), starts.handler())
    began = time.monotonic()
    other.get("https://site.test/poster.jpg", min_delay_s=60.0)
    assert time.monotonic() - began < 1.0


@pytest.mark.django_db(transaction=True)
def test_a_page_blocked_while_waiting_for_the_slot_is_never_requested() -> None:
    starts = Starts()
    blocked = threading.Event()
    first = client_for(DbHostGate("pages"), starts.handler())
    first.get("https://site.test/page", min_delay_s=0.5)
    threading.Timer(0.2, blocked.set).start()  # the page gets blocked during the wait

    def deny(url: str) -> None:
        if blocked.is_set():
            raise RequestRefusedError("safety: blocked")

    media = client_for(DbHostGate("media"), starts.handler())
    with pytest.raises(RequestRefusedError):
        media.get("https://site.test/poster.jpg", min_delay_s=0.5, before_request=deny)
    assert [path for path, _ in starts.at] == ["/page"]


@pytest.mark.django_db(transaction=True)
def test_an_expired_lease_is_reclaimed_and_a_live_one_makes_the_host_busy() -> None:
    starts = Starts()
    past = timezone.now() - timedelta(minutes=5)
    HostSlot.objects.create(
        key="site.test", next_allowed_at=past, leased_until=past, lease_owner="dead:worker"
    )
    client_for(DbHostGate("pages"), starts.handler()).get("https://site.test/a", min_delay_s=0)
    assert len(starts.at) == 1

    HostSlot.objects.filter(pk="site.test").update(
        leased_until=timezone.now() + timedelta(minutes=5), lease_owner="alive:worker"
    )
    gate = DbHostGate("media", max_wait_s=0.5)
    began = time.monotonic()
    with pytest.raises(HostBusyError) as busy:
        client_for(gate, starts.handler()).get("https://site.test/b", min_delay_s=0)
    assert 0.45 <= time.monotonic() - began < 3.0
    assert busy.value.host == "site.test"
    assert len(starts.at) == 1


@pytest.mark.django_db(transaction=True)
def test_a_penalty_blocks_every_lane_and_a_long_one_is_busy_at_once() -> None:
    starts = Starts()
    DbHostGate("media").penalize("https://cdn.example/x.jpg", 0.4, "HTTP 429")
    began = time.monotonic()
    client_for(DbHostGate("pages"), starts.handler()).get("https://cdn.example/a", min_delay_s=0)
    assert time.monotonic() - began >= 0.35
    assert slot("cdn.example").penalty_reason == "HTTP 429"

    DbHostGate("media").penalize("cdn.example", 3600, "HTTP 503")
    began = time.monotonic()
    with pytest.raises(HostBusyError) as busy:
        client_for(DbHostGate("pages", max_wait_s=60), starts.handler()).get(
            "https://cdn.example/b", min_delay_s=0
        )
    assert time.monotonic() - began < 1.0  # never sleeps behind a long penalty
    assert busy.value.retry_at > timezone.now() + timedelta(minutes=59)


@pytest.mark.django_db(transaction=True)
def test_a_short_penalty_does_not_shorten_the_host_s_delay() -> None:
    """A penalty may only ever push the next request out, never pull it in.

    `penalize` writes `penalty_until` and nothing else, and `_ACQUIRE` demands the delay *and*
    the penalty, so a backoff shorter than what is left of the delay must not become a way to
    ask sooner. Every other penalty test here runs with `min_delay_s=0`, which cannot see it.
    """
    starts = Starts()
    client = client_for(DbHostGate("media"), starts.handler())

    client.get("https://cdn.example/1.jpg", min_delay_s=0.6)
    DbHostGate("pages").penalize("cdn.example", 0.05, "HTTP 503")
    client.get("https://cdn.example/2.jpg", min_delay_s=0.6)

    assert len(starts.at) == 2
    assert starts.gaps()[0] >= 0.53, "the penalty must not have replaced the start-to-start delay"


@pytest.mark.django_db(transaction=True)
def test_a_retry_status_penalises_the_host_for_everyone() -> None:
    statuses = [503, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses.pop(0), headers={"retry-after": "1"})

    client = client_for(DbHostGate("media"), handler)
    began = time.monotonic()
    response = client.get("https://cdn.example/x.jpg", min_delay_s=0)
    assert response.status == 200 and response.attempts == 2
    assert time.monotonic() - began >= 0.95
    assert slot("cdn.example").penalty_reason == "HTTP 503"


def lanes(key: str) -> tuple[str, str]:
    row = slot(key)
    return row.last_lane, row.waiting_lane


@pytest.mark.django_db(transaction=True)
def test_a_waiting_lane_gets_the_next_turn() -> None:
    now = timezone.now()
    HostSlot.objects.create(
        key="site.test",
        next_allowed_at=now - timedelta(seconds=1),
        last_lane="pages",
        waiting_lane="media",
        waiting_until=now + timedelta(seconds=30),
    )
    with pytest.raises(HostBusyError):
        DbHostGate("pages", max_wait_s=0.3).acquire("site.test", 0)

    turn = DbHostGate("media", max_wait_s=0.3).acquire("site.test", 0)
    assert lanes("site.test") == ("media", "")
    turn.start(0)
    turn.done()


@pytest.mark.django_db(transaction=True)
def test_a_lane_alone_on_a_host_gets_every_turn() -> None:
    gate = DbHostGate("media", max_wait_s=0.5)
    for _ in range(3):
        turn = gate.acquire("cdn.example", 0)
        turn.start(0)
        turn.done()
    assert lanes("cdn.example") == ("media", "")


@pytest.mark.django_db(transaction=True)
def test_a_waiting_mark_is_left_for_the_other_lane() -> None:
    holder = DbHostGate("pages")
    turn = holder.acquire("site.test", 0)
    turn.start(5.0)
    turn.done()
    with pytest.raises(HostBusyError):
        DbHostGate("media", max_wait_s=0.2).acquire("site.test", 5.0)
    assert lanes("site.test") == ("pages", "media")


@pytest.mark.django_db(transaction=True)
def test_the_delay_counts_on_the_database_clock_not_the_transaction_start() -> None:
    gate = DbHostGate("pages")
    with transaction.atomic():
        time.sleep(0.4)  # now() stays at the transaction start, clock_timestamp() moves on
        turn = gate.acquire("site.test", 1.0)
        turn.start(1.0)
        turn.done()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT extract(epoch FROM next_allowed_at - clock_timestamp()) "
                "FROM core_hostslot WHERE key = 'site.test'"
            )
            row = cursor.fetchone()
    assert row is not None and float(row[0]) > 0.9


@pytest.mark.django_db(transaction=True)
def test_a_nested_turn_at_a_held_host_does_not_wait_for_itself() -> None:
    gate = DbHostGate("media", max_wait_s=0.5)
    outer = gate.acquire("cdn.example", 10.0)
    inner = gate.acquire("www.cdn.example", 10.0)
    inner.start(10.0)
    inner.done()
    outer.start(10.0)
    outer.done()
    assert slot("cdn.example").leased_until is None


@pytest.mark.django_db(transaction=True)
def test_granted_turns_report_their_wait() -> None:
    waits: list[SlotWait] = []
    gate = DbHostGate("pages", on_wait=waits.append)
    for _ in range(2):
        turn = gate.acquire("site.test", 1.0)
        turn.start(1.0)
        turn.done()
    assert [wait.key for wait in waits] == ["site.test", "site.test"]
    assert waits[0].waited_s < 0.7 <= waits[1].waited_s


@pytest.mark.django_db
def test_the_admin_saves_only_the_operator_floor() -> None:
    from django.contrib.admin.sites import site as admin_site
    from django.test import RequestFactory

    from core.admin import HostSlotAdmin

    started = timezone.now()
    HostSlot.objects.create(key="cdn.example", next_allowed_at=started)
    stale = slot("cdn.example")
    later = started + timedelta(seconds=30)
    HostSlot.objects.filter(pk="cdn.example").update(next_allowed_at=later, lease_owner="media:x")
    stale.min_delay_s = 2.5

    HostSlotAdmin(HostSlot, admin_site).save_model(RequestFactory().post("/"), stale, None, True)

    row = slot("cdn.example")
    assert (row.min_delay_s, row.next_allowed_at, row.lease_owner) == (2.5, later, "media:x")


class DripClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_dripping_response_hits_the_deadline_and_is_not_retried() -> None:
    clock = DripClock()
    requests: list[str] = []

    def drip() -> Iterator[bytes]:
        for _ in range(10):
            clock.now += 19.0
            yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=drip())

    client = PoliteClient(
        UA,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=lambda seconds: None,
        deadline_s=60.0,
    )
    with pytest.raises(RequestDeadlineError):
        client.get("https://slow.example/")
    assert len(requests) == 1
