"""Periodic batch jobs run by the single `scheduler` process (ADR 0012).

The scheduler only enqueues crawl work and runs matching batches; crawlers and the media worker
are separate long-running processes. The job table lives in the project package because it
wires crawler and matching commands together, which no app may do.
"""

import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from catalog.catalogue_size import over_threshold
from catalog.models import Site
from catalog.request_accounting import record_observed_hosts, rollup_request_counts
from core.env import env_int
from core.models import JobRun
from crawler import adapter_alerts, adapter_health, safety_digest
from crawler.blocking import AGE_SELF_DECLARATION, AGE_VERIFICATION_PROVIDER
from crawler.models import AdapterHealth, CrawlTask
from crawler.services import RECHECK_PRIORITY
from crawler.sites.base import DEEP_DETAIL_PRIORITY
from index import catalogue_share
from index.models import FrameHash, Page
from index.services import media_hosts_by_site
from marceline.hosting import observe_notice_removals
from marceline.safety_rules import sync_release_queue
from matching.capture_triage import GateSignals
from matching.copy_extent import CLASSIFIER_VERSION
from matching.models import (
    ClusterMember,
    CommonFrameGroup,
    ConfidenceSettings,
    Reference,
    ReferenceLeak,
    ReferenceMatch,
)

logger = logging.getLogger(__name__)

# Session-level advisory lock key held by the running scheduler (build_clusters uses 73428109).
LOCK_KEY = 73428200
# Skip enqueueing for a site while more tasks than this are pending: never outpace the crawlers.
BACKLOG_THRESHOLD = 300
# ...except for pages never availability-checked: up to this many per site per recheck run.
NEVER_CHECKED_BATCH = 100
RECHECK_OLDER_THAN_HOURS = 24
# Removed pages (review-app M5): a takedown is only "handled" while the URL stays gone, so removed
# active pages are checked again weekly, least recently checked first, a bounded batch per site at
# a priority below every ordinary detail page — a re-upload is rare, the crawl budget is not.
REMOVED_RECHECK_OLDER_THAN_HOURS = 7 * 24
REMOVED_RECHECK_BATCH = 50
REMOVED_RECHECK_PRIORITY = DEEP_DETAIL_PRIORITY
POLL_SECONDS = 60
MESSAGE_MAX_CHARS = 2000
REFERENCE_SUMMARY = re.compile(
    r"^Reference \d+ .*: (\d+) pages matched \((\d+) strong, (\d+) weak\), "
    r"\d+ clusters matched; \d+ creator-linked pages NOT matched(?:; bands: .*)?$"
)

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class JobContext:
    now: datetime
    last_succeeded_at: datetime | None


@dataclass(frozen=True)
class Job:
    """A periodic job. `guard` returns a skip reason or None; `run` returns a short summary."""

    name: str
    default_hours: int
    run: Callable[[JobContext], str]
    guard: Callable[[JobContext], str | None] | None = None
    #: Run once per UTC day, on or after this hour, instead of every `interval()`. For a job whose
    #: output a person reads at a predictable time (the safety digest, 2026-09-18). A guard cannot
    #: do this: `run_job` stamps `last_started_at` before it asks the guard, so a job that skips
    #: "because it is not the hour yet" would be due at that same wrong minute the next day and
    #: skip forever. `interval()` still applies to the *overdue* warning in `make status`.
    daily_at_hour: Callable[[], int] | None = None

    @property
    def env_var(self) -> str:
        return f"SCHEDULER_{self.name.upper()}_HOURS"

    def interval(self) -> timedelta:
        hours = env_int(self.env_var, self.default_hours)
        if hours <= 0:
            raise ValueError(f"{self.env_var} must be a positive number of hours, got {hours}")
        return timedelta(hours=hours)

    def is_due(self, record: JobRun | None, now: datetime) -> bool:
        if self.daily_at_hour is not None:
            return self._due_today(record, now.astimezone(UTC))
        if record is None or record.last_started_at is None:
            return True
        return now - record.last_started_at >= self.interval()

    def _due_today(self, record: JobRun | None, now: datetime) -> bool:
        """Due once the send hour has come today and this job has not run since it came."""
        assert self.daily_at_hour is not None
        hour = max(0, min(23, self.daily_at_hour()))
        if now.hour < hour:
            return False
        if record is None or record.last_started_at is None:
            return True
        due_from = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        return record.last_started_at.astimezone(UTC) < due_from


