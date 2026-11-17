"""System resources block on the staff overview (issue #43)."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from catalog.models import Site
from core.locks import CRAWL_WORKER_LOCKS, MEDIA_WORKER_LOCKS, RUNTIME_LOCKS
from core.models import JobRun
from crawler.models import CrawlTask
from crawler.services import STALE_LOCK_AFTER
from marceline.review.resources import (
    _advisory_lock_holders,
    _parse_meminfo,
    disk_stats,
    queue_stats,
    resources_snapshot,
    runtime_lane_stats,
    scheduler_stats,
    worker_stats,
)

OVERVIEW = "/api/v1/review/overview"


def _advisory_lock(space: int, key: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [space, key])


def _advisory_unlock(space: int, key: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [space, key])


@pytest.fixture
def other_database_connection() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A second connection to the `postgres` maintenance database on the *same* cluster —
    genuinely another database, not another transaction on this one — so a test can prove an
    advisory lock held there is invisible to `_advisory_lock_holders` reading from this database
    (issue #43 follow-up: `pg_locks` is cluster-wide, but `core.locks` locks are tagged with the
    caller's database at acquisition time, so they must be filtered on it). Every official
    Postgres image (dev compose, the check-runner) ships a `postgres` database by default; skip
    rather than fail if this cluster's credentials cannot reach it."""
    params = connection.settings_dict
    try:
        other = psycopg.connect(
            host=params["HOST"] or "localhost",
            port=params["PORT"] or 5432,
            dbname="postgres",
            user=params["USER"],
            password=params["PASSWORD"],
            autocommit=True,
        )
    except psycopg.OperationalError as exc:
        pytest.skip(f"no 'postgres' maintenance database reachable on this cluster: {exc}")
    try:
        yield other
    finally:
        other.close()


# --- pure host parsing -----------------------------------------------------------------------


def test_parse_meminfo_reads_total_and_available() -> None:
    text = "MemTotal:        8130000 kB\nMemFree:          100000 kB\nMemAvailable:    4300000 kB\n"
    assert _parse_meminfo(text) == (8130000 * 1024, 4300000 * 1024)


def test_parse_meminfo_missing_field_is_none() -> None:
    assert _parse_meminfo("MemTotal:        8130000 kB\n") is None


def test_parse_meminfo_garbage_is_none() -> None:
    assert _parse_meminfo("not meminfo at all") is None


def test_disk_stats_reads_the_real_filesystem(tmp_path: Path) -> None:
    stats = disk_stats(tmp_path)
    assert stats is not None
    assert stats.total_bytes > 0
    assert stats.free_bytes <= stats.total_bytes


def test_disk_stats_missing_path_is_none() -> None:
    assert disk_stats("/no/such/path/at/all") is None


# --- database size and tables ----------------------------------------------------------------


@pytest.mark.django_db
def test_resources_snapshot_reports_database_size() -> None:
    snapshot = resources_snapshot(timezone.now())
    assert snapshot.database.size_bytes > 0
    assert len(snapshot.database.tables) > 0
    assert all(table.total_bytes >= 0 for table in snapshot.database.tables)


# --- crawl queue depth ------------------------------------------------------------------------


@pytest.mark.django_db
def test_queue_stats_pending_and_oldest(site: Site) -> None:
    now = timezone.now()
    old = CrawlTask.objects.create(
        site=site, url="https://example.test/old", kind="detail", status="pending"
    )
    CrawlTask.objects.filter(pk=old.pk).update(created_at=now - timedelta(hours=3))
    CrawlTask.objects.create(
        site=site, url="https://example.test/new", kind="detail", status="pending"
    )

    stats = queue_stats(now)

    assert stats.pending_total == 2
    assert stats.oldest_pending_seconds is not None
    assert stats.oldest_pending_seconds >= timedelta(hours=3).total_seconds() - 5


@pytest.mark.django_db
def test_queue_stats_counts_stuck_running_tasks(site: Site) -> None:
    now = timezone.now()
    stuck = CrawlTask.objects.create(
        site=site, url="https://example.test/stuck", kind="detail", status="running"
    )
    CrawlTask.objects.filter(pk=stuck.pk).update(
        locked_at=now - STALE_LOCK_AFTER - timedelta(minutes=1)
    )
    CrawlTask.objects.create(
        site=site, url="https://example.test/fresh", kind="detail", status="running", locked_at=now
    )

    assert queue_stats(now).stuck_running == 1


