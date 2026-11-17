"""The crawl runtime (ADR 0030, issue #75): fair rotation, isolation, per-site locks shared with
the legacy workers, graceful stop, and both lanes end to end on database host slots."""

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from io import StringIO
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog.models import HostRole, RequestCount, Site, SiteHost
from catalog.request_accounting import HostRoles
from core.host_slots import DbHostGate
from core.http import PoliteClient
from core.locks import CRAWL_WORKER_LOCKS, RUNTIME_LANE_KEYS, RUNTIME_LOCKS
from core.models import HostSlot
from crawler import services
from crawler.models import CrawlTask
from crawler.sites.base import TaskSpec
from crawler.tests.helpers import load_fixture
from index.models import FrameHash, Media, Page
from marceline.management.commands.crawl_runtime import parse_sites
from marceline.runtime.hash_pool import HashPool, HashPoolError
from marceline.runtime.lanes import (
    PAGES,
    RELEASED,
    LaneOptions,
    MediaWork,
    OnWait,
    PagesWork,
    ThreadWorker,
)
from marceline.runtime.rotation import SiteRotation
from marceline.runtime.runtime import (
    STOP_ONCE,
    LaneBusyError,
    Runtime,
    RuntimeConfig,
    summary_line,
)
from matching import media as media_queue
from matching.frames import hash_poster
from matching.media_hash import HasherUnavailableError, UnsupportedMediaError, hash_media_bytes
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker, slot_media_delay_s
from matching.pdq import MediaDecodeError
from matching.tests.images import sprite_sheet, synthetic_image, to_bytes

UA = "MarcelineBot/0.1"
PUBLIC = ["93.184.216.34"]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# --- rotation -----------------------------------------------------------------------------------


def test_rotation_is_round_robin_and_skips_busy_and_resting_sites() -> None:
    clock = Clock()
    rotation = SiteRotation(clock)
    rotation.set_sites(["b.test", "a.test", "c.test"])

    first = rotation.take()
    second = rotation.take()
    assert (first, second) == ("a.test", "b.test")
    rotation.give_back("a.test")
    assert rotation.take() == "c.test"  # a is free again, but c comes first in the round
    assert rotation.take() == "a.test"
    assert rotation.take() is None  # every site busy
    rotation.give_back("b.test", rest_s=10)
    rotation.give_back("c.test")
    rotation.give_back("a.test")
    assert rotation.take() == "c.test"
    assert rotation.take() == "a.test"
    assert rotation.take() is None  # b rests
    assert rotation.ready_in() == 10
    clock.now += 10
    assert rotation.take() == "b.test"


def test_rotation_follows_site_changes_and_knows_when_everything_rests() -> None:
    clock = Clock()
    rotation = SiteRotation(clock)
    assert rotation.all_resting()
    rotation.set_sites(["a.test", "b.test"])
    assert not rotation.all_resting()
    for site in ("a.test", "b.test"):
        assert rotation.take() == site
        rotation.give_back(site, rest_s=5)
    assert rotation.all_resting()
    rotation.set_sites(["a.test"])
    assert rotation.sites() == ["a.test"]
    clock.now += 5
    assert rotation.take() == "a.test"
    assert rotation.take() is None


def test_summary_line_names_sites_hosts_and_errors() -> None:
    from collections import Counter

    line = summary_line(
        Counter({("a.test", "done"): 3, ("a.test", "skipped"): 1, ("b.test", "failed"): 2}),
        Counter({"b.test": 1}),
        {"a.test": [0.1, 2.0, 3.5], "cdn.test": [0.0]},
    )
    assert line == (
        "sites: a.test done=3 skipped=1 | b.test failed=2; "
        "slots: a.test n=3 p50=2.0s max=3.5s | cdn.test n=1 p50=0.0s max=0.0s; errors: b.test=1"
    )


def test_sites_must_be_named_or_all() -> None:
    with pytest.raises(CommandError, match="RUNTIME_SITES"):
        parse_sites([])
    with pytest.raises(CommandError):
        parse_sites([" ", ""])
    assert parse_sites(["all"]) is None
    assert parse_sites(["site20.example", " site06.example"]) == frozenset(
        {"site20.example", "site06.example"}
    )


# --- the loop, with a fake lane -----------------------------------------------------------------


@dataclass
class FakeItem:
    pk: int
    status: str = "running"