def _call(command: str, **options: Any) -> list[str]:
    out = StringIO()
    try:
        call_command(command, stdout=out, stderr=out, **options)
    finally:
        logger.debug("%s output:\n%s", command, out.getvalue())
    return [
        line.strip()
        for line in out.getvalue().splitlines()
        if line.strip() and not line[0].isspace() and not line.startswith("NOT matched:")
    ]


def _truncate(message: str) -> str:
    if len(message) <= MESSAGE_MAX_CHARS:
        return message
    return message[: MESSAGE_MAX_CHARS - 1] + "…"


def pending_backlog(site: Site) -> int:
    return CrawlTask.objects.filter(site=site, status=CrawlTask.Status.PENDING).count()


BacklogFallback = Callable[[Site], list[str]]


def _per_site(
    job_name: str, command: str, *, over_backlog: BacklogFallback | None = None, **options: Any
) -> str:
    """Run a crawler command per enabled site, skipping sites whose queue is too long.

    `over_backlog` runs instead for a skipped site (a small, bounded batch) and returns summary
    lines."""
    results: list[str] = []
    skipped: list[str] = []
    for site in Site.objects.filter(enabled=True).order_by("domain"):
        pending = pending_backlog(site)
        if pending > BACKLOG_THRESHOLD:
            logger.info(
                "%s: skipped %s (%d pending crawl tasks > %d)",
                job_name,
                site.domain,
                pending,
                BACKLOG_THRESHOLD,
            )
            skipped.append(site.domain)
            if over_backlog is not None:
                results.extend(over_backlog(site))
            continue
        for line in _call(command, site=site.domain, **options):
            results.append(line if line.startswith(site.domain) else f"{site.domain}: {line}")
    parts = results or ["no site enqueued"]
    if skipped:
        parts.append(f"backlog skipped: {', '.join(skipped)}")
    return "; ".join(parts)


def run_crawl_seeds(context: JobContext) -> str:
    return _per_site("crawl_seeds", "crawl_seed", refresh=True)


def never_checked_batch(site: Site) -> list[str]:
    """Pages never availability-checked must not wait forever behind a backlog: queue a bounded
    batch of them at reviewer-recheck priority even while the site is over the threshold."""
    lines = _call(
        "crawl_recheck",
        site=site.domain,
        never_checked=True,
        limit=NEVER_CHECKED_BATCH,
        priority=RECHECK_PRIORITY,
    )
    logger.info(
        "crawl_recheck: %s over backlog, never-checked batch (limit %d, priority %d): %s",
        site.domain,
        NEVER_CHECKED_BATCH,
        RECHECK_PRIORITY,
        "; ".join(lines),
    )
    prefix = f"{site.domain}: "
    return [f"{prefix}never-checked batch: {line.removeprefix(prefix)}" for line in lines]


def run_crawl_recheck(context: JobContext) -> str:
    return _per_site(
        "crawl_recheck",
        "crawl_recheck",
        over_backlog=never_checked_batch,
        older_than_hours=RECHECK_OLDER_THAN_HOURS,
    )


