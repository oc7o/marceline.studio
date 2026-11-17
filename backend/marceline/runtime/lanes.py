"""What each lane does: claim one row of one site, process it, and give rows back on shutdown.

The work itself is the legacy workers' code — `crawler.worker.CrawlWorker` and
`matching.media_worker.MediaWorker` — with a `PoliteClient` per thread on a `DbHostGate`, one
robots.txt cache shared by the lane's threads and, for media, hashing in `HashPool` children.
Every client counts its requests (`RequestAccounting`, issue #74); budgets, the safety deny, media
robots.txt, `public_only`, site pauses and refunds work exactly as in the legacy workers.
"""

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone

from catalog.request_accounting import HostRoles, RequestAccounting
from core.host_slots import DbHostGate, SlotWait
from core.http import FetchResponse, PoliteClient, Resolver, resolve_host
from core.locks import CRAWL_WORKER_LOCKS, MEDIA_WORKER_LOCKS
from core.robots import RobotsCache
from crawler import services as crawl_services
from crawler.models import CrawlTask
from crawler.worker import CrawlWorker
from index.models import Media
from marceline.runtime.hash_pool import HashPool
from matching import media as media_queue
from matching.frames import FFMPEG_TIMEOUT_S
from matching.media_robots import MediaRobots
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker, slot_media_delay_s

PAGES = "pages"
MEDIA = "media"
LANES = (PAGES, MEDIA)
RELEASED = "released: crawl runtime stopped"
#: httpx's per-read timeout in `PoliteClient` (default 20 s) on top of the whole-response deadline,
#: plus room for the checks inside the turn: a host's lease must outlive its longest request.
LEASE_MARGIN_S = 90.0
#: A hash child gets ffmpeg's own timeout plus this before it is killed (ADR 0030 § 1).
HASH_DEADLINE_MARGIN_S = 30.0


@dataclass(frozen=True)
class LaneOptions:
    user_agent: str
    deadline_s: float
    slot_max_wait_s: float
    cdn_delay_s: float = 1.0
    hash_processes: int = 2
    #: Compute the second signal's sketch beside PDQ in the hash children (ADR 0037).
    embed: bool = False
    #: Where the pinned ONNX model is; None = the default cache location.
    model_path: str | None = None
    #: Tests only: a fake transport and resolver instead of the network.
    transport: httpx.BaseTransport | None = None
    resolver: Resolver = resolve_host


class Item(Protocol):
    """A claimed queue row (`CrawlTask`, `Media`): only its id matters to the runtime."""

    @property
    def pk(self) -> Any: ...


OnWait = Callable[[SlotWait], None]


class ThreadWorker(Protocol):
    """One thread's worker: its own client, so connections and cookies never cross threads."""

    def claim(self, domain: str) -> Item | None: ...

    def process(self, item: Item) -> str:
        """Process a claimed row; never raises for the row's own problems. Returns its status."""
        ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class LaneWork(Protocol):
    name: str
    lock_space: int
    needs_adapter: bool

    def thread_worker(self, stop: threading.Event, on_wait: OnWait) -> ThreadWorker: ...

    def maintenance(self, domains: Sequence[str]) -> None: ...

    def release(self, ids: Sequence[int]) -> int:
        """Put rows still RUNNING at shutdown back with their attempt refunded."""
        ...

    def close(self) -> None: ...


def _gate(lane: str, options: LaneOptions, stop: threading.Event, on_wait: OnWait) -> DbHostGate:
    def sleep(seconds: float) -> None:
        stop.wait(seconds)

    return DbHostGate(
        lane,
        lease_s=options.deadline_s + LEASE_MARGIN_S,
        max_wait_s=options.slot_max_wait_s,
        sleep=sleep,
        on_wait=on_wait,
        cancelled=stop.is_set,
    )


def _no_fetch(url: str, min_delay_s: float) -> FetchResponse:
    raise RuntimeError("the shared robots.txt cache fetches through each worker's client")