@dataclass
class FakeWork:
    queues: dict[str, list[int]]
    broken: set[str] = field(default_factory=set)
    block: threading.Event | None = None
    #: How long `process` waits for `block`; long enough to outlive a whole run when a test wants
    #: a thread that really does not come back.
    block_timeout_s: float = 3.0
    name: str = PAGES
    lock_space: int = CRAWL_WORKER_LOCKS
    needs_adapter: bool = False
    order: list[int] = field(default_factory=list)
    released: list[int] = field(default_factory=list)
    maintained: int = 0
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def thread_worker(self, stop: threading.Event, on_wait: OnWait) -> ThreadWorker:
        return FakeThread(self)

    def maintenance(self, domains: object) -> None:
        self.maintained += 1

    def release(self, ids: object) -> int:
        assert isinstance(ids, list)
        self.released.extend(ids)
        return len(ids)

    def close(self) -> None:
        self.closed = True


class FakeThread:
    def __init__(self, work: FakeWork) -> None:
        self.work = work

    def claim(self, domain: str) -> FakeItem | None:
        with self.work.lock:
            if domain in self.work.broken:
                raise RuntimeError("claim exploded")
            queue = self.work.queues.get(domain, [])
            return FakeItem(queue.pop(0)) if queue else None

    def process(self, item: object) -> str:
        assert isinstance(item, FakeItem)
        with self.work.lock:
            self.work.order.append(item.pk)
        if self.work.block is not None:
            self.work.block.wait(self.work.block_timeout_s)
        item.status = "done"
        return item.status

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


def config(**overrides: object) -> RuntimeConfig:
    values: dict[str, object] = {
        "threads": 1,
        "sites": None,
        "idle_s": 5.0,
        "once": True,
        "watchdog_s": None,
        "install_signals": False,
        # The pytest process is far bigger than a lane; RSS restarts are not under test here.
        "max_rss_mb": 1_000_000.0,
    }
    values.update(overrides)
    return RuntimeConfig(**values)  # type: ignore[arg-type]


def make_sites(*domains: str) -> list[Site]:
    return [Site.objects.create(domain=domain, crawl_delay_s=0) for domain in domains]


@pytest.fixture
def other_session() -> Iterator[BaseDatabaseWrapper]:
    """A second database session, like a legacy worker's process."""
    other = connections.create_connection("default")
    try:
        yield other
    finally:
        other.close()


def lock_in(session: BaseDatabaseWrapper, space: int, key: int) -> None:
    with session.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [space, key])


@pytest.mark.django_db(transaction=True)
def test_sites_take_turns_so_a_big_backlog_cannot_starve_a_small_site() -> None:
    make_sites("a.test", "b.test")
    work = FakeWork({"a.test": [101, 102, 103, 104, 105], "b.test": [201, 202]})

    reason = Runtime(work, config()).run()

    assert reason == STOP_ONCE
    assert work.order == [101, 201, 102, 202, 103, 104, 105]
    assert work.maintained == 1 and work.closed


@pytest.mark.django_db(transaction=True)
def test_one_failing_site_does_not_stop_the_others() -> None:
    make_sites("a.test", "broken.test")
    work = FakeWork({"a.test": [1, 2, 3]}, broken={"broken.test"})
    runtime = Runtime(work, config(threads=2))

    assert runtime.run() == STOP_ONCE
    assert sorted(work.order) == [1, 2, 3]
    assert runtime.stats.processed_total == 3


@pytest.mark.django_db(transaction=True)
def test_only_allowlisted_enabled_sites_are_served() -> None:
    make_sites("a.test", "b.test")
    Site.objects.create(domain="off.test", enabled=False)
    work = FakeWork({"a.test": [1], "b.test": [2], "off.test": [3]})

    Runtime(work, config(sites=frozenset({"b.test", "off.test"}))).run()

    assert work.order == [2]


@pytest.mark.django_db(transaction=True)
def test_a_site_whose_legacy_worker_holds_the_lock_is_skipped(
    other_session: BaseDatabaseWrapper,
) -> None:
    _, legacy = make_sites("a.test", "legacy.test")
    lock_in(other_session, CRAWL_WORKER_LOCKS, legacy.pk)
    work = FakeWork({"a.test": [1], "legacy.test": [2]})

    Runtime(work, config()).run()

    assert work.order == [1]
    assert work.queues["legacy.test"] == [2]