def run_crawl_recheck_removed(context: JobContext) -> str:
    # Not behind the backlog guard: the batch is small and queued below the backlog's details.
    results: list[str] = []
    for site in Site.objects.filter(enabled=True).order_by("domain"):
        lines = _call(
            "crawl_recheck",
            site=site.domain,
            only_removed=True,
            older_than_hours=REMOVED_RECHECK_OLDER_THAN_HOURS,
            limit=REMOVED_RECHECK_BATCH,
            priority=REMOVED_RECHECK_PRIORITY,
        )
        results.extend(
            line if line.startswith(site.domain) else f"{site.domain}: {line}" for line in lines
        )
    return "; ".join(results) or "no site enqueued"


def discovery_scheduled_enabled(context: JobContext) -> str | None:
    if not settings.DISCOVERY_SCHEDULE_ENABLED:
        return "scheduled discovery is off (DISCOVERY_SCHEDULE_ENABLED)"
    if not settings.DATAFORSEO_LOGIN or not settings.DATAFORSEO_PASSWORD:
        return "DataForSEO credentials not configured (DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD)"
    return None


def run_discovery(context: JobContext) -> str:
    # Issue #20: the operator's own `discover_sources` (no --creator/--query) planned for every
    # target creator (ADR 0029) in the default market, propose-only (ADR 0013) — it only ever
    # writes a `proposed` SourceCandidate, never an allowlisted or enabled Site. Every planned
    # query is refused up front if it hits the safety terms, minor near misses or German
    # non-consent wording (ADR 0013 amendment 2026-09-17), same as a manual run. The spend guard
    # (DISCOVERY_MAX_CALLS_PER_RUN per run, DISCOVERY_DAILY_BUDGET_USD as a rolling 24h total
    # shared with every other caller) is the command's own, not reimplemented here.
    return "; ".join(_call("discover_sources")) or "done"


def targeted_indexing_enabled(context: JobContext) -> str | None:
    if settings.TARGETED_INDEXING_ENABLED:
        return None
    return "targeted indexing is off (TARGETED_INDEXING_ENABLED)"


def run_sync_targets(context: JobContext) -> str:
    # Deactivate untargeted pages, reactivate pages of new targets, rebuild breadcrumbs (#71).
    lines = _call("deactivate_untargeted", apply=True)
    return next((line for line in lines if line.startswith("Applied:")), "; ".join(lines))


def run_build_clusters(context: JobContext) -> str:
    return "; ".join(_call("build_clusters")) or "done"


def run_build_common_frames(context: JobContext) -> str:
    return "; ".join(_call("build_common_frames")) or "done"


def run_match_references(context: JobContext) -> str:
    # Incremental by default: only new page frames and new references are scanned (ADR 0015).
    lines = _call("match_references")
    counts = [match.groups() for line in lines if (match := REFERENCE_SUMMARY.fullmatch(line))]
    if counts:
        pages = sum(int(count[0]) for count in counts)
        strong = sum(int(count[1]) for count in counts)
        weak = sum(int(count[2]) for count in counts)
        total = (
            f"Total: {len(counts)} references, {pages} pages matched ({strong} strong, {weak} weak)"
        )
        return total + "\n" + "\n".join(lines)
    return "; ".join(lines) or "done"


def _new_frame_hashes(since: datetime) -> bool:
    # A page reactivated since (issue #71) brings frames matching has not seen yet.
    return (
        FrameHash.objects.filter(created_at__gte=since).exists()
        or Page.objects.filter(active=True, active_changed_at__gte=since).exists()
    )


def new_frame_hashes(context: JobContext) -> str | None:
    if context.last_succeeded_at is None or _new_frame_hashes(context.last_succeeded_at):
        return None
    return "no new frame hashes since last successful run"


