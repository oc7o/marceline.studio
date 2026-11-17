"""The lane loop: threads over a fair site rotation, per-site locks, maintenance, restarts.

`Runtime.run()` runs in the process's main thread (the dispatcher) and returns why it stopped:

- **Single instance per lane**: `core.locks.RUNTIME_LOCKS`; a second runtime for a lane refuses.
- **Coexistence with the legacy per-site workers**: for every site it serves the runtime holds that
  site's `CRAWL_WORKER_LOCKS` / `MEDIA_WORKER_LOCKS` lock on the dispatcher's connection, so an old
  `crawl_worker --site D` and the runtime never serve one site together, whoever starts first. A
  site whose lock is taken is skipped and retried every `lock_retry_s`; the held locks are checked
  against `pg_locks` just as often, and a site whose lock was lost is dropped until it is retaken.
- **Fairness**: `SiteRotation` — one task per site per lane at a time, round robin across sites
  with work; a site with nothing claimable (or whose claim failed) rests for `idle_s`.
- **Isolation**: a failure in one site's claim or processing is logged and counted for that site;
  the thread goes on with the next site. A database connection error closes that thread's
  connection so the next task reconnects. The *dispatcher's* connection holds every advisory
  lock, so when its connection goes the lane stops serving at once (`_locks_lost`) and re-takes
  what is free on the next tick — it must not keep crawling sites another worker may now hold.
- **Maintenance** once per lane every `maintenance_s` (stale rows, expired media links).
- **Graceful stop** on SIGTERM/SIGINT or a restart trigger — `max_tasks` processed, RSS above
  `max_rss_mb`, `max_uptime_s` reached, the applied migrations changed, or a task hung past
  `hung_after_s` (stacks are logged): stop handing out work, cancel slot waits, let in-flight tasks
  finish within `grace_s`, put rows still RUNNING back with their attempt refunded, release every
  lock. Compose (`restart: unless-stopped`) starts a fresh process, which runs the current code.
  **The task threads are daemon threads on purpose**: past `grace_s` the dispatcher has already
  handed their rows back and given up every lock, so the process must be free to exit. A
  non-daemon thread would be joined again by the interpreter at exit — a lane whose thread hangs
  for good (an uninterruptible `threading.Lock`, a socket without a timeout) would sit in a live
  container that serves nothing, holds nothing and is never restarted, because `restart:
  unless-stopped` only fires when the process really ends.
- **Watchdog**: `faulthandler.dump_traceback_later(watchdog_s, exit=True)` is re-armed every tick,
  so a dispatcher stuck behind a GIL-holding regex or a deadlock dumps every stack and exits.
- **Logging**: one INFO summary every `summary_s` (outcomes per site, slot waits per host, errors,
  RSS) — `docker compose logs crawler-pages` replaces reading one log per site.
"""

import faulthandler
import logging
import signal
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from django.db import InterfaceError, OperationalError, connection

from catalog.models import Site
from core.host_slots import SlotWait
from core.locks import (
    RUNTIME_LANE_KEYS,
    RUNTIME_LOCKS,
    advisory_unlock,
    held_advisory_locks,
    try_advisory_lock,
)
from crawler.sites.registry import get_adapter
from marceline.runtime.hash_pool import rss_mb
from marceline.runtime.lanes import LaneWork, ThreadWorker
from marceline.runtime.rotation import SiteRotation

logger = logging.getLogger(__name__)

STOP_SIGNAL = "signal"
STOP_ONCE = "queue drained"
TICK_S = 1.0


class LaneBusyError(Exception):
    """Another runtime already serves this lane."""


@dataclass(frozen=True)
class RuntimeConfig:
    threads: int
    #: Domains to serve; None serves every enabled site (with an adapter, for pages).
    sites: frozenset[str] | None
    idle_s: float = 10.0
    lock_retry_s: float = 60.0
    maintenance_s: float = 600.0
    summary_s: float = 300.0
    migration_check_s: float = 60.0
    grace_s: float = 80.0
    hung_after_s: float = 600.0
    max_tasks: int = 20_000
    max_rss_mb: float = 500.0
    max_uptime_s: float = 24 * 3600.0
    watchdog_s: float | None = 180.0
    #: Stop once every served site has nothing to claim (debugging and tests).
    once: bool = False
    install_signals: bool = True


@dataclass
class _Heartbeat:
    domain: str
    item_id: int
    started: float