@pytest.mark.django_db
def test_queue_stats_lists_paused_sites_with_their_pending_count(site: Site) -> None:
    now = timezone.now()
    site.paused_until = now + timedelta(minutes=30)
    site.pause_reason = "robots.txt unreadable"
    site.save()
    CrawlTask.objects.create(
        site=site, url="https://example.test/waiting", kind="detail", status="pending"
    )
    other = Site.objects.create(domain="other.test")
    other.paused_until = now - timedelta(minutes=1)  # pause already expired
    other.save()

    stats = queue_stats(now)

    assert [paused.domain for paused in stats.paused_sites] == [site.domain]
    assert stats.paused_sites[0].pending == 1
    assert stats.paused_sites[0].pause_reason == "robots.txt unreadable"


# --- worker and runtime liveness (advisory locks) ---------------------------------------------


@pytest.mark.django_db
def test_worker_stats_reflects_a_held_lock(site: Site) -> None:
    before = {w.domain: w for w in worker_stats()}
    assert before[site.domain].crawler_alive is False
    assert before[site.domain].media_alive is False

    _advisory_lock(CRAWL_WORKER_LOCKS, site.pk)
    try:
        stats = {w.domain: w for w in worker_stats()}
        assert stats[site.domain].crawler_alive is True
        assert stats[site.domain].media_alive is False
    finally:
        _advisory_unlock(CRAWL_WORKER_LOCKS, site.pk)

    stats = {w.domain: w for w in worker_stats()}
    assert stats[site.domain].crawler_alive is False


@pytest.mark.django_db
def test_worker_stats_only_lists_enabled_sites(site: Site) -> None:
    Site.objects.create(domain="disabled.test", enabled=False)
    assert "disabled.test" not in {w.domain for w in worker_stats()}


@pytest.mark.django_db
def test_runtime_lane_stats_reflects_a_held_lock() -> None:
    _advisory_lock(RUNTIME_LOCKS, 1)  # pages
    try:
        lanes = {lane.lane: lane for lane in runtime_lane_stats()}
        assert lanes["pages"].alive is True
        assert lanes["media"].alive is False
        assert lanes["pages"].since is not None
    finally:
        _advisory_unlock(RUNTIME_LOCKS, 1)

    lanes = {lane.lane: lane for lane in runtime_lane_stats()}
    assert lanes["pages"].alive is False


@pytest.mark.django_db
def test_runtime_lane_stats_ignores_a_lock_held_in_another_database(
    other_database_connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    """The bug this guards: `pg_locks` lists every database's advisory locks, so a lock taken
    from a *different* database on the same cluster must not make this database's runtime lane
    (or, by the same query, a per-site worker) look alive."""
    with other_database_connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [RUNTIME_LOCKS, 1])  # pages, elsewhere
    try:
        lanes = {lane.lane: lane for lane in runtime_lane_stats()}
        assert lanes["pages"].alive is False
        assert lanes["pages"].since is None
    finally:
        with other_database_connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [RUNTIME_LOCKS, 1])


@pytest.mark.django_db
def test_advisory_lock_holders_ignores_other_lock_spaces(site: Site) -> None:
    _advisory_lock(MEDIA_WORKER_LOCKS, site.pk)
    try:
        holders = _advisory_lock_holders((CRAWL_WORKER_LOCKS,))
        assert all(h.objid != site.pk for h in holders)
    finally:
        _advisory_unlock(MEDIA_WORKER_LOCKS, site.pk)


# --- scheduler job durations -------------------------------------------------------------------


@pytest.mark.django_db
def test_scheduler_stats_computes_duration() -> None:
    now = timezone.now()
    JobRun.objects.create(
        name="match_references",
        last_started_at=now - timedelta(minutes=5),
        last_finished_at=now,
        last_status="ok",
    )
    JobRun.objects.create(name="never_run")

    stats = {job.name: job for job in scheduler_stats()}

    assert stats["match_references"].last_duration_s == pytest.approx(300, abs=1)
    assert stats["never_run"].last_duration_s is None


# --- wired into the overview endpoint -----------------------------------------------------------


def test_requires_staff(client: Client, user_client: Client) -> None:
    assert client.get(OVERVIEW).status_code == 401
    assert user_client.get(OVERVIEW).status_code == 403


def test_overview_carries_the_resources_block(staff_client: Client, site: Site) -> None:
    response = staff_client.get(OVERVIEW)

    assert response.status_code == 200
    resources = response.json()["resources"]
    assert resources["database"]["size_bytes"] > 0
    assert "host" in resources
    assert "queue" in resources
    assert "workers" in resources
    assert "runtime_lanes" in resources
    assert "scheduler" in resources