def _matching_inputs_changed(since: datetime) -> bool:
    """Changes that alter match evidence without adding a frame or a reference.

    Cluster memberships and mirror corroboration, human common-frame labels (only confirmed groups
    discount evidence), calibrated band thresholds and human decisions (a rejected pair stops
    seeding cluster corroboration). The incremental scan makes such a run cheap. Match rows are
    rewritten by every run, so their decisions count only after the last run *finished*. A match
    whose copy extent another classifier version wrote (issue #62) needs a run too, or a deploy
    that changes the classifier would wait for the next new frame.
    """
    finished = (
        JobRun.objects.filter(name="match_references")
        .values_list("last_finished_at", flat=True)
        .first()
    )
    decided_since = max(since, finished) if finished is not None else since
    return (
        ClusterMember.objects.filter(updated_at__gte=since).exists()
        or CommonFrameGroup.objects.filter(labeled_at__gte=since).exists()
        or ConfidenceSettings.objects.filter(updated_at__gte=since).exists()
        or ReferenceLeak.objects.exclude(status="proposed").filter(updated_at__gte=since).exists()
        or ReferenceMatch.objects.exclude(status="proposed")
        .filter(updated_at__gt=decided_since)
        .exists()
        or ReferenceMatch.objects.exclude(copy_extent_version=CLASSIFIER_VERSION).exists()
    )


def new_frame_hashes_or_references(context: JobContext) -> str | None:
    if not Reference.objects.exists():
        return "no references"
    since = context.last_succeeded_at
    if (
        since is None
        or _new_frame_hashes(since)
        or Reference.objects.filter(created_at__gte=since).exists()
        or _matching_inputs_changed(since)
    ):
        return None
    return (
        "no new frame hashes, references, clusters, common-frame labels, thresholds or decisions "
        "since last successful run"
    )


def crawler_gate_signals() -> GateSignals:
    """Age gates the crawler skipped (ADR 0007 amendment), for browser-capture triage (ADR 0016).

    Skipped tasks keep the gate label in `decision_reason`; a detail task's URL is the page URL.
    A verification provider seen anywhere on a site outranks a self-declaration gate there.
    """
    by_url: dict[str, str] = {}
    sites: dict[int, str] = {}
    rows = CrawlTask.objects.filter(status=CrawlTask.Status.SKIPPED).filter(
        Q(decision_reason__startswith=AGE_SELF_DECLARATION)
        | Q(decision_reason__startswith=AGE_VERIFICATION_PROVIDER)
    )
    for site_id, url, reason in rows.values_list("site_id", "url", "decision_reason"):
        label = (
            AGE_VERIFICATION_PROVIDER
            if reason.startswith(AGE_VERIFICATION_PROVIDER)
            else AGE_SELF_DECLARATION
        )
        by_url[url] = label
        if sites.get(site_id) != AGE_VERIFICATION_PROVIDER:
            sites[site_id] = label
    pages = {
        page_id: by_url[url]
        for url, page_id in Page.objects.filter(url__in=list(by_url)).values_list("url", "pk")
    }
    return GateSignals(pages=pages, sites=sites)


def run_browser_capture_triage(context: JobContext) -> str:
    # Only refreshes the queue; nothing captures. Runs whatever BROWSER_CAPTURE_ENABLED says
    # (ADR 0016): triage reads the database and writes queue rows only.
    return "; ".join(_call("triage_browser_capture", gate_signals=crawler_gate_signals())) or "done"


#: Registry lookups per daily run. Small: records last HOSTING_REFRESH_DAYS, and the RDAP servers
#: are someone else's infrastructure.
LOOKUP_HOSTING_LIMIT = 25


def run_lookup_hosting(context: JobContext) -> str:
    # DNS and RDAP registries only, never the hosts themselves (ADR 0027).
    return "; ".join(_call("lookup_hosting", limit=LOOKUP_HOSTING_LIMIT)) or "done"


def run_observe_notice_removals(context: JobContext) -> str:
    # Stamps come from availability checks already made; this job makes no request itself.
    return f"{observe_notice_removals()} notice(s) observed removed"


def run_request_accounting(context: JobContext) -> str:
    # Makes no request: records the hosts media URLs point to (origin or CDN, issue #74) and rolls
    # hourly request counters past their retention up into daily rows.
    hosts = record_observed_hosts(media_hosts_by_site())
    moved = rollup_request_counts(context.now)
    return f"{hosts} new site host(s); {moved} hourly request counter row(s) rolled up"