@pytest.mark.django_db(transaction=True)
def test_a_legacy_worker_cannot_start_for_a_site_the_runtime_serves() -> None:
    make_sites("a.test")
    started = threading.Event()
    work = FakeWork({"a.test": [1]}, block=started)
    runtime = Runtime(work, config(once=False, grace_s=5.0))
    errors: list[str] = []

    def legacy() -> None:
        try:
            deadline = time.monotonic() + 10
            while not runtime.rotation.sites() and time.monotonic() < deadline:
                time.sleep(0.05)
            with pytest.raises(CommandError, match="one crawler per site"):
                call_command("crawl_worker", "--site", "a.test", "--once", stdout=StringIO())
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))
        finally:
            connections.close_all()
            started.set()
            runtime.stop("test done")

    threading.Thread(target=legacy).start()
    assert runtime.run() == "test done"
    assert errors == []
    # The lock was released again: a legacy worker may take over now.
    call_command("crawl_worker", "--site", "a.test", "--once", stdout=StringIO())


@pytest.mark.django_db(transaction=True)
def test_a_second_runtime_for_the_same_lane_refuses(other_session: BaseDatabaseWrapper) -> None:
    lock_in(other_session, RUNTIME_LOCKS, RUNTIME_LANE_KEYS[PAGES])
    with pytest.raises(LaneBusyError):
        Runtime(FakeWork({}), config()).run()


@pytest.mark.django_db(transaction=True)
def test_stopping_releases_what_did_not_finish_within_the_grace_period() -> None:
    make_sites("a.test")
    never = threading.Event()
    work = FakeWork({"a.test": [7, 8]}, block=never)
    runtime = Runtime(work, config(once=False, grace_s=0.3))
    threading.Timer(1.0, runtime.stop, args=("SIGTERM",)).start()

    assert runtime.run() == "SIGTERM"
    assert work.order == [7]
    assert work.released == [7]
    assert work.closed


@pytest.mark.django_db(transaction=True)
def test_a_thread_that_never_comes_back_cannot_keep_the_lane_alive() -> None:
    """A lane restarts by *exiting*: compose only starts a fresh process once this one ends.

    Past the grace period the dispatcher has handed the row back and dropped every lock, so an
    unfinished thread is abandoned by design. If it were a non-daemon thread the interpreter
    would join it again at exit and the container would stay up serving nothing — no crawling,
    no locks, no restart, and nothing in the logs after the stop line.
    """
    make_sites("a.test")
    never = threading.Event()
    work = FakeWork({"a.test": [7]}, block=never, block_timeout_s=120.0)
    runtime = Runtime(work, config(once=False, grace_s=0.3))
    threading.Timer(0.5, runtime.stop, args=("SIGTERM",)).start()
    try:
        assert runtime.run() == "SIGTERM"

        assert work.released == [7]
        stuck = [thread for thread in runtime._threads if thread.is_alive()]
        assert stuck, "the point of the test is a thread still running after the grace period"
        assert all(thread.daemon for thread in stuck), (
            "a lane task thread must be a daemon thread: a hung one would block interpreter "
            "shutdown and the lane would never be restarted"
        )
    finally:
        never.set()
        for thread in runtime._threads:
            thread.join(5)