class _PagesThread:
    def __init__(
        self, options: LaneOptions, robots: RobotsCache, stop: threading.Event, on_wait: OnWait
    ) -> None:
        self._client = PoliteClient(
            options.user_agent,
            observer=RequestAccounting(),
            gate=_gate(PAGES, options, stop, on_wait),
            deadline_s=options.deadline_s,
            transport=options.transport,
        )
        self._worker = CrawlWorker(self._client, options.user_agent, robots=robots)

    def claim(self, domain: str) -> Item | None:
        return crawl_services.claim_task(site_domain=domain)

    def process(self, item: Item) -> str:
        assert isinstance(item, CrawlTask)
        self._worker.process_claimed(item)
        return str(item.status)

    def flush(self) -> None:
        self._client.flush()

    def close(self) -> None:
        self._client.close()


class PagesWork:
    """`--lane pages`: crawl tasks of every served site (legacy `crawl_worker`)."""

    name = PAGES
    lock_space = CRAWL_WORKER_LOCKS
    needs_adapter = True

    def __init__(self, options: LaneOptions) -> None:
        self._options = options
        self._robots = RobotsCache(_no_fetch, options.user_agent)

    def thread_worker(self, stop: threading.Event, on_wait: OnWait) -> ThreadWorker:
        return _PagesThread(self._options, self._robots, stop, on_wait)

    def maintenance(self, domains: Sequence[str]) -> None:
        crawl_services.recover_stale_tasks()
        for domain in domains:
            crawl_services.recover_expired_media(site_domain=domain)

    def release(self, ids: Sequence[int]) -> int:
        return CrawlTask.objects.filter(pk__in=list(ids), status=CrawlTask.Status.RUNNING).update(
            status=CrawlTask.Status.PENDING,
            attempts=Greatest(F("attempts") - 1, 0),
            locked_at=None,
            last_error=RELEASED,
            updated_at=timezone.now(),
        )

    def close(self) -> None:
        return None


class _MediaThread:
    def __init__(
        self,
        options: LaneOptions,
        robots: MediaRobots,
        pool: HashPool,
        stop: threading.Event,
        on_wait: OnWait,
    ) -> None:
        # Media URLs come from crawled pages: every hop must reach a public address only.
        self._client = PoliteClient(
            options.user_agent,
            max_bytes=CLIENT_MAX_BYTES,
            observer=RequestAccounting(),
            public_only=True,
            gate=_gate(MEDIA, options, stop, on_wait),
            deadline_s=options.deadline_s,
            transport=options.transport,
            resolver=options.resolver,
        )
        roles = HostRoles()
        cdn_delay_s = options.cdn_delay_s

        def delay_for(media: Media) -> float:
            return slot_media_delay_s(media, roles, cdn_delay_s=cdn_delay_s)

        self._worker = MediaWorker(
            self._client,
            user_agent=options.user_agent,
            robots=robots,
            hasher=pool.hash,
            delay_for=delay_for,
        )

    def claim(self, domain: str) -> Item | None:
        return media_queue.claim_media(site_domain=domain)

    def process(self, item: Item) -> str:
        assert isinstance(item, Media)
        self._worker.process_claimed(item)
        return str(item.status)

    def flush(self) -> None:
        self._client.flush()

    def close(self) -> None:
        self._client.close()


class MediaWork:
    """`--lane media`: fetch and hash pending media of every served site (legacy `media_worker`)."""

    name = MEDIA
    lock_space = MEDIA_WORKER_LOCKS
    needs_adapter = False

    def __init__(self, options: LaneOptions, pool: HashPool | None = None) -> None:
        self._options = options
        self._robots = MediaRobots(None, options.user_agent)
        self.pool = pool or HashPool(
            options.hash_processes,
            deadline_s=FFMPEG_TIMEOUT_S + HASH_DEADLINE_MARGIN_S,
            embed=options.embed,
            model_path=options.model_path,
        )

    def thread_worker(self, stop: threading.Event, on_wait: OnWait) -> ThreadWorker:
        return _MediaThread(self._options, self._robots, self.pool, stop, on_wait)

    def maintenance(self, domains: Sequence[str]) -> None:
        media_queue.recover_stale_media()

    def release(self, ids: Sequence[int]) -> int:
        return Media.objects.filter(pk__in=list(ids), status=Media.Status.RUNNING).update(
            status=Media.Status.PENDING,
            attempts=Greatest(F("attempts") - 1, 0),
            locked_at=None,
            last_error=RELEASED,
        )

    def close(self) -> None:
        self.pool.close()