def run_adapter_health(context: JobContext) -> str:
    """Judge every enabled site's adapter from the counters the crawlers wrote (issue #22).

    Reads and writes counters only — no request, no page text, no model. It is what turns a
    silent parser break into an operator alert, and it is also the gate the LLM repair reads
    (ADR 0042): nothing may be asked of a model for a site this pass has not recorded broken.
    """
    reports = adapter_health.evaluate_all(now=context.now)
    broken = [report for report in reports if report.status == AdapterHealth.Status.BROKEN]
    notified = adapter_alerts.notify_broken(reports)
    names = ", ".join(report.domain for report in broken) or "none"
    return f"{len(reports)} adapter(s) judged, {len(broken)} broken ({names}), {notified} alerted"


def run_catalogue_share(context: JobContext) -> str:
    """How much of each site's catalogue we hold, and one mail per new crossing (issue #107).

    Two counts per site and a comparison against a stored total. No request, no page text, and no
    change to any crawl: the threshold is a monitoring number, not a switch (ADR 0049). A site
    whose total is unknown is simply not measured — that is the honest answer, and
    `manage.py catalogue_size` says how many sites are in that state.
    """
    records, alerted = catalogue_share.evaluate(now=context.now)
    over = sum(1 for record in records if over_threshold(record))
    return f"{len(records)} site(s) with a known total, {over} over the threshold, {alerted} mailed"


def run_sync_cases(context: JobContext) -> str:
    # Reconciles every organisation's customer cases (ADR 0025) from the fresh matches and
    # decisions. Unguarded: removals and reopenings follow availability checks, not matching.
    return "; ".join(_call("sync_cases")) or "done"


def run_rights_housekeeping(context: JobContext) -> str:
    # Rights model (ADR 0034): works under ended mandates or schedules expire, then closed
    # records past retention are deleted with their log and encrypted proofs. No requests.
    return "; ".join(_call("rights_housekeeping")) or "done"


def run_invoice_retention(context: JobContext) -> str:
    # Billing (ADR 0032 amendment 2026-09-17): issued invoices/cancellations past their
    # eight-year retention (counted from the end of their issuing year) are deleted with their
    # stored PDF. No requests.
    return "; ".join(_call("invoice_retention")) or "done"


def safety_retention_enabled(context: JobContext) -> str | None:
    if settings.SAFETY_RETENTION_ENABLED:
        return None
    return "safety retention is off (SAFETY_RETENTION_ENABLED)"


def run_safety_retention(context: JobContext) -> str:
    # docs/safety.md, issue #100: closes a reported safety incident the hotline never came back on
    # after close_after_days (90), then deletes an incident closed longer than retain_months (12)
    # ago, with its URLs and any moved hashes. Both periods are constants in index.incidents, not
    # settings, so nothing is passed here — the command's own defaults apply. Deletion is
    # irreversible, so this is guarded like page_retention: the operator reads
    # `safety_retention --dry-run` and turns it on once by hand. The command already prints counts
    # and incident ids only, never a URL or page content, so this job repeats that output verbatim.
    return "; ".join(_call("safety_retention")) or "done"


def page_retention_enabled(context: JobContext) -> str | None:
    if settings.PAGE_RETENTION_ENABLED:
        return None
    return "page retention is off (PAGE_RETENTION_ENABLED)"


def run_page_retention(context: JobContext) -> str:
    # ADR 0029 amendment 2026-09-17: pages deactivated (untargeted) longer than
    # index.retention.PAGE_RETENTION_MONTHS are deleted outright. Deletion is irreversible, so
    # this is guarded like sync_targets: the operator reviews `page_retention --dry-run` and
    # turns it on once by hand.
    return "; ".join(_call("page_retention", apply=True)) or "done"