@dataclass
class LaneStats:
    """Counters since the last summary (and totals since start), shared by the lane's threads."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    processed_total: int = 0
    outcomes: Counter[tuple[str, str]] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    waits: defaultdict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def outcome(self, domain: str, status: str) -> None:
        with self.lock:
            self.processed_total += 1
            self.outcomes[(domain, status)] += 1

    def error(self, domain: str) -> None:
        with self.lock:
            self.errors[domain] += 1

    def wait(self, event: SlotWait) -> None:
        with self.lock:
            samples = self.waits[event.key]
            if len(samples) < 10_000:
                samples.append(event.waited_s)

    def take_summary(self) -> str:
        with self.lock:
            outcomes, errors, waits = self.outcomes, self.errors, self.waits
            self.outcomes, self.errors = Counter(), Counter()
            self.waits = defaultdict(list)
        return summary_line(outcomes, errors, waits)


def summary_line(
    outcomes: Counter[tuple[str, str]], errors: Counter[str], waits: dict[str, list[float]]
) -> str:
    """`sites: a.com done=3 skipped=1 | …; slots: host n=4 p50=1.0s max=3.1s | …; errors: …`."""
    by_site: defaultdict[str, list[str]] = defaultdict(list)
    for (domain, status), count in sorted(outcomes.items()):
        by_site[domain].append(f"{status}={count}")
    sites = " | ".join(f"{domain} {' '.join(parts)}" for domain, parts in by_site.items())
    slots = []
    for key, samples in sorted(waits.items(), key=lambda item: -max(item[1], default=0.0))[:8]:
        ordered = sorted(samples)
        p50 = ordered[len(ordered) // 2] if ordered else 0.0
        slots.append(f"{key} n={len(ordered)} p50={p50:.1f}s max={ordered[-1]:.1f}s")
    failed = " | ".join(f"{domain}={count}" for domain, count in sorted(errors.items()))
    return f"sites: {sites or '-'}; slots: {' | '.join(slots) or '-'}; errors: {failed or '-'}"


class Runtime:
    def __init__(
        self,
        work: LaneWork,
        config: RuntimeConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.work = work
        self.config = config
        self.stats = LaneStats()
        self.rotation = SiteRotation(clock)
        self.stop_event = threading.Event()
        self.stop_reason = ""
        self._clock = clock
        self._threads: list[threading.Thread] = []
        self._heartbeats: dict[int, _Heartbeat] = {}
        self._heartbeat_lock = threading.Lock()
        self._held: dict[int, str] = {}  # site id -> domain, locks on the dispatcher connection
        self._skipped: set[str] = set()
        self._previous_signals: dict[int, Any] = {}
        #: Re-take the locks on the next tick instead of waiting for `lock_retry_s`.
        self._resync = False

    # --- dispatcher -----------------------------------------------------------------------------

    def run(self) -> str:
        lane_key = RUNTIME_LANE_KEYS[self.work.name]
        if not try_advisory_lock(RUNTIME_LOCKS, lane_key):
            raise LaneBusyError(f"another crawl runtime already serves the {self.work.name} lane")
        started = self._clock()
        migration = self._latest_migration()
        if self.config.install_signals:
            self._install_signals()
        try:
            self._sync_sites()
            self.work.maintenance(self.rotation.sites())
            logger.info(
                "%s lane started: %d thread(s), serving %s",
                self.work.name,
                self.config.threads,
                ", ".join(self.rotation.sites()) or "no site yet",
            )
            for index in range(self.config.threads):
                thread = threading.Thread(
                    target=self._thread_main,
                    args=(index,),
                    name=f"{self.work.name}-{index}",
                    # Abandoned past `grace_s` (see the module docstring): the interpreter must
                    # not join it again at exit, or the lane could never be restarted.
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            now = self._clock()
            next_locks = now + self.config.lock_retry_s
            next_maintenance = now + self.config.maintenance_s
            next_summary = now + self.config.summary_s
            next_migration = now + self.config.migration_check_s
            while not self.stop_event.is_set():
                self._arm_watchdog()
                self.stop_event.wait(TICK_S)
                if self.stop_event.is_set():
                    break
                now = self._clock()
                try:
                    if now >= next_locks or self._resync:
                        next_locks = now + self.config.lock_retry_s
                        self._resync = False
                        self._sync_sites()
                    if now >= next_maintenance:
                        next_maintenance = now + self.config.maintenance_s
                        self.work.maintenance(self.rotation.sites())
                    if now >= next_summary:
                        next_summary = now + self.config.summary_s
                        self._log_summary()
                    if now >= next_migration:
                        next_migration = now + self.config.migration_check_s
                        if self._latest_migration() != migration:
                            self.stop("applied migrations changed")
                except (InterfaceError, OperationalError):
                    logger.exception("%s lane: dispatcher database error", self.work.name)
                    connection.close()
                    self._locks_lost()
                except Exception:
                    logger.exception("%s lane: dispatcher step failed", self.work.name)
                self._check_restart(started, now)
        finally:
            self.stop(self.stop_reason or "stopped by an error")
            self._shutdown()
            self._disarm_watchdog()
            self._unlock_sites()
            advisory_unlock(RUNTIME_LOCKS, lane_key)
            self._restore_signals()
        return self.stop_reason

    def stop(self, reason: str) -> None:
        if not self.stop_event.is_set():
            self.stop_reason = reason
            logger.info("%s lane stopping: %s", self.work.name, reason)
        self.stop_event.set()

    def _check_restart(self, started: float, now: float) -> None:
        config = self.config
        if config.once and self.rotation.all_resting():
            self.stop(STOP_ONCE)
        elif self.stats.processed_total >= config.max_tasks:
            self.stop(f"processed {self.stats.processed_total} tasks")
        elif (rss := rss_mb()) > config.max_rss_mb:
            self.stop(f"RSS {rss:.0f} MiB above {config.max_rss_mb:.0f} MiB")
        elif now - started >= config.max_uptime_s:
            self.stop(f"up for {(now - started) / 3600:.1f} h")
        elif hung := self._hung_threads(now):
            self._log_stacks(hung)
            self.stop(f"task hung: {hung[0]}")

    def _hung_threads(self, now: float) -> list[str]:
        with self._heartbeat_lock:
            return [
                f"{beat.domain} item {beat.item_id} for {now - beat.started:.0f} s"
                for beat in self._heartbeats.values()
                if now - beat.started > self.config.hung_after_s
            ]

    def _log_stacks(self, hung: Sequence[str]) -> None:
        frames = sys._current_frames()
        names = {thread.ident: thread.name for thread in self._threads}
        for ident, frame in frames.items():
            if ident in names:
                stack = "".join(traceback.format_stack(frame))
                logger.error(
                    "%s lane: hung %s; stack of %s:\n%s", self.work.name, hung, names[ident], stack
                )

    def _log_summary(self) -> None:
        logger.info(
            "%s lane: %s; busy %d/%d; rss %.0f MiB; total %d",
            self.work.name,
            self.stats.take_summary(),
            self.rotation.busy_count(),
            self.config.threads,
            rss_mb(),
            self.stats.processed_total,
        )

    # --- sites and their locks ------------------------------------------------------------------

    def eligible_sites(self) -> dict[int, str]:
        rows = Site.objects.filter(enabled=True).order_by("domain").values_list("pk", "domain")
        eligible = {}
        for site_id, domain in rows:
            if self.config.sites is not None and domain not in self.config.sites:
                continue
            if self.work.needs_adapter and get_adapter(domain) is None:
                continue
            eligible[site_id] = domain
        return eligible

    def _sync_sites(self) -> None:
        """Verify held locks, take locks of newly eligible sites, give up ineligible ones."""
        space = self.work.lock_space
        lane_key = RUNTIME_LANE_KEYS[self.work.name]
        if lane_key not in held_advisory_locks(RUNTIME_LOCKS) and not try_advisory_lock(
            RUNTIME_LOCKS, lane_key
        ):
            self.stop("lost the lane lock to another runtime")
            return
        if self._held:
            actual = held_advisory_locks(space)
            for site_id in [site_id for site_id in self._held if site_id not in actual]:
                logger.error(
                    "%s lane: lost the lock of %s; not serving it until it is retaken",
                    self.work.name,
                    self._held.pop(site_id),
                )
        eligible = self.eligible_sites()
        for site_id, domain in list(self._held.items()):
            if site_id not in eligible and not self.rotation.is_busy(domain):
                advisory_unlock(space, site_id)
                del self._held[site_id]
                logger.info("%s lane: no longer serving %s", self.work.name, domain)
        for site_id, domain in eligible.items():
            if site_id in self._held:
                continue
            if try_advisory_lock(space, site_id):
                self._held[site_id] = domain
                self._skipped.discard(domain)
                logger.info("%s lane: serving %s", self.work.name, domain)
            elif domain not in self._skipped:
                self._skipped.add(domain)
                logger.warning(
                    "%s lane: skipping %s, another worker holds its lock (retry every %.0f s)",
                    self.work.name,
                    domain,
                    self.config.lock_retry_s,
                )
        self.rotation.set_sites(
            domain for site_id, domain in self._held.items() if site_id in eligible
        )

    def _locks_lost(self) -> None:
        """The dispatcher's connection went, so every advisory lock on it went with it.

        Stop serving at once rather than at the next `lock_retry_s`: while the lock is gone a
        legacy `crawl_worker --site D` can take it and crawl the same site alongside this lane's
        threads — one worker per site is the invariant those locks exist for, and a whole minute
        of two crawlers is a minute of double requests to that site's server. The rotation is
        emptied (the threads go idle within a tick), and the next tick re-takes what is free.
        """
        if self._held:
            logger.error(
                "%s lane: lost the lane lock and %d site lock(s) with the connection; "
                "not serving anything until they are re-taken",
                self.work.name,
                len(self._held),
            )
        self._held.clear()
        self._skipped.clear()
        self.rotation.set_sites(())
        self._resync = True

    def _unlock_sites(self) -> None:
        for site_id in list(self._held):
            try:
                advisory_unlock(self.work.lock_space, site_id)
            except Exception:
                logger.exception("%s lane: could not unlock site %s", self.work.name, site_id)
        self._held.clear()

    # --- threads --------------------------------------------------------------------------------

    def _thread_main(self, index: int) -> None:
        worker: ThreadWorker | None = None
        try:
            worker = self.work.thread_worker(self.stop_event, self.stats.wait)
            while not self.stop_event.is_set():
                domain = self.rotation.take()
                if domain is None:
                    worker.flush()
                    self.stop_event.wait(min(max(self.rotation.ready_in(), 0.05), TICK_S))
                    continue
                self.rotation.give_back(domain, rest_s=self._serve(index, worker, domain))
        except Exception:
            logger.exception("%s lane: thread %d stopped", self.work.name, index)
            self.stop(f"thread {index} failed")
        finally:
            if worker is not None:
                try:
                    worker.close()
                except Exception:
                    logger.exception("%s lane: closing thread %d's client", self.work.name, index)
            connection.close()

    def _serve(self, index: int, worker: ThreadWorker, domain: str) -> float:
        """One claim and one task for `domain`; returns how long the site should rest."""
        try:
            item = worker.claim(domain)
            if item is None:
                return self.config.idle_s
            with self._heartbeat_lock:
                self._heartbeats[index] = _Heartbeat(domain, int(item.pk), self._clock())
            try:
                status = worker.process(item)
            finally:
                with self._heartbeat_lock:
                    self._heartbeats.pop(index, None)
            self.stats.outcome(domain, status)
            return 0.0
        except Exception as exc:
            logger.exception("%s lane: %s failed", self.work.name, domain)
            self.stats.error(domain)
            if isinstance(exc, InterfaceError | OperationalError):
                connection.close()
            return self.config.idle_s

    def _shutdown(self) -> None:
        deadline = self._clock() + self.config.grace_s
        for thread in self._threads:
            thread.join(max(deadline - self._clock(), 0.0))
        with self._heartbeat_lock:
            stuck = [beat.item_id for beat in self._heartbeats.values()]
        if stuck:
            try:
                released = self.work.release(stuck)
                logger.warning(
                    "%s lane: %d task(s) did not finish in time, %d released",
                    self.work.name,
                    len(stuck),
                    released,
                )
            except Exception:
                logger.exception("%s lane: could not release unfinished tasks", self.work.name)
        try:
            self._log_summary()
        except Exception:
            logger.exception("%s lane: final summary failed", self.work.name)
        try:
            self.work.close()
        except Exception:
            logger.exception("%s lane: closing failed", self.work.name)

    # --- process plumbing -----------------------------------------------------------------------

    def _install_signals(self) -> None:
        def handle(signum: int, frame: FrameType | None) -> None:
            self.stop(STOP_SIGNAL)

        for signum in (signal.SIGTERM, signal.SIGINT):
            self._previous_signals[signum] = signal.signal(signum, handle)

    def _restore_signals(self) -> None:
        for signum, previous in self._previous_signals.items():
            signal.signal(signum, previous)
        self._previous_signals.clear()

    def _arm_watchdog(self) -> None:
        if self.config.watchdog_s:
            faulthandler.dump_traceback_later(self.config.watchdog_s, exit=True)

    def _disarm_watchdog(self) -> None:
        if self.config.watchdog_s:
            faulthandler.cancel_dump_traceback_later()

    @staticmethod
    def _latest_migration() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT coalesce(max(id), 0) FROM django_migrations")
            row = cursor.fetchone()
        return int(row[0]) if row else 0