@pytest.mark.django_db(transaction=True)
def test_a_dispatcher_database_error_stops_serving_until_the_locks_are_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every advisory lock lives on the dispatcher's connection, so closing it drops them all.

    Until they are re-taken the lane must serve nothing: a legacy `crawl_worker --site D` can
    take the freed lock and crawl the same site alongside this lane's threads, and one worker per
    site is exactly what those locks are for. `lock_retry_s` is 1000 s here, so only an immediate
    re-sync can get the site back.
    """
    make_sites("a.test")
    work = FakeWork({})
    runtime = Runtime(work, config(once=False, lock_retry_s=1000.0, maintenance_s=0.05))
    syncs: list[list[str]] = []
    sync_sites = Runtime._sync_sites

    def record(self: Runtime) -> None:
        syncs.append(self.rotation.sites())  # what the lane was serving when the sync began
        sync_sites(self)

    monkeypatch.setattr(Runtime, "_sync_sites", record)
    calls = iter([None, OperationalError("server closed the connection unexpectedly")])

    def maintenance(domains: object) -> None:
        work.maintained += 1
        if (failure := next(calls, None)) is not None:
            raise failure

    monkeypatch.setattr(work, "maintenance", maintenance)
    threading.Timer(3.0, runtime.stop, args=("done",)).start()

    assert runtime.run() == "done"

    assert syncs[0] == [], "the startup sync runs before any site is served"
    assert len(syncs) >= 2, "the locks must be re-taken on the next tick, not in lock_retry_s"
    assert syncs[1] == [], "the lane must stop serving the sites whose locks it just lost"
    assert runtime.rotation.sites() == ["a.test"], "and take them again once it can"


@pytest.mark.django_db(transaction=True)
def test_changed_migrations_restart_the_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    make_sites("a.test")
    ids = iter([41, 42, 42, 42])
    monkeypatch.setattr(Runtime, "_latest_migration", staticmethod(lambda: next(ids)))

    runtime = Runtime(FakeWork({}), config(once=False, migration_check_s=0.1))

    assert runtime.run() == "applied migrations changed"


@pytest.mark.django_db(transaction=True)
def test_the_command_refuses_without_sites_and_stops_when_idle(settings: Settings) -> None:
    with pytest.raises(CommandError, match="RUNTIME_SITES"):
        call_command("crawl_runtime", "--lane", "pages", stdout=StringIO())
    settings.RUNTIME_MAX_RSS_MB = 1_000_000  # the test process is not the lane's size
    previous = signal.getsignal(signal.SIGTERM)
    out = StringIO()
    call_command("crawl_runtime", "--lane", "pages", "--sites", "none.test", "--once", stdout=out)
    assert out.getvalue().strip() == f"pages lane stopped: {STOP_ONCE}"
    assert signal.getsignal(signal.SIGTERM) is previous


# --- pages lane end to end ----------------------------------------------------------------------

SITE01_ROBOTS = "https://site01.example/robots.txt"
VIDEO_URL = "https://site01.example/video/2099/lena-hartwell-new-studio-session-part-1/"


@pytest.mark.django_db(transaction=True)
def test_pages_lane_crawls_on_host_slots_and_counts_requests() -> None:
    site01, _ = make_sites("site01.example", "no-adapter.test")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == SITE01_ROBOTS:
            return httpx.Response(200, text="User-agent: *\nDisallow: /embed/*\n")
        if str(request.url) == VIDEO_URL:
            body = load_fixture("site01.example", "video-2.html")
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")

    services.enqueue(site01, [TaskSpec(VIDEO_URL, CrawlTask.Kind.DETAIL)])
    work = PagesWork(
        LaneOptions(UA, deadline_s=30, slot_max_wait_s=10, transport=httpx.MockTransport(handler))
    )

    Runtime(work, config(threads=3)).run()

    task = CrawlTask.objects.get(url=VIDEO_URL)
    assert task.status == CrawlTask.Status.DONE
    assert Page.objects.filter(url=VIDEO_URL).exists()
    assert requested == [SITE01_ROBOTS, VIDEO_URL]
    slot = HostSlot.objects.get(pk="site01.example")
    assert slot.last_lane == PAGES and slot.leased_until is None
    kinds = set(RequestCount.objects.filter(site=site01).values_list("kind", flat=True))
    assert kinds == {"page", "robots"}


@pytest.mark.django_db(transaction=True)
def test_a_task_released_at_shutdown_gets_its_attempt_back() -> None:
    (site,) = make_sites("site01.example")
    services.enqueue(site, [TaskSpec(VIDEO_URL, CrawlTask.Kind.DETAIL)])
    task = services.claim_task()
    assert task is not None and task.attempts == 1

    work = PagesWork(LaneOptions(UA, deadline_s=30, slot_max_wait_s=10))
    assert work.release([task.pk]) == 1

    task.refresh_from_db()
    assert (task.status, task.attempts, task.last_error) == ("pending", 0, RELEASED)


# --- media lane ---------------------------------------------------------------------------------


def media_on(site: Site, url: str, *, status: str = "clear") -> Media:
    page = Page.objects.create(
        site=site, url=f"https://{site.domain}/v/{url[-8:]}", safety_score=1.0, safety_status=status
    )
    return Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=url, eligible_at=timezone.now()
    )


@pytest.mark.django_db(transaction=True)
def test_media_delay_follows_the_host_role() -> None:
    site = Site.objects.create(domain="example.test", crawl_delay_s=4.0)
    roles = HostRoles()
    origin = media_on(site, "https://www.example.test/p/1.jpg")
    subdomain = media_on(site, "https://img.example.test/p/2.jpg")
    cdn = media_on(site, "https://cdn.other.test/p/3.jpg")
    SiteHost.objects.create(site=site, host="fast.example.test", role=HostRole.CDN)
    reviewed_cdn = media_on(site, "https://fast.example.test/p/4.jpg")

    delays = [
        slot_media_delay_s(media, roles, cdn_delay_s=1.0)
        for media in (origin, subdomain, cdn, reviewed_cdn)
    ]

    assert delays == [4.0, 4.0, 1.0, 1.0]


@pytest.mark.django_db(transaction=True)
def test_media_lane_hashes_in_a_child_process_on_the_cdn_slot() -> None:
    site = Site.objects.create(domain="example.test", crawl_delay_s=30.0)
    png = to_bytes(synthetic_image(3))
    media = media_on(site, "https://cdn.example-cdn.test/poster.png")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/poster.png":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(404, text="no robots.txt")

    options = LaneOptions(
        UA,
        deadline_s=30,
        slot_max_wait_s=10,
        hash_processes=1,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: PUBLIC,
    )
    work = MediaWork(options)

    Runtime(work, config(threads=2)).run()

    media.refresh_from_db()
    assert media.status == Media.Status.DONE
    assert requested == [
        "https://cdn.example-cdn.test/robots.txt",
        "https://cdn.example-cdn.test/poster.png",
    ]
    stored = list(FrameHash.objects.filter(media=media).values_list("pdq", flat=True))
    assert [bytes(value) for value in stored] == [frame.hash for frame in hash_poster(png)]
    slot = HostSlot.objects.get(pk="cdn.example-cdn.test")
    assert slot.last_delay_s == 1.0  # the CDN delay, not the site's 30 s


@pytest.mark.django_db(transaction=True)
def test_a_page_blocked_after_the_claim_causes_no_media_request_on_host_slots() -> None:
    site = Site.objects.create(domain="example.test", crawl_delay_s=0)
    media = media_on(site, "https://cdn.example-cdn.test/poster.png")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(404)

    client = PoliteClient(
        UA,
        max_bytes=CLIENT_MAX_BYTES,
        observer=None,
        public_only=True,
        resolver=lambda host: PUBLIC,
        transport=httpx.MockTransport(handler),
        gate=DbHostGate("media"),
    )
    claimed = media_queue.claim_media()
    assert claimed is not None and claimed.pk == media.pk
    Page.objects.filter(pk=media.page_id).update(safety_status=Page.SafetyStatus.BLOCKED)

    MediaWorker(client, user_agent=UA).process_claimed(claimed)

    claimed.refresh_from_db()
    assert claimed.status == Media.Status.SKIPPED
    assert requested == []


def refuse_all(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"nothing may be requested: {request.url}")


@pytest.mark.django_db(transaction=True)
def test_a_busy_host_hands_a_crawl_task_back_with_its_attempt_refunded() -> None:
    (site,) = make_sites("site01.example")
    services.enqueue(site, [TaskSpec(VIDEO_URL, CrawlTask.Kind.DETAIL)])
    DbHostGate("media").penalize("site01.example", 3600, "HTTP 429")
    work = PagesWork(
        LaneOptions(UA, deadline_s=30, slot_max_wait_s=5, transport=httpx.MockTransport(refuse_all))
    )
    thread = work.thread_worker(threading.Event(), lambda wait: None)
    task = thread.claim("site01.example")
    assert task is not None

    thread.process(task)
    thread.close()

    stored = CrawlTask.objects.get(url=VIDEO_URL)
    assert stored.status == CrawlTask.Status.PENDING
    assert stored.attempts == 0
    assert stored.next_attempt_at is not None
    assert stored.next_attempt_at > timezone.now() + timedelta(minutes=59)
    assert "host slot busy" in stored.last_error


@pytest.mark.django_db(transaction=True)
def test_a_busy_host_defers_media_with_its_attempt_refunded() -> None:
    site = Site.objects.create(domain="example.test", crawl_delay_s=0)
    media = media_on(site, "https://cdn.example-cdn.test/poster.png")
    other = media_on(site, "https://cdn.example-cdn.test/other.png")
    DbHostGate("pages").penalize("cdn.example-cdn.test", 3600, "HTTP 503")
    work = MediaWork(
        LaneOptions(
            UA,
            deadline_s=30,
            slot_max_wait_s=5,
            transport=httpx.MockTransport(refuse_all),
            resolver=lambda host: PUBLIC,
        ),
        pool=HashPool(1, deadline_s=5),
    )
    try:
        thread = work.thread_worker(threading.Event(), lambda wait: None)
        claimed = thread.claim("example.test")
        assert claimed is not None
        thread.process(claimed)
        thread.close()
    finally:
        work.close()

    for row in (media, other):
        row.refresh_from_db()
        assert row.status == Media.Status.PENDING and row.attempts == 0
        assert row.next_attempt_at is not None
        assert row.next_attempt_at > timezone.now() + timedelta(minutes=59)


# --- hash pool ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pool() -> Iterator[HashPool]:
    hash_pool = HashPool(1, deadline_s=60)
    try:
        yield hash_pool
    finally:
        hash_pool.close()


@pytest.fixture(scope="module")
def preview_mp4() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "preview.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=2:size=320x240:rate=10",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            timeout=60,
        )
        return path.read_bytes()


@pytest.mark.parametrize("kind", ["poster", "frame", "photo"])
def test_hash_children_hash_like_the_worker_thread(pool: HashPool, kind: str) -> None:
    data = to_bytes(synthetic_image(5))
    assert pool.hash(kind, data) == hash_media_bytes(kind, data)


def test_hash_children_hash_preview_clips_like_the_worker_thread(
    pool: HashPool, preview_mp4: bytes
) -> None:
    assert pool.hash("preview", preview_mp4) == hash_media_bytes("preview", preview_mp4)


def test_hash_children_hash_a_sprite_grid_like_the_worker_thread(pool: HashPool) -> None:
    """`sprite_grid` travels over the pipe alongside `crop_bars` (issue #14)."""
    data = to_bytes(sprite_sheet(4, 3, (120, 90)))
    assert pool.hash("sprite", data, sprite_grid=(4, 3)) == hash_media_bytes(
        "sprite", data, sprite_grid=(4, 3)
    )


def test_a_sprite_with_no_confident_grid_is_unsupported_in_the_caller(pool: HashPool) -> None:
    """A child that could not determine the grid raises `UnsupportedMediaError`, not
    `HashPoolError` — this is a deliberate skip of the picture, never a broken hasher."""
    with pytest.raises(UnsupportedMediaError, match="sprite grid unknown"):
        pool.hash("sprite", to_bytes(synthetic_image(7)))


def test_undecodable_bytes_are_a_decode_error_in_the_caller(pool: HashPool) -> None:
    with pytest.raises(MediaDecodeError):
        pool.hash("poster", b"not an image")
    assert pool.hash("poster", to_bytes(synthetic_image(6))).frames


def test_a_pool_failure_is_a_hasher_that_was_unavailable_not_a_bad_picture() -> None:
    """What the media worker branches on: the row is retried, not failed for good.

    A killed, timed-out or shut-down child says nothing about the bytes, so `HashPoolError` must
    stay a `HasherUnavailableError` (`matching/tests/test_media_worker.py` has the other half).
    """
    assert issubclass(HashPoolError, HasherUnavailableError)
    assert not issubclass(HashPoolError, MediaDecodeError)


def test_a_killed_child_fails_its_job_and_the_pool_keeps_serving() -> None:
    hash_pool = HashPool(1, deadline_s=60)
    try:
        (pid,) = hash_pool.pids()
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
        with pytest.raises(HashPoolError, match="died"):
            hash_pool.hash("poster", to_bytes(synthetic_image(7)))
        assert hash_pool.restarts == 1
        assert hash_pool.hash("poster", to_bytes(synthetic_image(7))).frames
        assert hash_pool.pids() != [pid]
    finally:
        hash_pool.close()


def test_a_job_over_its_deadline_is_killed(preview_mp4: bytes) -> None:
    hash_pool = HashPool(1, deadline_s=0.001)
    try:
        with pytest.raises(HashPoolError, match="timed out"):
            hash_pool.hash("preview", preview_mp4)
        assert hash_pool.restarts == 1
    finally:
        hash_pool.close()


def test_children_are_recycled_after_their_job_count() -> None:
    hash_pool = HashPool(1, deadline_s=60, max_jobs=2)
    try:
        (first,) = hash_pool.pids()
        data = to_bytes(synthetic_image(8))
        hash_pool.hash("poster", data)
        assert hash_pool.pids() == [first]
        hash_pool.hash("poster", data)
        assert hash_pool.pids() != [first]
        assert hash_pool.restarts == 0  # recycling is not a failure
    finally:
        hash_pool.close()