def customer_data_deletion_enabled(context: JobContext) -> str | None:
    if settings.CUSTOMER_DATA_DELETION_ENABLED:
        return None
    return "customer data deletion is off (CUSTOMER_DATA_DELETION_ENABLED)"


def run_customer_data_retention(context: JobContext) -> str:
    # ADR 0045 (issue #85): an organisation whose pilot ended, and an original marked for removal,
    # are deleted CUSTOMER_DATA_RETENTION_DAYS after the date staff set. Irreversible, so it is
    # guarded like page_retention: the operator reads `customer_data_retention --dry-run` and
    # turns the flag on once by hand. Invoices, legal holds and the index itself are never touched.
    return "; ".join(_call("customer_data_retention", apply=True)) or "done"


def research_retention_enabled(context: JobContext) -> str | None:
    if settings.RESEARCH_RETENTION_ENABLED:
        return None
    return "research retention is off (RESEARCH_RETENTION_ENABLED)"


def run_research_retention(context: JobContext) -> str:
    # ADR 0048 (issues #103, #104): the discovery query text past its period is blanked (the row
    # stays — it is ADR 0013's spend ledger), prospect research nobody acted on is forgotten, and a
    # catalogue name nothing uses any more is deleted. Guarded like page_retention and
    # customer_data_retention: the operator reads `research_retention --dry-run` and turns it on
    # once by hand. It runs last of all, because page retention and customer-data deletion both
    # free names that this pass then judges on its next run.
    return "; ".join(_call("research_retention", apply=True)) or "done"


def invite_retention_enabled(context: JobContext) -> str | None:
    if settings.INVITE_RETENTION_ENABLED:
        return None
    return "invite retention is off (INVITE_RETENTION_ENABLED)"


def run_invite_retention(context: JobContext) -> str:
    # ADR 0052 amendment: an unredeemed accounts.CustomerInvite (expired, revoked, abandoned) is
    # deleted ONBOARDING_INVITE_RETENTION_DAYS after it stopped being usable. A redeemed invite is
    # never touched here -- it cascades away with its organisation. Guarded like page_retention and
    # research_retention: the operator reads `invite_retention --dry-run` and turns it on once by
    # hand. No ordering dependency on the crawler/matching jobs above: accounts data is its own
    # thing.
    return "; ".join(_call("invite_retention", apply=True)) or "done"


def upload_spool_configured(context: JobContext) -> str | None:
    from marceline.uploads import spool

    if spool.available():
        return None
    return "no upload spool is configured (CLOUD_UPLOAD_SPOOL_DIR)"


def run_upload_spool(context: JobContext) -> str:
    # Cloud fingerprinting (issue #59, ADR 0044): delete every spooled upload past the hard TTL,
    # whatever the database believes, and mark the rows that were still open. It runs here as
    # well as in `fingerprint_worker` on purpose — a stopped or crashed worker must not be the
    # only thing between a customer's file and its deletion. No requests, no decoding.
    from django.utils import timezone

    from marceline.models import MediaUpload
    from marceline.uploads import spool

    reaped = spool.reap()
    if not reaped:
        return "nothing past the spool TTL"
    MediaUpload.objects.filter(public_id__in=reaped, status__in=MediaUpload.OPEN_STATUSES).update(
        status=MediaUpload.Status.EXPIRED,
        result_code="expired",
        bytes_deleted_at=timezone.now(),
    )
    return f"reaped {len(reaped)} upload(s)"


def run_safety_release_queue(context: JobContext) -> str:
    # Customer release queue (docs/safety.md): who counts as a customer creator changes with
    # ownership, not with crawls, so blocked "flagged term" pages are re-decided on a timer too.
    # Reads text already stored; no requests, and it only queues or dequeues — never releases.
    report = sync_release_queue(now=context.now)
    return (
        f"{report.queued} queued ({report.newly_queued} new), {report.dequeued} dequeued, "
        f"{report.uncredited} without customer credit, {report.other_hits} with another hit"
    )


