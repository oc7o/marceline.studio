import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core import mail as django_mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.utils import timezone

from catalog.models import Creator, RequestCountDaily, Site, SiteHost
from catalog.request_accounting import upsert_counts
from core.models import JobRun
from crawler import adapter_health
from crawler.models import AdapterHealth, CrawlTask
from crawler.services import RECHECK_PRIORITY
from index.models import FrameHash, Media, Page
from marceline import scheduler
from marceline.scheduler import Job, JobContext
from matching.capture_triage import GateSignals
from matching.copy_extent import CLASSIFIER_VERSION
from matching.models import (
    BrowserCaptureCandidate,
    Cluster,
    ClusterMember,
    CommonFrameGroup,
    ConfidenceSettings,
    Reference,
    ReferenceLeak,
    ReferenceMatch,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def recording_job(
    name: str,
    calls: list[str],
    *,
    hours: int = 3,
    fail: bool = False,
    guard: Callable[[JobContext], str | None] | None = None,
) -> Job:
    def run(context: JobContext) -> str:
        calls.append(name)
        if fail:
            raise RuntimeError("boom")
        return f"{name} done"

    return Job(name, hours, run, guard=guard)


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Replace call_command inside the scheduler; record calls and write a fake summary."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call_command(name: str, *args: Any, stdout: Any, stderr: Any, **options: Any) -> None:
        calls.append((name, options))
        prefix = f"{options['site']}: " if "site" in options else ""
        stdout.write(f"{prefix}{name} output\nTotal: 1 page\n")

    monkeypatch.setattr(scheduler, "call_command", fake_call_command)
    return calls


def frame_hash(site: Site, slug: str) -> FrameHash:
    page = Page.objects.create(site=site, url=f"https://{site.domain}/{slug}", title=slug)
    media = Media.objects.create(page=page, kind="frame", url=f"https://{site.domain}/{slug}.png")
    return FrameHash.objects.create(media=media, pdq=bytes(32), quality=100)


def reference(slug: str) -> None:
    creator, _ = Creator.objects.get_or_create(name="Creator")
    creator.references.create(title=slug, sha256=slug.ljust(64, "0"), frame_fps=1)


def test_job_is_due_without_history_then_after_its_interval(db: None) -> None:
    calls: list[str] = []
    jobs = [recording_job("alpha", calls, hours=24)]
    clock = FakeClock()

    scheduler.run_due_jobs(jobs, clock)
    clock.advance(hours=23, minutes=59)
    scheduler.run_due_jobs(jobs, clock)
    assert calls == ["alpha"]

    clock.advance(minutes=1)
    scheduler.run_due_jobs(jobs, clock)
    assert calls == ["alpha", "alpha"]


def test_env_override_changes_interval(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    jobs = [recording_job("alpha", calls, hours=24)]
    clock = FakeClock()
    monkeypatch.setenv("SCHEDULER_ALPHA_HOURS", "1")

    scheduler.run_due_jobs(jobs, clock)
    clock.advance(hours=1)
    scheduler.run_due_jobs(jobs, clock)

    assert calls == ["alpha", "alpha"]
    assert jobs[0].interval() == timedelta(hours=1)
    monkeypatch.setenv("SCHEDULER_ALPHA_HOURS", "0")
    with pytest.raises(ValueError, match="SCHEDULER_ALPHA_HOURS"):
        jobs[0].interval()


def test_job_run_state_persists(db: None) -> None:
    calls: list[str] = []
    clock = FakeClock()

    scheduler.run_due_jobs([recording_job("alpha", calls)], clock)

    record = JobRun.objects.get(name="alpha")
    assert record.last_started_at == T0
    assert record.last_finished_at == T0
    assert record.last_succeeded_at == T0
    assert record.last_status == JobRun.Status.OK
    assert record.last_message == "alpha done"

    # A fresh job list (as after a restart) sees the persisted run and does not repeat it.
    clock.advance(hours=1)
    scheduler.run_due_jobs([recording_job("alpha", calls)], clock)
    assert calls == ["alpha"]


def test_failing_job_does_not_stop_others_and_retries_next_interval(
    db: None, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    jobs = [recording_job("alpha", calls, fail=True), recording_job("beta", calls)]
    clock = FakeClock()
    caplog.set_level(logging.INFO, logger="marceline.scheduler")

    scheduler.run_due_jobs(jobs, clock)

    assert calls == ["alpha", "beta"]
    failed = JobRun.objects.get(name="alpha")
    assert failed.last_status == JobRun.Status.FAILED
    assert failed.last_message == "RuntimeError: boom"
    assert failed.last_succeeded_at is None
    assert JobRun.objects.get(name="beta").last_status == JobRun.Status.OK
    assert "alpha: failed" in caplog.text
    assert "beta: finished: beta done" in caplog.text

    clock.advance(hours=1)
    scheduler.run_due_jobs(jobs, clock)
    assert calls == ["alpha", "beta"]
    clock.advance(hours=2)
    scheduler.run_due_jobs(jobs, clock)
    assert calls == ["alpha", "beta", "alpha", "beta"]


def test_guard_skip_is_recorded_without_running(db: None, caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []
    caplog.set_level(logging.INFO, logger="marceline.scheduler")

    scheduler.run_due_jobs(
        [recording_job("alpha", calls, guard=lambda context: "nothing to do")], FakeClock()
    )

    record = JobRun.objects.get(name="alpha")
    assert calls == []
    assert record.last_status == JobRun.Status.SKIPPED
    assert record.last_message == "nothing to do"
    assert record.last_succeeded_at is None
    assert "alpha: skipped (nothing to do)" in caplog.text


def test_crawl_jobs_skip_sites_with_a_backlog(
    db: None,
    commands: list[tuple[str, dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    busy = Site.objects.create(domain="busy.test")
    Site.objects.create(domain="idle.test")
    Site.objects.create(domain="disabled.test", enabled=False)
    CrawlTask.objects.bulk_create(
        CrawlTask(site=busy, url=f"https://busy.test/{i}", kind=CrawlTask.Kind.DETAIL)
        for i in range(scheduler.BACKLOG_THRESHOLD + 1)
    )
    caplog.set_level(logging.INFO, logger="marceline.scheduler")
    context = JobContext(now=T0, last_succeeded_at=None)

    seeds = scheduler.run_crawl_seeds(context)
    recheck = scheduler.run_crawl_recheck(context)

    assert commands == [
        ("crawl_seed", {"site": "idle.test", "refresh": True}),
        (
            "crawl_recheck",
            {
                "site": "busy.test",
                "never_checked": True,
                "limit": scheduler.NEVER_CHECKED_BATCH,
                "priority": RECHECK_PRIORITY,
            },
        ),
        ("crawl_recheck", {"site": "idle.test", "older_than_hours": 24}),
    ]
    assert "crawl_seeds: skipped busy.test (301 pending crawl tasks > 300)" in caplog.text
    assert "crawl_recheck: skipped busy.test" in caplog.text
    assert "crawl_recheck: busy.test over backlog, never-checked batch" in caplog.text
    assert "idle.test: crawl_seed output" in seeds
    assert "backlog skipped: busy.test" in seeds
    assert "busy.test: never-checked batch: crawl_recheck output" in recheck
    assert "backlog skipped: busy.test" in recheck


def test_recheck_queues_never_checked_batch_while_over_backlog(
    db: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(scheduler, "NEVER_CHECKED_BATCH", 2)
    busy = Site.objects.create(domain="busy.test")
    CrawlTask.objects.bulk_create(
        CrawlTask(site=busy, url=f"https://busy.test/queued/{i}", kind=CrawlTask.Kind.LISTING)
        for i in range(scheduler.BACKLOG_THRESHOLD + 1)
    )
    never = [
        Page.objects.create(site=busy, url=f"https://busy.test/never/{i}", title=str(i))
        for i in range(3)
    ]
    stale = Page.objects.create(
        site=busy,
        url="https://busy.test/stale",
        availability_checked_at=timezone.now() - timedelta(days=5),
    )
    caplog.set_level(logging.INFO, logger="marceline.scheduler")

    summary = scheduler.run_crawl_recheck(JobContext(now=T0, last_succeeded_at=None))

    queued = CrawlTask.objects.filter(url__startswith="https://busy.test/never/")
    assert sorted(queued.values_list("url", flat=True)) == [never[0].url, never[1].url]
    assert set(queued.values_list("priority", flat=True)) == {RECHECK_PRIORITY}
    assert set(queued.values_list("kind", flat=True)) == {CrawlTask.Kind.DETAIL}
    assert not CrawlTask.objects.filter(url=stale.url).exists()
    assert "busy.test: never-checked batch: Enqueued 2 task(s)." in summary
    assert "backlog skipped: busy.test" in summary
    assert "never-checked batch (limit 2, priority 35)" in caplog.text


def test_removed_pages_are_rechecked_weekly_in_a_small_low_priority_batch(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review-app M5: without this, a re-upload at a removed URL was never noticed and a case
    # stayed "handled".
    monkeypatch.setattr(scheduler, "REMOVED_RECHECK_BATCH", 2)
    job = next(job for job in scheduler.JOBS if job.name == "crawl_recheck_removed")
    assert job.interval() == timedelta(days=7)
    busy = Site.objects.create(domain="busy.test")
    CrawlTask.objects.bulk_create(
        CrawlTask(site=busy, url=f"https://busy.test/queued/{i}", kind=CrawlTask.Kind.LISTING)
        for i in range(scheduler.BACKLOG_THRESHOLD + 1)
    )
    now = timezone.now()

    def page(slug: str, *, days: int, removed: bool = True, active: bool = True) -> Page:
        return Page.objects.create(
            site=busy,
            url=f"https://busy.test/{slug}",
            availability=Page.Availability.REMOVED if removed else Page.Availability.PLAYABLE,
            availability_checked_at=now - timedelta(days=days),
            active=active,
            inactive_reason="" if active else Page.InactiveReason.UNTARGETED,
        )

    oldest, older = page("oldest", days=30), page("older", days=20)
    page("third", days=10)
    page("recent", days=2)
    page("playable", days=30, removed=False)
    page("inactive", days=30, active=False)

    summary = scheduler.run_crawl_recheck_removed(JobContext(now=T0, last_succeeded_at=None))

    queued = CrawlTask.objects.filter(kind=CrawlTask.Kind.DETAIL)
    assert sorted(queued.values_list("url", flat=True)) == sorted([oldest.url, older.url])
    assert set(queued.values_list("priority", flat=True)) == {scheduler.REMOVED_RECHECK_PRIORITY}
    assert summary == "busy.test: Enqueued 2 task(s)."


def test_discovery_job_is_off_by_default_and_before_sync_targets(
    db: None, commands: list[tuple[str, dict[str, Any]]], settings: Any
) -> None:
    job = next(job for job in scheduler.JOBS if job.name == "discovery")
    names = [job.name for job in scheduler.JOBS]
    assert names.index("discovery") < names.index("sync_targets")
    assert job.interval() == timedelta(hours=24)
    assert job.env_var == "SCHEDULER_DISCOVERY_HOURS"
    context = JobContext(now=T0, last_succeeded_at=None)

    # Off by default (issue #20): discovery is a paid search API.
    assert job.guard is not None
    assert job.guard(context) == "scheduled discovery is off (DISCOVERY_SCHEDULE_ENABLED)"

    settings.DISCOVERY_SCHEDULE_ENABLED = True
    settings.DATAFORSEO_LOGIN = ""
    settings.DATAFORSEO_PASSWORD = ""
    assert job.guard(context) == (
        "DataForSEO credentials not configured (DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD)"
    )

    settings.DATAFORSEO_LOGIN = "secret-login"
    settings.DATAFORSEO_PASSWORD = "secret-pass"
    assert job.guard(context) is None

    assert scheduler.run_discovery(context) == "discover_sources output; Total: 1 page"
    assert commands == [("discover_sources", {})]


def test_build_clusters_guard_needs_new_frame_hashes(site: Site) -> None:
    guard = scheduler.new_frame_hashes
    assert guard(JobContext(now=T0, last_succeeded_at=None)) is None

    frame_hash(site, "old")
    later = timezone.now() + timedelta(minutes=1)
    assert guard(JobContext(now=later, last_succeeded_at=later)) == (
        "no new frame hashes since last successful run"
    )
    before = timezone.now() - timedelta(minutes=1)
    assert guard(JobContext(now=later, last_succeeded_at=before)) is None


def test_frame_hash_guards_count_a_reactivated_page_as_new(site: Site) -> None:
    frame_hash(site, "old")
    later = timezone.now() + timedelta(minutes=1)
    context = JobContext(now=later, last_succeeded_at=later)
    assert scheduler.new_frame_hashes(context) is not None

    Page.objects.update(active=True, active_changed_at=later + timedelta(seconds=1))
    assert scheduler.new_frame_hashes(context) is None
    # A deactivation brings nothing new to match.
    Page.objects.update(active=False, inactive_reason="untargeted")
    assert scheduler.new_frame_hashes(context) is not None


def test_sync_targets_runs_before_matching_only_when_enabled(
    db: None, commands: list[tuple[str, dict[str, Any]]], settings: Any
) -> None:
    job = next(job for job in scheduler.JOBS if job.name == "sync_targets")
    names = [job.name for job in scheduler.JOBS]
    assert names.index("sync_targets") < names.index("build_clusters")
    assert job.interval() == timedelta(hours=3)
    context = JobContext(now=T0, last_succeeded_at=None)

    settings.TARGETED_INDEXING_ENABLED = False
    assert job.guard is not None
    assert job.guard(context) == "targeted indexing is off (TARGETED_INDEXING_ENABLED)"
    settings.TARGETED_INDEXING_ENABLED = True
    assert job.guard(context) is None

    assert scheduler.run_sync_targets(context) == "deactivate_untargeted output; Total: 1 page"
    assert commands == [("deactivate_untargeted", {"apply": True})]


def test_match_references_guard_needs_new_frames_or_references(site: Site) -> None:
    guard = scheduler.new_frame_hashes_or_references
    assert guard(JobContext(now=T0, last_succeeded_at=None)) == "no references"

    reference("first")
    frame_hash(site, "old")
    assert guard(JobContext(now=T0, last_succeeded_at=None)) is None
    later = timezone.now() + timedelta(minutes=1)
    assert guard(JobContext(now=later, last_succeeded_at=later)) is not None

    before = timezone.now()
    reference("second")
    assert guard(JobContext(now=later, last_succeeded_at=before)) is None
    FrameHash.objects.all().delete()
    Creator.objects.get().references.filter(title="second").delete()
    assert guard(JobContext(now=later, last_succeeded_at=before)) is not None
    frame_hash(site, "new")
    assert guard(JobContext(now=later, last_succeeded_at=before)) is None


def test_match_references_guard_sees_clusters_labels_thresholds_and_decisions(
    site: Site,
) -> None:
    guard = scheduler.new_frame_hashes_or_references
    reference("first")
    original = Reference.objects.get()
    page = frame_hash(site, "page").media.page
    other = frame_hash(site, "other").media.page
    match = ReferenceMatch.objects.create(
        reference=original,
        page=page,
        best_distance=5,
        matched_frames=1,
        page_frames=1,
        best_reference_timestamp_s=0,
        copy_extent_version=CLASSIFIER_VERSION,
    )
    leak = ReferenceLeak.objects.create(reference=original, page=other)
    group = CommonFrameGroup.objects.create(representative_hash=bytes(32), max_distance=8)
    since = timezone.now() + timedelta(seconds=1)
    context = JobContext(now=since, last_succeeded_at=since)
    assert guard(context) is not None

    JobRun.objects.create(name="match_references", last_finished_at=since)
    ReferenceMatch.objects.filter(pk=match.pk).update(updated_at=since + timedelta(seconds=1))
    assert guard(context) is not None  # a proposed row rewritten by matching is no change
    ReferenceMatch.objects.filter(pk=match.pk).update(
        status="rejected", updated_at=since + timedelta(seconds=1)
    )
    assert guard(context) is None
    ReferenceMatch.objects.filter(pk=match.pk).update(updated_at=since - timedelta(seconds=1))
    assert guard(context) is not None

    later = since + timedelta(seconds=1)
    ReferenceLeak.objects.filter(pk=leak.pk).update(status="confirmed", updated_at=later)
    assert guard(context) is None
    ReferenceLeak.objects.filter(pk=leak.pk).update(updated_at=since - timedelta(seconds=1))
    CommonFrameGroup.objects.filter(pk=group.pk).update(updated_at=later)  # a rebuild
    assert guard(context) is not None
    CommonFrameGroup.objects.filter(pk=group.pk).update(labeled_at=later)  # a human label
    assert guard(context) is None
    CommonFrameGroup.objects.filter(pk=group.pk).update(labeled_at=None)
    cluster = Cluster.objects.create()
    member = ClusterMember.objects.create(cluster=cluster, page=page, evidence="manual")
    ClusterMember.objects.filter(pk=member.pk).update(updated_at=later)
    assert guard(context) is None
    ClusterMember.objects.filter(pk=member.pk).update(updated_at=since - timedelta(seconds=1))
    ConfidenceSettings.objects.create(pk=1, high_threshold=0.8, review_threshold=0.5)
    ConfidenceSettings.objects.filter(pk=1).update(updated_at=later)
    assert guard(context) is None
    ConfidenceSettings.objects.filter(pk=1).update(updated_at=since - timedelta(seconds=1))
    assert guard(context) is not None
    # A copy extent written by another classifier version (issue #62) needs a matching run.
    ReferenceMatch.objects.filter(pk=match.pk).update(copy_extent_version=CLASSIFIER_VERSION - 1)
    assert guard(context) is None


def test_clusters_and_common_frames_run_before_incremental_match_references(
    db: None, commands: list[tuple[str, dict[str, Any]]], settings: Any
) -> None:
    # Explicit, not ambient: the operator's `.env` turns targeted indexing on (conftest.py).
    settings.TARGETED_INDEXING_ENABLED = False
    reference("first")

    records = scheduler.run_due_jobs(scheduler.JOBS, FakeClock())

    # Common frames discount evidence, so they are rebuilt before matching; matching is
    # incremental (no --full). Browser-capture triage reads the fresh matches, so it runs last.
    assert commands[:3] == [
        ("build_clusters", {}),
        ("build_common_frames", {}),
        ("match_references", {}),
    ]
    assert [name for name, _ in commands] == [
        "build_clusters",
        "build_common_frames",
        "match_references",
        "sync_cases",
        "triage_browser_capture",
        "lookup_hosting",
        "rights_housekeeping",
        "invoice_retention",
    ]
    assert commands[3] == ("sync_cases", {})
    assert isinstance(commands[4][1]["gate_signals"], GateSignals)
    assert commands[5] == ("lookup_hosting", {"limit": scheduler.LOOKUP_HOSTING_LIMIT})
    assert [record.name for record in records] == [
        # Issue #22: a broken adapter is judged first, from counters the crawlers already wrote.
        "adapter_health",
        "crawl_seeds",
        "crawl_recheck",
        "crawl_recheck_removed",
        "discovery",
        "sync_targets",
        "build_clusters",
        "build_common_frames",
        "match_references",
        "sync_cases",
        "browser_capture_triage",
        "lookup_hosting",
        "observe_notice_removals",
        "request_accounting",
        # Issue #107: our share of each site's catalogue. Counts only; it calls no management
        # command either, and it changes nothing about any crawl.
        "catalogue_share",
        "safety_release_queue",
        # ADR 0044: the spool reaper, the backstop for a worker that died mid-decode. It calls no
        # management command, so it never appears in `commands` above.
        "upload_spool",
        "safety_digest",
        # Accounts data; placed before the fixed retention tail below rather than after it, so
        # that tail's own indices (`JOBS[-6:]` etc., asserted elsewhere) are undisturbed.
        "invite_retention",
        "rights_housekeeping",
        "invoice_retention",
        "safety_retention",
        "page_retention",
        "customer_data_retention",
        "research_retention",
    ]
    assert JobRun.objects.get(name="sync_targets").last_status == JobRun.Status.SKIPPED
    assert JobRun.objects.get(name="safety_retention").last_status == JobRun.Status.SKIPPED
    assert JobRun.objects.get(name="observe_notice_removals").last_message == (
        "0 notice(s) observed removed"
    )
    assert JobRun.objects.get(name="match_references").last_message == (
        "match_references output; Total: 1 page"
    )


def test_sync_targets_runs_before_clusters_and_matching_when_enabled(
    db: None, commands: list[tuple[str, dict[str, Any]]], settings: Any
) -> None:
    settings.TARGETED_INDEXING_ENABLED = True
    reference("first")

    records = scheduler.run_due_jobs(scheduler.JOBS, FakeClock())

    # Page activity is brought up to date before anything is clustered or matched (issue #71).
    assert [name for name, _ in commands] == [
        "deactivate_untargeted",
        "build_clusters",
        "build_common_frames",
        "match_references",
        "sync_cases",
        "triage_browser_capture",
        "lookup_hosting",
        "rights_housekeeping",
        "invoice_retention",
    ]
    assert commands[0] == ("deactivate_untargeted", {"apply": True})
    assert JobRun.objects.get(name="sync_targets").last_status == JobRun.Status.OK
    names = [record.name for record in records]
    assert names.index("sync_targets") < names.index("build_clusters")


def test_sync_cases_runs_every_three_hours_right_after_matching(
    db: None, commands: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [job.name for job in scheduler.JOBS]
    assert names.index("sync_cases") == names.index("match_references") + 1
    job = scheduler.JOBS[names.index("sync_cases")]
    assert job.interval() == timedelta(hours=3)
    assert job.guard is None
    monkeypatch.setenv("SCHEDULER_SYNC_CASES_HOURS", "1")
    assert job.interval() == timedelta(hours=1)
    monkeypatch.delenv("SCHEDULER_SYNC_CASES_HOURS")

    clock = FakeClock()
    record = scheduler.run_due_jobs([job], clock)[0]
    clock.advance(hours=2, minutes=59)
    scheduler.run_due_jobs([job], clock)

    assert commands == [("sync_cases", {})]
    assert record.last_status == JobRun.Status.OK
    assert record.last_message == "sync_cases output; Total: 1 page"


def test_sync_cases_job_reconciles_real_cases(db: None) -> None:
    job = next(job for job in scheduler.JOBS if job.name == "sync_cases")

    assert job.run(JobContext(now=T0, last_succeeded_at=None)) == (
        "0 originals reconciled, 0 with a case"
    )


def test_build_common_frames_is_daily_and_guarded_by_new_frame_hashes(
    site: Site, commands: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = next(job for job in scheduler.JOBS if job.name == "build_common_frames")
    assert job.interval() == timedelta(hours=24)
    assert job.guard is scheduler.new_frame_hashes
    monkeypatch.setenv("SCHEDULER_BUILD_COMMON_FRAMES_HOURS", "6")
    assert job.interval() == timedelta(hours=6)
    monkeypatch.delenv("SCHEDULER_BUILD_COMMON_FRAMES_HOURS")

    frame_hash(site, "first")
    clock = FakeClock(timezone.now() + timedelta(seconds=1))
    scheduler.run_due_jobs([job], clock)
    assert [name for name, _ in commands] == ["build_common_frames"]

    # A day later without new frame hashes: skipped. With a new one: rebuilt.
    clock.advance(hours=24, minutes=1)
    record = scheduler.run_due_jobs([job], clock)[0]
    assert record.last_status == JobRun.Status.SKIPPED
    assert [name for name, _ in commands] == ["build_common_frames"]
    clock.advance(hours=24)
    frame_hash(site, "second")
    FrameHash.objects.filter(media__page__title="second").update(created_at=clock.now)
    record = scheduler.run_due_jobs([job], clock)[0]
    assert record.last_status == JobRun.Status.OK
    assert [name for name, _ in commands] == ["build_common_frames", "build_common_frames"]
    assert record.last_message == "build_common_frames output; Total: 1 page"


def test_browser_capture_triage_is_daily_and_guarded_by_matches_leaks_or_flags(
    site: Site,
) -> None:
    job = next(job for job in scheduler.JOBS if job.name == "browser_capture_triage")
    assert job.interval() == timedelta(hours=24)
    guard = scheduler.new_matches_leaks_or_flags
    assert job.guard is guard
    assert guard(JobContext(now=T0, last_succeeded_at=None)) is None

    page = Page.objects.create(site=site, url="https://example.test/page", title="page")
    reference("first")
    later = timezone.now() + timedelta(minutes=1)
    assert guard(JobContext(now=later, last_succeeded_at=later)) == (
        "no new or changed matches, leaks or safety flags since last successful run"
    )
    before = timezone.now() - timedelta(seconds=1)
    assert guard(JobContext(now=later, last_succeeded_at=before)) is not None  # nothing changed
    leak = ReferenceLeak.objects.create(reference=Creator.objects.get().references.get(), page=page)
    assert guard(JobContext(now=later, last_succeeded_at=before)) is None
    after = timezone.now() + timedelta(seconds=1)
    assert guard(JobContext(now=later, last_succeeded_at=after)) is not None
    leak.save()
    Page.objects.filter(pk=page.pk).update(safety_flagged_at=later + timedelta(seconds=1))
    assert guard(JobContext(now=later, last_succeeded_at=later)) is None


def test_crawler_gate_signals_from_skipped_tasks(site: Site) -> None:
    gated = Page.objects.create(site=site, url="https://example.test/gated", title="gated")
    verify = Site.objects.create(domain="verify.test")
    CrawlTask.objects.create(
        site=site,
        url=gated.url,
        kind=CrawlTask.Kind.DETAIL,
        status=CrawlTask.Status.SKIPPED,
        decision_reason="age_self_declaration gate, no operator approval (Site.age_confirmation)",
    )
    CrawlTask.objects.create(
        site=verify,
        url="https://verify.test/video",
        kind=CrawlTask.Kind.DETAIL,
        status=CrawlTask.Status.SKIPPED,
        decision_reason="age_verification_provider: ExampleVerify (always skipped, never automated)",
    )
    CrawlTask.objects.create(
        site=verify,
        url="https://verify.test/other",
        kind=CrawlTask.Kind.DETAIL,
        status=CrawlTask.Status.SKIPPED,
        decision_reason="age_self_declaration gate, no operator approval (Site.age_confirmation)",
    )
    CrawlTask.objects.create(
        site=site,
        url="https://example.test/pending",
        kind=CrawlTask.Kind.DETAIL,
        decision_reason="age_self_declaration gate",
    )

    signals = scheduler.crawler_gate_signals()

    assert signals.checked
    assert dict(signals.pages) == {gated.pk: "age_self_declaration"}
    assert dict(signals.sites) == {
        site.pk: "age_self_declaration",
        verify.pk: "age_verification_provider",
    }


def test_browser_capture_triage_job_only_refreshes_the_queue(site: Site) -> None:
    creator = Creator.objects.create(name="Creator")
    original = creator.references.create(title="o", sha256="o" * 64, frame_fps=1)
    page = Page.objects.create(site=site, url="https://example.test/poster", title="poster")
    ReferenceLeak.objects.create(reference=original, page=page, status="confirmed")

    summary = scheduler.run_browser_capture_triage(JobContext(now=T0, last_succeeded_at=None))

    assert summary.startswith("Browser capture triage: 1 candidates; created 1,")
    assert "Budget: capture disabled;" in summary
    assert "  page" not in summary
    row = BrowserCaptureCandidate.objects.get()
    assert (row.status, row.attempts, row.signals["gate_checked"]) == ("queued", 0, True)


def test_match_summary_persisted_and_logged(
    db: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    summaries = [
        "Reference 1 First: title: 5 pages matched (3 strong, 2 weak), "
        "2 clusters matched; 1 creator-linked pages NOT matched",
        "Reference 2 Second: 7 pages matched (6 strong, 1 weak), "
        "3 clusters matched; 1 creator-linked pages NOT matched; bands: 2 high, 4 review, 1 low",
    ]
    detail = "  NOT matched: 9 Example https://example.test/page"
    output = f"{summaries[0]}\n{detail}\n\n{summaries[1]}\n{detail}\n"

    def fake_call(command: str, **options: Any) -> None:
        assert command == "match_references"
        options["stdout"].write(output)

    monkeypatch.setattr(scheduler, "call_command", fake_call)
    caplog.set_level(logging.DEBUG, logger="marceline.scheduler")
    record = scheduler.run_job(
        Job("match_references", 3, scheduler.run_match_references), FakeClock()
    )

    record.refresh_from_db()
    expected = "Total: 2 references, 12 pages matched (9 strong, 3 weak)\n" + "\n".join(summaries)
    assert record.last_message == expected
    info = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert f"match_references: finished: {expected}" in info
    assert detail not in info
    assert output in caplog.text


@pytest.mark.parametrize("extra", [0, 1, 100])
def test_summary_length_limit(db: None, caplog: pytest.LogCaptureFixture, extra: int) -> None:
    message = "x" * (scheduler.MESSAGE_MAX_CHARS + extra)
    caplog.set_level(logging.INFO, logger="marceline.scheduler")
    record = scheduler.run_job(Job("long", 3, lambda context: message), FakeClock())
    record.refresh_from_db()
    expected = message if extra == 0 else "x" * (scheduler.MESSAGE_MAX_CHARS - 1) + "…"
    assert record.last_message == expected
    assert f"long: finished: {expected}" in caplog.messages


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("2 clusters created / 3 members proposed\n", "2 clusters created / 3 members proposed"),
        (
            "First summary\n  detail\n\tmore detail\n\nSecond summary\n",
            "First summary; Second summary",
        ),
        ("", "done"),
    ],
)
def test_other_command_summaries(
    monkeypatch: pytest.MonkeyPatch, output: str, expected: str
) -> None:
    def fake_call(command: str, **options: Any) -> None:
        options["stdout"].write(output)

    monkeypatch.setattr(scheduler, "call_command", fake_call)
    assert scheduler.run_build_clusters(JobContext(T0, None)) == expected


def test_empty_match_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "call_command", lambda *args, **kwargs: None)
    assert scheduler.run_match_references(JobContext(T0, None)) == "done"


@pytest.fixture
def fake_jobs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    jobs = (recording_job("alpha", calls), recording_job("beta", calls))
    monkeypatch.setattr(scheduler, "JOBS", jobs)
    return calls


@pytest.fixture
def lock_held_elsewhere(db: None) -> Iterator[None]:
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [scheduler.LOCK_KEY])
            assert cursor.fetchone() == (True,)
        yield
    finally:
        other.close()


def test_second_scheduler_exits_while_lock_is_held(
    fake_jobs: list[str], lock_held_elsewhere: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="marceline.scheduler")

    # Exit non-zero, like `crawl_worker` for a site another worker holds: a `scheduler --only
    # page_retention` run beside the scheduler container does nothing at all, and an operator
    # (or a cron line, or an agent) must not read that as "the job ran".
    with pytest.raises(CommandError, match="another scheduler holds the single-instance lock"):
        call_command("scheduler", "--once")

    assert fake_jobs == []
    assert not JobRun.objects.exists()
    assert "another scheduler holds the lock" in caplog.text


def test_lock_is_released_after_a_run(db: None, fake_jobs: list[str]) -> None:
    call_command("scheduler", "--once")
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [scheduler.LOCK_KEY])
            assert cursor.fetchone() == (True,)
    finally:
        other.close()
    assert fake_jobs == ["alpha", "beta"]


def test_once_and_only(db: None, fake_jobs: list[str]) -> None:
    call_command("scheduler", "--once", "--only", "beta")

    assert fake_jobs == ["beta"]
    assert list(JobRun.objects.values_list("name", flat=True)) == ["beta"]
    with pytest.raises(CommandError, match="Unknown job 'gamma'"):
        call_command("scheduler", "--once", "--only", "gamma")


def test_invalid_interval_env_is_a_command_error(
    db: None, fake_jobs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCHEDULER_BETA_HOURS", "-1")

    with pytest.raises(CommandError, match="SCHEDULER_BETA_HOURS"):
        call_command("scheduler", "--once")
    assert fake_jobs == []


def test_request_accounting_job_records_media_hosts_and_rolls_up_old_counters(
    site: Site,
) -> None:
    page = Page.objects.create(site=site, url="https://example.test/v")
    Media.objects.create(page=page, kind="poster", url="https://img.cdn.test/a.jpg")
    Media.objects.create(page=page, kind="frame", url="https://WWW.example.test:8443/b.jpg?x=1")
    Media.objects.create(page=page, kind="capture", url="https://example.test/v#browser-capture")
    old = datetime(2026, 7, 1, 3, tzinfo=UTC)
    upsert_counts({(old, site.pk, "example.test", "origin", "page", "page", "2xx"): [5, 0]})
    job = next(job for job in scheduler.JOBS if job.name == "request_accounting")

    message = job.run(JobContext(now=datetime(2026, 9, 17, tzinfo=UTC), last_succeeded_at=None))

    assert message == "2 new site host(s); 1 hourly request counter row(s) rolled up"
    assert sorted(SiteHost.objects.values_list("host", "role")) == [
        ("img.cdn.test", "cdn"),
        ("www.example.test", "origin"),
    ]
    assert RequestCountDaily.objects.get().requests == 5


class StopLoop(Exception):
    pass


def test_loop_sleeps_and_repeats_until_stopped(db: None) -> None:
    calls: list[str] = []
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise StopLoop
        clock.advance(hours=2)

    with pytest.raises(StopLoop):
        scheduler.run_scheduler(
            [recording_job("alpha", calls, hours=3)], once=False, clock=clock, sleep=sleep
        )

    # Passes at T0, T0+2h (not due), T0+4h (due).
    assert calls == ["alpha", "alpha"]
    assert sleeps == [scheduler.POLL_SECONDS] * 3


def test_safety_release_queue_runs_every_three_hours_without_commands(
    db: None, commands: list[tuple[str, dict[str, Any]]]
) -> None:
    job = next(job for job in scheduler.JOBS if job.name == "safety_release_queue")

    message = job.run(JobContext(now=datetime(2026, 9, 17, tzinfo=UTC), last_succeeded_at=None))

    assert job.default_hours == 3
    assert commands == []
    assert message == "0 queued (0 new), 0 dequeued, 0 without customer credit, 0 with another hit"


def test_rights_housekeeping_runs_daily_before_invoice_and_page_retention(
    db: None, commands: list[tuple[str, dict[str, Any]]]
) -> None:
    names = [job.name for job in scheduler.JOBS]
    assert names[-6:] == [
        "rights_housekeeping",
        "invoice_retention",
        "safety_retention",
        "page_retention",
        "customer_data_retention",
        # ADR 0048: truly last, because the two jobs above free names it then judges.
        "research_retention",
    ]
    job = scheduler.JOBS[-6]

    message = job.run(JobContext(now=datetime(2026, 9, 17, tzinfo=UTC), last_succeeded_at=None))

    assert (job.name, job.default_hours) == ("rights_housekeeping", 24)
    assert job.env_var == "SCHEDULER_RIGHTS_HOUSEKEEPING_HOURS"
    assert commands == [("rights_housekeeping", {})]
    assert message == "rights_housekeeping output; Total: 1 page"


def test_invoice_retention_runs_daily_before_safety_and_page_retention(
    db: None, commands: list[tuple[str, dict[str, Any]]]
) -> None:
    job = scheduler.JOBS[-5]

    message = job.run(JobContext(now=datetime(2026, 9, 17, tzinfo=UTC), last_succeeded_at=None))

    assert (job.name, job.default_hours) == ("invoice_retention", 24)
    assert job.env_var == "SCHEDULER_INVOICE_RETENTION_HOURS"
    assert commands == [("invoice_retention", {})]
    assert message == "invoice_retention output; Total: 1 page"


def test_safety_retention_runs_daily_before_page_retention_only_when_enabled(
    db: None, commands: list[tuple[str, dict[str, Any]]], settings: Any
) -> None:
    # Right before page_retention (issue #100): closing a stale reported case here lifts
    # page_retention's own exclusion of open/reported incidents within the same pass, and neither
    # job otherwise depends on the other.
    job = scheduler.JOBS[-4]
    context = JobContext(now=datetime(2026, 9, 17, tzinfo=UTC), last_succeeded_at=None)

    assert job.name == "safety_retention"
    assert job.default_hours == 24
    assert job.env_var == "SCHEDULER_SAFETY_RETENTION_HOURS"
    assert job.guard is not None
    settings.SAFETY_RETENTION_ENABLED = False
    assert job.guard(context) == "safety retention is off (SAFETY_RETENTION_ENABLED)"
    settings.SAFETY_RETENTION_ENABLED = True
    assert job.guard(context) is None

    message = job.run(context)

    # No period is passed: both live in index.incidents as code constants, matching the command's
    # own default flags (docs/safety.md).
    assert commands == [("safety_retention", {})]
    assert message == "safety_retention output; Total: 1 page"


def test_page_retention_runs_daily_second_to_last_only_when_enabled(
    db: None, commands: list[tuple[str, dict[str, Any]]], settings: Any
) -> None:
    # Last until ADR 0045 added customer data deletion and ADR 0048 the research side behind it;
    # still after everything that reads a page.
    job = scheduler.JOBS[-3]
    context = JobContext(now=datetime(2026, 9, 17, tzinfo=UTC), last_succeeded_at=None)

    assert job.name == "page_retention"
    assert job.default_hours == 24
    assert job.env_var == "SCHEDULER_PAGE_RETENTION_HOURS"
    assert job.guard is not None
    settings.PAGE_RETENTION_ENABLED = False
    assert job.guard(context) == "page retention is off (PAGE_RETENTION_ENABLED)"
    settings.PAGE_RETENTION_ENABLED = True
    assert job.guard(context) is None

    message = job.run(context)

    assert commands == [("page_retention", {"apply": True})]
    assert message == "page_retention output; Total: 1 page"


def test_research_retention_runs_last_of_all_only_when_enabled(
    db: None, commands: list[tuple[str, dict[str, Any]]], settings: Any
) -> None:
    """ADR 0048 (issues #103, #104). Last: page retention and customer-data deletion both delete
    rows that keep a name in use, so the research side is judged after them."""
    job = scheduler.JOBS[-1]
    context = JobContext(now=datetime(2026, 9, 17, tzinfo=UTC), last_succeeded_at=None)

    assert job.name == "research_retention"
    assert job.default_hours == 24
    assert job.env_var == "SCHEDULER_RESEARCH_RETENTION_HOURS"
    assert job.guard is not None
    settings.RESEARCH_RETENTION_ENABLED = False
    assert job.guard(context) == "research retention is off (RESEARCH_RETENTION_ENABLED)"
    settings.RESEARCH_RETENTION_ENABLED = True
    assert job.guard(context) is None

    message = job.run(context)

    assert commands == [("research_retention", {"apply": True})]
    assert message == "research_retention output; Total: 1 page"


def test_adapter_health_is_judged_first_and_makes_no_request(db: None) -> None:
    """Issue #22: a broken parser is a data problem, and it reads counters, never a site.

    It lives here rather than in `crawler/tests/` because importing `marceline.scheduler` from
    the crawler app is exactly the dependency import-linter forbids.
    """
    site = Site.objects.create(domain="site01.example", crawl_delay_s=0)
    names = [job.name for job in scheduler.JOBS]
    assert names[0] == "adapter_health"
    job = scheduler.JOBS[0]
    assert job.interval() == timedelta(hours=6)
    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(site.pk, adapter_health.ParseOutcome(no_page=True), now=now)
    message = job.run(JobContext(now=now, last_succeeded_at=None))
    assert "1 broken (site01.example)" in message
    assert AdapterHealth.objects.get(site=site).status == AdapterHealth.Status.BROKEN


def test_a_daily_job_runs_once_a_day_on_or_after_its_hour(db: None) -> None:
    """The safety digest has to land before the working day, not at whatever minute it first ran.

    A guard cannot express that: `run_job` stamps `last_started_at` before asking the guard, so a
    job skipping "not the hour yet" would be due at the same wrong minute the next day, forever.
    """
    calls: list[str] = []
    job = Job("alpha", 24, recording_job("alpha", calls).run, daily_at_hour=lambda: 6)
    clock = FakeClock(datetime(2026, 9, 1, 3, 0, tzinfo=UTC))

    scheduler.run_due_jobs([job], clock)
    assert calls == []  # before the hour

    clock.advance(hours=3)
    scheduler.run_due_jobs([job], clock)
    assert calls == ["alpha"]

    clock.advance(hours=5)  # same day (11:00), already sent
    scheduler.run_due_jobs([job], clock)
    clock.advance(hours=18)  # next day, but not yet the hour (05:00)
    scheduler.run_due_jobs([job], clock)
    assert calls == ["alpha"]

    clock.advance(hours=1)
    scheduler.run_due_jobs([job], clock)
    assert calls == ["alpha", "alpha"]


def test_safety_digest_job_mails_one_window_a_day(
    db: None, site: Site, settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings.SAFETY_ALERT_TO = "ops@example.test"
    settings.SAFETY_ALERT_FROM = "marceline@example.test"
    settings.SAFETY_DIGEST_HOUR_UTC = 6
    job = next(job for job in scheduler.JOBS if job.name == "safety_digest")
    clock = FakeClock(datetime(2026, 9, 18, 6, 5, tzinfo=UTC))
    Page.objects.create(
        site=site,
        url="https://site01.example/one",
        title="blocked one",
        safety_status=Page.SafetyStatus.BLOCKED,
        safety_matches=[{"term": "jailbait", "category": "minor", "action": "auto_block"}],
        safety_flagged_at=clock.now - timedelta(hours=2),
    )

    assert job.daily_at_hour is not None and job.daily_at_hour() == 6
    assert job.env_var == "SCHEDULER_SAFETY_DIGEST_HOURS"

    scheduler.run_due_jobs([job], clock)

    record = JobRun.objects.get(name="safety_digest")
    assert record.last_status == JobRun.Status.OK
    assert "1 page(s) blocked" in record.last_message and "mailed" in record.last_message
    assert len(django_mail.outbox) == 1

    # Same day: nothing runs again, so the operator gets exactly one mail a day.
    clock.advance(hours=4)
    scheduler.run_due_jobs([job], clock)
    assert len(django_mail.outbox) == 1

    # Next day, no new block: the job still runs and still mails — one report a day, quiet or not
    # (2026-09-19), because its own absence is the signal the reporting pipeline stopped.
    clock.advance(hours=20)
    scheduler.run_due_jobs([job], clock)
    assert len(django_mail.outbox) == 2
    assert "mailed" in JobRun.objects.get(name="safety_digest").last_message
    assert "0 page(s) newly blocked" in django_mail.outbox[1].body
