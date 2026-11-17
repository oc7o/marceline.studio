"""System resources block for the staff overview (issue #43): whether this development VPS is
healthy, cheap enough to compute on every 60 s poll.

Host figures come from `/proc` and `shutil.disk_usage`, never from the docker socket — this
process never shells out to docker and never mounts it. Database figures are catalog lookups
(`pg_database_size`, `pg_class.reltuples`) or reads of small system views (`pg_locks`,
`pg_stat_activity`), never a table scan. A host figure that cannot be read from inside this
container (no `/proc/loadavg`, no `/proc/meminfo`) is left out rather than guessed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone
from ninja import Schema

from catalog.models import Site
from core.locks import (
    CRAWL_WORKER_LOCKS,
    MEDIA_WORKER_LOCKS,
    RUNTIME_LANE_KEYS,
    RUNTIME_LOCKS,
)
from core.models import JobRun
from crawler.models import CrawlTask
from crawler.services import STALE_LOCK_AFTER

TOP_TABLES_LIMIT = 8


# --- schemas -------------------------------------------------------------------------------


class MemoryOut(Schema):
    total_bytes: int
    available_bytes: int
    used_bytes: int


class DiskOut(Schema):
    total_bytes: int
    used_bytes: int
    free_bytes: int


class HostOut(Schema):
    cpu_count: int | None
    load_average_1m: float | None
    load_average_5m: float | None
    load_average_15m: float | None
    memory: MemoryOut | None
    disk: DiskOut | None


class TableSizeOut(Schema):
    name: str
    total_bytes: int
    approx_rows: int


class DatabaseOut(Schema):
    size_bytes: int
    tables: list[TableSizeOut]


class PausedSiteOut(Schema):
    domain: str
    pending: int
    paused_until: datetime
    pause_reason: str


class QueueOut(Schema):
    pending_total: int
    oldest_pending_seconds: float | None
    stuck_running: int
    paused_sites: list[PausedSiteOut]


class WorkerSiteOut(Schema):
    domain: str
    crawler_alive: bool
    media_alive: bool


class RuntimeLaneOut(Schema):
    lane: str
    alive: bool
    since: datetime | None
    sites: list[str]


class SchedulerJobOut(Schema):
    name: str
    last_status: str
    last_finished_at: datetime | None
    last_duration_s: float | None


class ResourcesOut(Schema):
    generated_at: datetime
    host: HostOut
    database: DatabaseOut
    queue: QueueOut
    workers: list[WorkerSiteOut]
    runtime_lanes: list[RuntimeLaneOut]
    scheduler: list[SchedulerJobOut]


# --- host: /proc and the filesystem --------------------------------------------------------


def _parse_meminfo(text: str) -> tuple[int, int] | None:
    """`(total_bytes, available_bytes)` from `/proc/meminfo` text, or None if either is absent."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if not rest.endswith("kB"):
            continue
        try:
            values[key] = int(rest[:-2].strip()) * 1024
        except ValueError:
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    return total, available


def memory_stats() -> MemoryOut | None:
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    parsed = _parse_meminfo(text)
    if parsed is None:
        return None
    total, available = parsed
    return MemoryOut(total_bytes=total, available_bytes=available, used_bytes=total - available)