def run_safety_digest(context: JobContext) -> str:
    """The one daily operator report (crawler/safety_digest.py): safety, then crawler.

    The window runs from the last successful run of this job, so downtime costs no event and
    repeats none. Sent once a day even on a fully quiet window (2026-09-19) — its own absence is
    what tells the operator the reporting pipeline stopped, not a quiet day. Sends no request;
    reads pages, rule decisions, incidents, adapter health and robots policy rows only.
    """
    start, end = safety_digest.window(context.now, context.last_succeeded_at)
    return safety_digest.send_digest(start, end)


def safety_digest_hour() -> int:
    return int(settings.SAFETY_DIGEST_HOUR_UTC)


def new_matches_leaks_or_flags(context: JobContext) -> str | None:
    since = context.last_succeeded_at
    if (
        since is None
        or ReferenceMatch.objects.filter(updated_at__gte=since).exists()
        or ReferenceLeak.objects.filter(updated_at__gte=since).exists()
        or Page.objects.filter(safety_flagged_at__gte=since).exists()
    ):
        return None
    return "no new or changed matches, leaks or safety flags since last successful run"


# Order matters: jobs due in the same pass run top to bottom, so page activity is current before
# anything is matched (issue #71), matches get cluster links, and
# common frames (which count clusters as one video and discount match evidence) are rebuilt first.
# Customer cases are reconciled right after matching, so production reads with
# CASE_SYNC_ON_READ off (ADR 0028) see new confirmations within one interval.
# Browser-capture triage reads matches and leaks, so it runs after matching. Page retention runs
# last: it only ever deletes pages already deactivated by sync_targets, long past their clock.
# Discovery (issue #20) sits with the other acquisition jobs, before matching starts: it proposes
# candidate domains only, so it has no ordering dependency on clusters or matches.
# Customer data deletion (ADR 0045) runs after page retention, last of all: it deletes what a
# customer registered, so every job that might still read an original has run for this pass.
# Safety retention (issue #100) sits right before page retention: it only closes stale reported
# incidents and deletes incidents already closed, never a page, so nothing else in this list
# depends on it — but page_retention's own guard excludes a page still attached to an open or
# reported incident, and running safety retention first means a case closed in this same pass
# already lifts that exclusion, instead of waiting for tomorrow's run.
JOBS: tuple[Job, ...] = (
    # First: a broken adapter is a data problem, and every hour it goes unnoticed is crawl budget
    # spent on empty pages. It reads counters the crawlers already wrote and makes no request.
    Job("adapter_health", 6, run_adapter_health),
    Job("crawl_seeds", 24, run_crawl_seeds),
    Job("crawl_recheck", 24, run_crawl_recheck),
    Job("crawl_recheck_removed", 7 * 24, run_crawl_recheck_removed),
    Job("discovery", 24, run_discovery, guard=discovery_scheduled_enabled),
    Job("sync_targets", 3, run_sync_targets, guard=targeted_indexing_enabled),
    Job("build_clusters", 3, run_build_clusters, guard=new_frame_hashes),
    Job("build_common_frames", 24, run_build_common_frames, guard=new_frame_hashes),
    Job("match_references", 3, run_match_references, guard=new_frame_hashes_or_references),
    Job("sync_cases", 3, run_sync_cases),
    Job(
        "browser_capture_triage",
        24,
        run_browser_capture_triage,
        guard=new_matches_leaks_or_flags,
    ),
    Job("lookup_hosting", 24, run_lookup_hosting),
    Job("observe_notice_removals", 24, run_observe_notice_removals),
    Job("request_accounting", 24, run_request_accounting),
    # Issue #107: our share of each site's own catalogue, and one mail per new crossing. Counts
    # only, no request, and it changes nothing about any crawl.
    Job("catalogue_share", 24, run_catalogue_share),
    Job("safety_release_queue", 3, run_safety_release_queue),
    # Hourly, and guarded only by the spool existing: this is the backstop that makes ADR 0044's
    # retention statement true when a worker dies mid-job.
    Job("upload_spool", 1, run_upload_spool, guard=upload_spool_configured),
    Job("safety_digest", 24, run_safety_digest, daily_at_hour=safety_digest_hour),
    # Accounts data, not crawler/matching data: no ordering dependency on anything above or on the
    # fixed tail below (`test_rights_housekeeping_runs_daily_before_invoice_and_page_retention`,
    # `test_customer_data_deletion.py` both pin that tail's exact six names by position) -- placed
    # before it, rather than after, purely to leave that block's indices alone.
    Job("invite_retention", 24, run_invite_retention, guard=invite_retention_enabled),
    Job("rights_housekeeping", 24, run_rights_housekeeping),
    Job("invoice_retention", 24, run_invoice_retention),
    Job("safety_retention", 24, run_safety_retention, guard=safety_retention_enabled),
    Job("page_retention", 24, run_page_retention, guard=page_retention_enabled),
    Job(
        "customer_data_retention",
        24,
        run_customer_data_retention,
        guard=customer_data_deletion_enabled,
    ),
    # Truly last: both jobs above delete rows that keep a name in use, so the research side is
    # judged after them (ADR 0048).
    Job("research_retention", 24, run_research_retention, guard=research_retention_enabled),
)