def disk_stats(path: Path | str) -> DiskOut | None:
    """Usage of the filesystem carrying `path` — the backend code volume, which on this compose
    setup is bind-mounted from the host, so it reports the host disk (docs/research/system-
    resources.md). Never a docker volume we can't see from in here."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return DiskOut(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free)


def load_average() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except OSError:
        return None


def host_stats() -> HostOut:
    load = load_average()
    return HostOut(
        cpu_count=os.cpu_count(),
        load_average_1m=load[0] if load else None,
        load_average_5m=load[1] if load else None,
        load_average_15m=load[2] if load else None,
        memory=memory_stats(),
        disk=disk_stats(settings.BASE_DIR),
    )


# --- database: catalog lookups, never a scan -----------------------------------------------


def database_stats() -> DatabaseOut:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        row = cursor.fetchone()
        size_bytes = int(row[0]) if row else 0
        cursor.execute(
            """
            SELECT c.relname, pg_total_relation_size(c.oid), c.reltuples
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = 'public'
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT %s
            """,
            [TOP_TABLES_LIMIT],
        )
        tables = [
            TableSizeOut(name=name, total_bytes=int(total), approx_rows=max(0, int(rows)))
            for name, total, rows in cursor.fetchall()
        ]
    return DatabaseOut(size_bytes=size_bytes, tables=tables)


# --- crawl queue depth -----------------------------------------------------------------------


def queue_stats(now: datetime) -> QueueOut:
    pending = CrawlTask.objects.filter(status=CrawlTask.Status.PENDING)
    pending_total = pending.count()
    oldest = pending.order_by("created_at").values_list("created_at", flat=True).first()
    oldest_pending_seconds = (now - oldest).total_seconds() if oldest else None
    stuck_running = CrawlTask.objects.filter(
        status=CrawlTask.Status.RUNNING, locked_at__lt=now - STALE_LOCK_AFTER
    ).count()
    paused_sites: list[PausedSiteOut] = []
    for site in Site.objects.filter(paused_until__gt=now).order_by("domain"):
        if site.paused_until is None:
            continue
        paused_sites.append(
            PausedSiteOut(
                domain=site.domain,
                pending=site.crawl_tasks.filter(status=CrawlTask.Status.PENDING).count(),
                paused_until=site.paused_until,
                pause_reason=site.pause_reason,
            )
        )
    return QueueOut(
        pending_total=pending_total,
        oldest_pending_seconds=oldest_pending_seconds,
        stuck_running=stuck_running,
        paused_sites=paused_sites,
    )


# --- worker and runtime liveness: session advisory locks (`core/locks.py`), never docker -----


@dataclass(frozen=True)
class _LockHolder:
    classid: int
    objid: int
    started_at: datetime | None


def _advisory_lock_holders(classids: tuple[int, ...]) -> list[_LockHolder]:
    """Session-level advisory-lock holders in `classids`, restricted to *this* database.

    `pg_locks` is cluster-wide: on a shared Postgres instance (several checks, or several sites'
    worth of state on one server) it also lists advisory locks held by connections to *other*
    databases. `core.locks` always takes the two-integer form (`pg_try_advisory_lock(space, id)`),
    which Postgres tags with the caller's database at acquisition time — so a lock taken from
    another database is a different lock, never a conflict, but it still shows up in this view.
    Without the `l.database` filter below, one database's worker or runtime lock would make
    another database's site or lane look alive (issue #43 follow-up, found by a parallel
    `make remote-check` run on the shared check-runner Postgres).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT l.classid, l.objid, a.backend_start
            FROM pg_locks l
            LEFT JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.locktype = 'advisory' AND l.granted AND l.classid = ANY(%s)
              AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
            """,
            [list(classids)],
        )
        return [
            _LockHolder(classid=int(classid), objid=int(objid), started_at=started_at)
            for classid, objid, started_at in cursor.fetchall()
        ]


def worker_stats() -> list[WorkerSiteOut]:
    holders = _advisory_lock_holders((CRAWL_WORKER_LOCKS, MEDIA_WORKER_LOCKS))
    crawler_sites = {h.objid for h in holders if h.classid == CRAWL_WORKER_LOCKS}
    media_sites = {h.objid for h in holders if h.classid == MEDIA_WORKER_LOCKS}
    return [
        WorkerSiteOut(
            domain=site.domain,
            crawler_alive=site.pk in crawler_sites,
            media_alive=site.pk in media_sites,
        )
        for site in Site.objects.filter(enabled=True).order_by("domain")
    ]


def runtime_lane_stats() -> list[RuntimeLaneOut]:
    holders = {h.objid: h for h in _advisory_lock_holders((RUNTIME_LOCKS,))}
    sites = list(settings.RUNTIME_SITES)
    return [
        RuntimeLaneOut(
            lane=lane,
            alive=key in holders,
            since=holders[key].started_at if key in holders else None,
            sites=sites,
        )
        for lane, key in sorted(RUNTIME_LANE_KEYS.items())
    ]


# --- scheduler: the same `JobRun` rows the overview's Scheduler table reads -------------------


def scheduler_stats() -> list[SchedulerJobOut]:
    jobs = []
    for job in JobRun.objects.order_by("name"):
        duration = None
        if (
            job.last_started_at
            and job.last_finished_at
            and job.last_finished_at >= job.last_started_at
        ):
            duration = (job.last_finished_at - job.last_started_at).total_seconds()
        jobs.append(
            SchedulerJobOut(
                name=job.name,
                last_status=job.last_status,
                last_finished_at=job.last_finished_at,
                last_duration_s=duration,
            )
        )
    return jobs


def resources_snapshot(now: datetime | None = None) -> ResourcesOut:
    now = now or timezone.now()
    return ResourcesOut(
        generated_at=now,
        host=host_stats(),
        database=database_stats(),
        queue=queue_stats(now),
        workers=worker_stats(),
        runtime_lanes=runtime_lane_stats(),
        scheduler=scheduler_stats(),
    )