def run_job(job: Job, clock: Clock) -> JobRun:
    """Run one job, isolating failures, and persist its state."""
    record, _ = JobRun.objects.get_or_create(name=job.name)
    started_at = clock()
    context = JobContext(now=started_at, last_succeeded_at=record.last_succeeded_at)
    # Persist the start first so a crashing job is not retried in a restart loop.
    record.last_started_at = started_at
    record.save(update_fields=("last_started_at",))
    try:
        reason = job.guard(context) if job.guard is not None else None
        if reason is not None:
            status, message = JobRun.Status.SKIPPED, reason
            logger.info("%s: skipped (%s)", job.name, reason)
        else:
            logger.info("%s: started", job.name)
            message = _truncate(job.run(context))
            status = JobRun.Status.OK
            logger.info("%s: finished: %s", job.name, message)
    except Exception as exc:
        status, message = JobRun.Status.FAILED, f"{type(exc).__name__}: {exc}"
        logger.exception("%s: failed", job.name)
    record.last_finished_at = clock()
    record.last_status = status
    record.last_message = _truncate(message)
    if status == JobRun.Status.OK:
        record.last_succeeded_at = started_at
    record.save()
    return record


def run_due_jobs(jobs: Sequence[Job], clock: Clock = timezone.now) -> list[JobRun]:
    """Run every due job once, in declaration order."""
    records = {r.name: r for r in JobRun.objects.filter(name__in=[job.name for job in jobs])}
    return [run_job(job, clock) for job in jobs if job.is_due(records.get(job.name), clock())]


@contextmanager
def single_instance_lock() -> Iterator[bool]:
    """Hold a session-level Postgres advisory lock; yields False if another process holds it."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        row = cursor.fetchone()
    acquired = bool(row and row[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])


def run_scheduler(
    jobs: Sequence[Job],
    *,
    once: bool,
    clock: Clock = timezone.now,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = POLL_SECONDS,
) -> bool:
    """Loop over due jobs until stopped; returns False if another scheduler is running."""
    with single_instance_lock() as acquired:
        if not acquired:
            logger.warning("another scheduler holds the lock; exiting")
            return False
        logger.info(
            "scheduler started: %s",
            ", ".join(f"{job.name} every {job.interval()}" for job in jobs),
        )
        while True:
            run_due_jobs(jobs, clock)
            if once:
                return True
            sleep(poll_seconds)
