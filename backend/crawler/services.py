"""Crawl queue and index writes: every database workflow of the crawler lives here."""

import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from urllib.parse import urldefrag, urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import (
    Exists,
    F,
    Func,
    JSONField,
    OuterRef,
    Q,
    QuerySet,
    TextField,
    Value,
)
from django.db.models.functions import Cast, Greatest
from django.utils import timezone

from catalog import request_accounting, robots_policy, safety_rules
from catalog.models import Creator, SafetyRuleSet, SafetyTerm, Site
from core.log_scrub import scrub_urls
from crawler import alerts, safety, safety_score
from crawler.listing_walk import TitleGate, WalkPlan, walk_context, walk_links, walk_of
from crawler.models import CrawlTask
from crawler.sites.base import (
    EXPIRING_DETAIL_PRIORITY,
    SEED_PRIORITY,
    MediaSpec,
    PageData,
    ParseResult,
    TaskSpec,
    dropped_performer_names,
    is_compilation,
)
from crawler.sites.common import creator_names, normalize_name
from index import signals
from index.breadcrumbs import record_page_links, recount_breadcrumbs
from index.incidents import RetainedHash, collect_page_hashes
from index.models import FrameHash, Media, Page, SafetyRuleDecision
from index.retention import stays_scrubbed, without_performer_credits
from index.services import (
    SAFETY_GATE_REASON,
    allow_page_media,
    cancel_page_media,
    gate_page_media,
    log_flag,
    page_media_allowed,
    release_page_media,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = timedelta(minutes=1)
RETRY_MAX_DELAY = timedelta(hours=6)
STALE_LOCK_AFTER = timedelta(minutes=15)
# Connection failures in a row after which a site counts as down and is paused (review M6). One
# unreadable robots.txt pauses at once: without it nothing on the site may be fetched anyway.
SITE_PAUSE_AFTER_FAILURES = 3
URL_MAX_LENGTH = 2000
ERROR_MAX_LENGTH = 5000
DEAD_DETAIL_COOLDOWN = timedelta(days=30)
# Availability rechecks a reviewer asked for (and the scheduler's never-checked batch): ahead
# of seeds and ordinary details, behind details whose tokenized media links expire within the hour.
RECHECK_PRIORITY = SEED_PRIORITY + 5
# How many pages `recheck_pages` queues per transaction. Big enough that the commits stop
# dominating the run, small enough that the batch's row locks (`enqueue` locks the pending
# DETAIL tasks it touches) never hold a claiming worker up for long.
RECHECK_BATCH = 200
# Expired-media recovery (tokenized site03 CDN links die within the hour, see
# docs/sources/site03.example.md). Skip reason written by matching.media_worker for 403/404/410
# answers on tokenized links; repeated here because crawler may not import matching.
LINK_EXPIRED = "link expired or gone"
MAX_MEDIA_RECOVERIES = 2
# Tasks one recovery run may put back into the queue, across all pages (review H3): recovery
# spends detail fetches from the daily budget, so it must never crowd out ordinary crawling.
MAX_RECOVERY_TASKS_PER_RUN = 50
MEDIA_RECOVERIES_KEY = "media_recoveries"
EXPIRED_CONTEXT_KEY = "expired_context_at"
# The page a detail task stored when its canonical URL differs from the task URL (review L10), so
# a later 404 on the task URL still finds the page to mark removed.
PAGE_URL_KEY = "page_url"
# Tokenized media links (matching.media.EXPIRING_URL_REGEX). Only these expire; a plain media URL
# that answers 404 is gone for good and never triggers a re-crawl (review H3).
EXPIRING_URL_REGEX = r"/(?:vid|img)/[^/]+/([0-9]{10})/"
EXPIRING_URL = re.compile(EXPIRING_URL_REGEX)
MEDIA_RECOVERY_WINDOW = timedelta(days=2)
_USABLE_MEDIA = (Media.Status.DONE, Media.Status.PENDING, Media.Status.RUNNING)
_ACTIVE_TASK = (CrawlTask.Status.PENDING, CrawlTask.Status.RUNNING)


def normalize_url(url: str) -> str:
    return urldefrag(url.strip()).url


def is_http_url(url: str) -> bool:
    return urlsplit(url).scheme in {"http", "https"} and len(url) <= URL_MAX_LENGTH


def is_on_site(url: str, domain: str) -> bool:
    """True for http(s) URLs on `domain` or one of its subdomains."""
    host = (urlsplit(url).hostname or "").lower()
    domain = domain.lower()
    return is_http_url(url) and (host == domain or host.endswith(f".{domain}"))


def _token_expiry(url: str) -> int | None:
    match = EXPIRING_URL.search(urlsplit(url).path)
    return int(match[1]) if match else None


def _card_priority(spec: TaskSpec) -> int:
    if spec.kind == CrawlTask.Kind.DETAIL and any(
        _token_expiry(media.url) is not None for media in spec.media
    ):
        return max(spec.priority, EXPIRING_DETAIL_PRIORITY)
    return spec.priority


@transaction.atomic
def enqueue(
    site: Site,
    specs: Iterable[TaskSpec],
    *,
    discovered_from: CrawlTask | None = None,
    refresh: bool = False,
    now: datetime | None = None,
    context: Mapping[str, object] | None = None,
) -> int:
    """Create tasks for new on-site URLs; returns how many were created.

    With `refresh`, finished tasks for the same URLs are put back into the queue (used by seeds).
    Unindexed detail 404/410 outcomes suppress refresh for DEAD_DETAIL_COOLDOWN.
    Keep skipped task rows: they are the tombstones for these URLs.
    `context` is added to every task created here — how a listing walk hands itself down
    (`crawler.listing_walk`); an existing task's context is never rewritten by it.
    """
    by_url: dict[str, TaskSpec] = {}
    for spec in specs:
        url = normalize_url(spec.url)
        if is_on_site(url, site.domain):
            by_url.setdefault(url, spec)
    if not by_url:
        return 0

    existing = set(CrawlTask.objects.filter(url__in=by_url).values_list("url", flat=True))
    new_tasks = [
        CrawlTask(
            site=site,
            url=url,
            kind=spec.kind,
            priority=_card_priority(spec),
            discovered_from=discovered_from,
            context={**(context or {}), **_media_context(spec.media)},
        )
        for url, spec in by_url.items()
        if url not in existing
    ]
    CrawlTask.objects.bulk_create(new_tasks, ignore_conflicts=True)
    # Lock against claims; preserve recovery counters and unrelated hints.
    pending = CrawlTask.objects.select_for_update().filter(
        site=site, url__in=by_url, kind=CrawlTask.Kind.DETAIL, status=CrawlTask.Status.PENDING
    )
    for task in pending:
        spec = by_url[task.url]
        if spec.kind != CrawlTask.Kind.DETAIL or not spec.media:
            continue
        context = task.context if isinstance(task.context, dict) else {}
        task.context = {**context, **_media_context(spec.media)}
        task.priority = max(task.priority, _card_priority(spec))
        if discovered_from is not None:
            task.discovered_from = discovered_from
        task.save(update_fields=("context", "priority", "discovered_from", "updated_at"))
    if refresh and existing:
        now = now or timezone.now()
        dead_urls = (
            CrawlTask.objects.filter(
                url__in=existing,
                kind=CrawlTask.Kind.DETAIL,
                status=CrawlTask.Status.SKIPPED,
                decision_reason__in=("HTTP 404", "HTTP 410"),
                fetched_at__gt=now - DEAD_DETAIL_COOLDOWN,
            )
            .exclude(url__in=Page.objects.filter(site=site).values("url"))
            .values("url")
        )
        CrawlTask.objects.filter(url__in=existing).exclude(url__in=dead_urls).exclude(
            status__in=(CrawlTask.Status.PENDING, CrawlTask.Status.RUNNING)
        ).update(
            status=CrawlTask.Status.PENDING,
            attempts=0,
            next_attempt_at=None,
            updated_at=now or timezone.now(),
        )
    return len(new_tasks)


def enqueue_seeds(
    site: Site,
    specs: Iterable[TaskSpec],
    *,
    refresh: bool = False,
    context: Mapping[str, object] | None = None,
) -> int:
    """`enqueue` for start URLs, then give every pending one its spec's priority.

    A seed's priority says whose seed it is (Auftrag or watchlist creator, issue #71), and that can
    change between runs, so a re-queued or still pending seed takes the new value. A URL named
    twice keeps its highest priority. Returns how many tasks were created.
    """
    by_url: dict[str, TaskSpec] = {}
    for spec in specs:
        url = normalize_url(spec.url)
        if url not in by_url or spec.priority > by_url[url].priority:
            by_url[url] = spec
    created = enqueue(site, by_url.values(), refresh=refresh, context=context)
    by_priority: dict[int, list[str]] = {}
    for url, spec in by_url.items():
        by_priority.setdefault(spec.priority, []).append(url)
    for priority, urls in by_priority.items():
        CrawlTask.objects.filter(site=site, url__in=urls, status=CrawlTask.Status.PENDING).exclude(
            kind=CrawlTask.Kind.DETAIL
        ).exclude(priority=priority).update(priority=priority, updated_at=timezone.now())
    return created


@dataclass(frozen=True)
class WalkStatus:
    """What `enqueue_listing_walks` did, per walk (its start URL), for the operator's report."""

    created: int = 0
    started: tuple[str, ...] = ()  # newly queued, or re-queued with --refresh
    resumed: tuple[str, ...] = ()  # the frontier had died; it is back in the queue
    walking: tuple[str, ...] = ()  # a frontier task is already pending or running
    complete: tuple[str, ...] = ()  # the walk reached the end of the catalogue


def enqueue_listing_walks(
    site: Site,
    specs: Iterable[TaskSpec],
    *,
    refresh: bool = False,
    now: datetime | None = None,
) -> WalkStatus:
    """Start or resume this site's breadth-first listing walks (`crawler.listing_walk`).

    One walk per broad start URL, identified by that URL in `CrawlTask.context`. A walk is a
    single pending task at a time, so there is nothing to do while one is pending or running; a
    walk whose frontier was skipped (`deactivate_untargeted --stop-broad-listings`) or failed is
    put back into the queue where it stopped, never at page 1. A frontier that is simply done
    with no successor is the end of the catalogue, and is reported rather than re-fetched —
    `--refresh` is how the operator walks the newest page again.
    """
    now = now or timezone.now()
    status = WalkStatus()
    for spec in specs:
        walk = normalize_url(spec.url)
        if not is_on_site(walk, site.domain):
            continue
        adopted = _adopt_walk_root(site, walk, now=now)
        created = enqueue_seeds(site, [spec], refresh=refresh, context=walk_context(walk))
        status = replace(status, created=status.created + created)
        chain = _walk_tasks(site, walk)
        if created or adopted or refresh:
            status = replace(status, started=(*status.started, walk))
        elif chain.filter(status__in=_ACTIVE_TASK).exists():
            status = replace(status, walking=(*status.walking, walk))
        elif (frontier := chain.order_by("-updated_at").first()) is None:
            continue
        elif frontier.status == CrawlTask.Status.DONE:
            status = replace(status, complete=(*status.complete, frontier.url))
        else:
            _requeue_walk_task(frontier, now=now)
            status = replace(status, resumed=(*status.resumed, frontier.url))
    return status


def _walk_tasks(site: Site, walk: str) -> QuerySet[CrawlTask]:
    """The walk's own listing chain: its detail tasks are leaves and never its frontier."""
    return CrawlTask.objects.filter(site=site, context__listing_walk=walk).exclude(
        kind=CrawlTask.Kind.DETAIL
    )


def _adopt_walk_root(site: Site, walk: str, *, now: datetime) -> bool:
    """Make an existing, ungated chain root this walk's root, so `--listings` gates it from now on.

    Only the root of a chain queued before the walk existed (or by an adapter that has since
    declared `card_text`) is ever adopted; the normal case is that there is nothing to adopt. A
    finished root is re-queued, which starts the gated walk at page 1.
    """
    return _adopt_walk_task(CrawlTask.objects.filter(site=site, url=walk).first(), walk, now=now)


def _adopt_walk_task(task: CrawlTask | None, walk: str, *, now: datetime) -> bool:
    """Take a task an older, ungated chain left behind into this walk.

    A page that already has a task is never created again, so without this the walk would simply
    end at the first page the old chain had reached. A finished one is re-queued: walking the
    catalogue again, gated, is the point.
    """
    if task is None or walk_of(task.context) is not None:
        return False
    task.context = {
        **(task.context if isinstance(task.context, dict) else {}),
        **walk_context(walk),
    }
    if task.status in _ACTIVE_TASK:
        task.save(update_fields=("context", "updated_at"))
    else:
        _requeue_walk_task(task, now=now)
    return True


def adopt_walk_successors(
    site: Site, walk: str, specs: Sequence[TaskSpec], *, now: datetime | None = None
) -> None:
    """The next pages of a walk that an older, ungated chain already holds tasks for."""
    urls = [normalize_url(spec.url) for spec in specs if spec.kind != CrawlTask.Kind.DETAIL]
    for task in CrawlTask.objects.filter(site=site, url__in=urls):
        _adopt_walk_task(task, walk, now=now or timezone.now())


def _requeue_walk_task(task: CrawlTask, *, now: datetime) -> None:
    task.status = CrawlTask.Status.PENDING
    task.attempts = 0
    task.next_attempt_at = None
    task.locked_at = None
    task.decision_reason = ""
    task.updated_at = now
    task.save(
        update_fields=(
            "status",
            "attempts",
            "next_attempt_at",
            "locked_at",
            "decision_reason",
            "context",
            "updated_at",
        )
    )


def over_budget_site_ids(now: datetime, *, site_domain: str | None = None) -> list[int]:
    """Enabled sites (or the one named) whose real origin requests used their daily budget.

    Counted from the request counters (issue #74), not from crawl tasks: robots.txt, retries,
    redirect hops and media on the site's own hosts count too. The workers' request accounting
    refuses the individual request as well; this only keeps such sites' tasks from being claimed.
    """
    return request_accounting.over_budget_site_ids(now, site_domain=site_domain)


def claim_task(*, site_domain: str | None = None, now: datetime | None = None) -> CrawlTask | None:
    """Lock the most urgent available task and mark it RUNNING. Safe across worker processes."""
    now = now or timezone.now()
    with transaction.atomic():
        queryset = (
            CrawlTask.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("site")
            .filter(status=CrawlTask.Status.PENDING, site__enabled=True)
            .filter(Q(site__paused_until__isnull=True) | Q(site__paused_until__lte=now))
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            # A task that used up its attempts is never handed out again, whatever put it back.
            .filter(attempts__lt=MAX_ATTEMPTS)
            .exclude(site_id__in=over_budget_site_ids(now, site_domain=site_domain))
            .order_by("-priority", "created_at", "id")
        )
        if site_domain:
            queryset = queryset.filter(site__domain=site_domain)
        task = queryset.first()
        if task is None:
            return None
        task.status = CrawlTask.Status.RUNNING
        task.locked_at = now
        task.attempts += 1
        task.save(update_fields=("status", "locked_at", "attempts", "updated_at"))
        return task


def complete_task(task: CrawlTask, *, reason: str = "", now: datetime | None = None) -> None:
    now = now or timezone.now()
    task.decision_reason = reason
    task.status = CrawlTask.Status.DONE
    task.fetched_at = now
    task.locked_at = None
    task.next_attempt_at = None
    task.last_error = ""
    task.save(
        update_fields=(
            "status",
            "decision_reason",
            "fetched_at",
            "locked_at",
            "next_attempt_at",
            "last_error",
            "updated_at",
        )
    )


def skip_task(
    task: CrawlTask, reason: str, *, fetched: bool = True, now: datetime | None = None
) -> None:
    """Final state for URLs we must not or need not crawl (robots, blocks, 404, off-site)."""
    now = now or timezone.now()
    task.status = CrawlTask.Status.SKIPPED
    task.decision_reason = reason
    task.locked_at = None
    task.next_attempt_at = None
    if fetched:
        task.fetched_at = now
    task.save(
        update_fields=(
            "status",
            "decision_reason",
            "locked_at",
            "next_attempt_at",
            "fetched_at",
            "updated_at",
        )
    )


def retry_delay(attempts: int) -> timedelta:
    return min(RETRY_BASE_DELAY * (1 << max(attempts - 1, 0)), RETRY_MAX_DELAY)


def record_site_failure(
    site: Site, error: str, *, robots: bool = False, now: datetime | None = None
) -> datetime | None:
    """Count one connection (or robots.txt) failure; pause the site once it looks down.

    Returns the end of the pause when the site is (now) paused, else None. Pauses back off like
    task retries, from RETRY_BASE_DELAY up to RETRY_MAX_DELAY: the n-th pause in a row lasts
    RETRY_BASE_DELAY * 2^(n-1). The first task after a pause is the probe — if it fails again the
    site is paused again at once, for twice as long.
    """
    now = now or timezone.now()
    with transaction.atomic():
        failures = (
            Site.objects.select_for_update()
            .values_list("consecutive_failures", flat=True)
            .get(pk=site.pk)
            + 1
        )
        pauses = failures if robots else failures - SITE_PAUSE_AFTER_FAILURES + 1
        paused_until = now + retry_delay(pauses) if pauses >= 1 else None
        fields: dict[str, object] = {"consecutive_failures": failures}
        if paused_until is not None:
            fields |= {"paused_until": paused_until, "pause_reason": error[:ERROR_MAX_LENGTH]}
        Site.objects.filter(pk=site.pk).update(**fields)
    for key, value in fields.items():
        setattr(site, key, value)
    if paused_until is not None:
        # `error` may come from a third-party exception (httpx, robots.txt fetch failures) and
        # can quote the URL it was raised about; the full text is kept in `pause_reason` above,
        # the logged copy is not (issue: sanitise exception messages centrally, 2026-09-18).
        logger.warning(
            "%s paused until %s after %d failure(s) in a row: %s",
            site.domain,
            paused_until.isoformat(timespec="seconds"),
            failures,
            scrub_urls(error),
        )
    return paused_until


def record_site_reachable(site: Site) -> None:
    """The site answered: end its failure streak. Writes only when there is a streak to end.

    A pause for a **TDM reservation** (issue #105) is not an outage and is never ended by a
    successful fetch: it stands until the operator has reviewed the reservation. Only the failure
    streak is reset in that case.
    """
    if site.pause_reason.startswith(robots_policy.TDM_PAUSE_REASON):
        if site.consecutive_failures:
            Site.objects.filter(pk=site.pk).update(consecutive_failures=0)
            site.consecutive_failures = 0
        return
    if site.consecutive_failures == 0 and site.paused_until is None:
        return
    Site.objects.filter(pk=site.pk).update(
        consecutive_failures=0, paused_until=None, pause_reason=""
    )
    site.consecutive_failures, site.paused_until, site.pause_reason = 0, None, ""


def release_task(
    task: CrawlTask,
    error: str,
    *,
    not_before: datetime | None = None,
    now: datetime | None = None,
) -> None:
    """Hand a claimed task back untouched because its site is down (review M6) or its request
    budget is used up (issue #74; `not_before` = when the budget can free up again).

    The attempt is refunded: an outage of the site says nothing about this URL, and burning
    MAX_ATTEMPTS during a long outage used to fail tasks permanently.
    """
    now = now or timezone.now()
    task.status = CrawlTask.Status.PENDING
    task.attempts = max(task.attempts - 1, 0)
    task.locked_at = None
    task.next_attempt_at = not_before
    task.last_error = error[:ERROR_MAX_LENGTH]
    task.save(
        update_fields=(
            "status",
            "attempts",
            "locked_at",
            "next_attempt_at",
            "last_error",
            "updated_at",
        )
    )


def abandon_task(task: CrawlTask, exc: BaseException, *, now: datetime | None = None) -> None:
    """Final state for a task whose processing raised: FAILED with the error class, no retry."""
    fail_task(
        task, f"worker error: {type(exc).__name__}: {exc}", retry=False, fetched=False, now=now
    )


def fail_task(
    task: CrawlTask,
    error: str,
    *,
    retry: bool = True,
    fetched: bool = True,
    now: datetime | None = None,
) -> None:
    """Record an error; retry with exponential backoff until MAX_ATTEMPTS, then FAILED."""
    now = now or timezone.now()
    task.last_error = error[:ERROR_MAX_LENGTH]
    task.locked_at = None
    if fetched:
        task.fetched_at = now
    if retry and task.attempts < MAX_ATTEMPTS:
        task.status = CrawlTask.Status.PENDING
        task.next_attempt_at = now + retry_delay(task.attempts)
    else:
        task.status = CrawlTask.Status.FAILED
        task.next_attempt_at = None
    task.save(
        update_fields=(
            "status",
            "last_error",
            "locked_at",
            "next_attempt_at",
            "fetched_at",
            "updated_at",
        )
    )


def recover_stale_tasks(*, now: datetime | None = None) -> int:
    """Put RUNNING tasks whose worker died back into the queue.

    A task that already used MAX_ATTEMPTS is FAILED instead: a URL that kills its worker every
    time must not be retried for ever (review M5). Returns how many tasks were re-queued.
    """
    now = now or timezone.now()
    stale = CrawlTask.objects.filter(
        status=CrawlTask.Status.RUNNING, locked_at__lt=now - STALE_LOCK_AFTER
    )
    stale.filter(attempts__gte=MAX_ATTEMPTS).update(
        status=CrawlTask.Status.FAILED,
        locked_at=None,
        next_attempt_at=None,
        last_error=f"abandoned: the worker stopped while processing it {MAX_ATTEMPTS} times",
        updated_at=now,
    )
    return stale.update(status=CrawlTask.Status.PENDING, locked_at=None, updated_at=now)


def match_creators(
    texts: Iterable[str], creators: Iterable[Creator], *, ignore: Collection[str] = ()
) -> list[Creator]:
    """Creators whose name or alias appears as whole words in any of `texts`.

    Names in `ignore` (normalized) are no evidence: a creator found only through one of them is
    not matched.
    """
    haystacks = [f" {normalize_name(text)} " for text in texts if text]
    matched: list[Creator] = []
    for creator in creators:
        needles = [normalize_name(name) for name in creator_names(creator)]
        if any(
            f" {needle} " in haystack
            for needle in needles
            if needle and needle not in ignore
            for haystack in haystacks
        ):
            matched.append(creator)
    return matched


@dataclass(frozen=True)
class CreatorLinks:
    """Whom a page links (`linked`), and who its text names but its credit flags rule out."""

    linked: list[Creator]
    ruled_out: list[Creator]


def creator_links(
    title: str,
    tags: Sequence[str],
    performers: Sequence[str],
    extra: Mapping[str, object],
    creators: Iterable[Creator],
) -> CreatorLinks:
    """The creator links of a page from its text and the adapter's credit flags (issue #3). Pure.

    A compilation post links nobody: naming twenty performers is no evidence that any one of them
    is in the page. A name the adapter dropped as noise (a title word, a fragment of a longer
    name) links nobody on its own. Everything else links as `match_creators` says.
    """
    texts = [title, *tags, *performers]
    named = match_creators(texts, creators)
    if is_compilation(extra):
        return CreatorLinks(linked=[], ruled_out=named)
    ignored = {normalize_name(name) for name in dropped_performer_names(extra)}
    linked = match_creators(texts, named, ignore=ignored) if ignored else named
    return CreatorLinks(linked=linked, ruled_out=[c for c in named if c not in linked])


def unlink_creators(page: Page, creator_ids: Collection[int]) -> int:
    """Remove these creator links of `page` and recount their breadcrumbs. Returns links removed."""
    if not creator_ids:
        return 0
    removed, _ = Page.creators.through.objects.filter(
        page_id=page.pk, creator_id__in=creator_ids
    ).delete()
    if removed:
        recount_breadcrumbs(page.site_id, creator_ids)
    return removed


# Per-process caches of what every stored page is checked against (review PERF: two queries and a
# regex compile per page). Short on purpose: an edited safety term must reach every crawler within
# a few minutes. Saves and deletes in this process clear them at once (crawler/apps.py).
REFERENCE_CACHE_TTL_S = 120.0
_reference_cache: dict[str, tuple[float, object]] = {}
# Rule set versions whose `catalog.SafetyRuleSet` snapshot this process already stored.
_recorded_rule_sets: set[str] = set()


def clear_reference_caches() -> None:
    _reference_cache.clear()
    _recorded_rule_sets.clear()


def _cached[T](key: str, load: Callable[[], T]) -> T:
    now = time.monotonic()
    hit = _reference_cache.get(key)
    if hit is not None and now - hit[0] < REFERENCE_CACHE_TTL_S:
        return hit[1]  # type: ignore[return-value]
    value = load()
    _reference_cache[key] = (now, value)
    return value


def cached_safety_terms() -> list[safety.CompiledTerm]:
    """`load_safety_terms`, at most REFERENCE_CACHE_TTL_S old. Only for the crawl hot path."""
    return _cached("safety_terms", load_safety_terms)


def cached_creators() -> list[Creator]:
    """Linkable creators, at most REFERENCE_CACHE_TTL_S old, for linking crawled pages.

    `Creator.objects.usable()` (catalog) is the rule: a name a customer typed that staff have not
    reviewed yet (`Creator.needs_review`) links nothing, because it could be a typo or a common
    word that would tie thousands of pages to it — and a **suppressed** name links nothing, because
    someone asked us to stop indexing them (docs/suppression.md). The cache is short, so a
    suppression applied now reaches every crawler within two minutes; the database trigger on the
    link table covers the gap in between.
    """
    return _cached("creators", lambda: list(Creator.objects.usable()))


def cached_title_gate() -> TitleGate:
    """The listing walk's title gate, compiled once per run from `cached_creators`.

    The name set is small and the card set is large, so the names are compiled once and reused for
    every card of every listing page this process walks; the same short TTL means a new or
    suppressed creator reaches the gate within two minutes. It is built from every linkable
    creator rather than from `marceline.targets` — the crawler may not import the project package,
    and the wider set is the safe direction: a card admitted for a creator who is no target costs
    one page, which ADR 0029's active-page rule then deactivates like any other untargeted page.
    """
    return _cached("title_gate", lambda: TitleGate.for_creators(cached_creators()))


def load_safety_terms() -> list[safety.CompiledTerm]:
    """Enabled `catalog.SafetyTerm`s, compiled (docs/safety.md)."""
    return safety.compile_terms(
        safety.Term(term.term, term.category, term.action, term.is_regex)
        for term in SafetyTerm.objects.filter(enabled=True).order_by("pk")
    )


def record_rule_set(terms: Sequence[safety.CompiledTerm]) -> str:
    """The version of this rule set, with its snapshot stored once (`catalog.SafetyRuleSet`)."""
    version = safety.rule_version(terms)
    if version not in _recorded_rule_sets:
        SafetyRuleSet.objects.get_or_create(
            version=version,
            defaults={
                "terms": [
                    {
                        "term": c.term.term,
                        "is_regex": c.term.is_regex,
                        "category": c.term.category,
                        "action": c.term.action,
                    }
                    for c in terms
                ]
            },
        )
        # Remembered only once committed: a rolled-back snapshot must be written again.
        transaction.on_commit(partial(_recorded_rule_sets.add, version))
    return version


def page_data_texts(data: PageData) -> list[str]:
    return [data.title, *data.tags, *data.performers, *safety.collect_texts(dict(data.extra))]


def page_texts(page: Page) -> list[str]:
    return [
        page.title,
        *safety.collect_texts(page.tags),
        *safety.collect_texts(page.raw_extraction),
    ]


def site_risk(site: Site, *, now: datetime | None = None) -> safety_score.SiteRisk:
    """The site's own contribution to the score: recent `block` incidents on it (ADR 0018).

    A governance control: a source that repeatedly produces blocked pages loses the
    benefit of the doubt for every other page on it, without being switched off entirely.

    Only blocks that are hotline cases count (`catalog.safety_rules.incident_due`): minor
    indicators and human blocks, as before 2026-09-17. A page blocked only by an `auto_block`
    or `customer_release_queue` rule was a review page until then and never counted; counting
    it now would gate whole sites for titles that are not incidents.
    """
    days = int(getattr(settings, "SAFETY_GATE_BLOCK_WINDOW_DAYS", 90))
    since = (now or timezone.now()) - timedelta(days=days)
    no_incident_rule = Q(safety_matches__contains=[{"action": safety_rules.AUTO_BLOCK}]) | Q(
        safety_matches__contains=[{"action": safety_rules.CUSTOMER_RELEASE_QUEUE}]
    )
    incident_rule = Q(
        safety_matches__contains=[{"action": safety_rules.AUTO_BLOCK_AND_INCIDENT}]
    ) | Q(safety_matches__contains=[{"action": safety_rules.LEGACY_BLOCK}])
    blocks = (
        Page.objects.filter(
            site=site, safety_status=Page.SafetyStatus.BLOCKED, safety_flagged_at__gte=since
        )
        .exclude(no_incident_rule & ~incident_rule)
        .count()
    )
    return safety_score.SiteRisk(site.domain, blocks)


def _page_score(
    status: str,
    keep_review: bool,
    texts: Sequence[str],
    verdict: safety.Verdict,
    risk: safety_score.SiteRisk,
) -> safety_score.PageScore:
    """The score for a page whose status has already been resolved.

    A flagged page scores zero: its hard deny does not depend on any threshold. A page a human
    cleared is judged on the rest of its text — the terms they already decided about must not
    hold its media back for ever (that review pages may be cleared is answered
    by the reviewer's documented decision, not by scoring the same words again).
    """
    if status != safety.CLEAR:
        reason = safety_score.BLOCK_TERM if status == safety.BLOCKED else safety_score.REVIEW_TERM
        return safety_score.PageScore(safety_score.MIN_SCORE, reason, (reason,))
    judged = safety.Verdict(safety.CLEAR) if keep_review else verdict
    return safety_score.score_page(texts, judged, site_risk=risk)


def _safety_fields(
    existing: Page | None,
    verdict: safety.Verdict,
    now: datetime,
    *,
    texts: Sequence[str],
    risk: safety_score.SiteRisk,
    rule_version: str = "",
) -> tuple[dict[str, object], bool]:
    """Page fields for a fresh verdict, and whether the page became (more) flagged.

    Also the customer release queue's automatic half (docs/safety.md): a release stands only while
    the page stays clear — any other safety hit on a re-crawl (which `safety.resolve` turns into a
    block or review) revokes it — and a queued page that picks up another hit leaves the queue.
    """
    current_matches = existing.safety_matches if existing is not None else []
    resolution = safety.resolve(
        existing.safety_status if existing is not None else None,
        current_matches if isinstance(current_matches, list) else [],
        existing is not None and existing.safety_reviewed_at is not None,
        verdict,
    )
    score = _page_score(resolution.status, resolution.keep_review, texts, verdict, risk)
    fields: dict[str, object] = {
        "safety_status": resolution.status,
        "safety_matches": resolution.matches,
        "safety_score": score.score,
        "safety_score_reason": score.reason,
        "safety_score_signals": list(score.signals),
        "safety_scored_at": now,
    }
    newly_flagged = resolution.status != safety.CLEAR and (
        existing is None or existing.safety_status != resolution.status
    )
    if newly_flagged:
        fields["safety_flagged_at"] = now
    if not resolution.keep_review and existing is not None and existing.safety_reviewed_at:
        fields["safety_reviewed_at"] = None
    if not resolution.keep_review and resolution.matches == safety.matches_json(verdict.matches):
        fields["safety_rule_version"] = rule_version if resolution.matches else ""
    release = existing.safety_release if existing is not None else Page.SafetyRelease.NONE
    if release == Page.SafetyRelease.RELEASED and resolution.status != safety.CLEAR:
        fields["safety_release"] = Page.SafetyRelease.REVOKED
        fields["safety_release_at"] = now
    elif release == Page.SafetyRelease.QUEUED and not (
        resolution.status == safety.BLOCKED and safety_rules.release_queue_only(resolution.matches)
    ):
        fields["safety_release"] = Page.SafetyRelease.NONE
        fields["safety_release_at"] = now
    return fields, newly_flagged


def rule_events(existing: Page | None, fields: Mapping[str, object]) -> list[str]:
    """`SafetyRuleDecision` kinds a stored verdict amounts to. Pure.

    A block is logged when the page is blocked with a set of rule matches it did not have before
    (term or action changed), under its strictest rule; re-crawls with the same matches log
    nothing. Revoked releases and pages leaving the release queue are logged as well.
    """
    kinds: list[str] = []
    before = existing.safety_matches if existing is not None else []
    after = fields.get("safety_matches", before)
    if fields.get("safety_status") == safety.BLOCKED and safety_rules.match_keys(
        after
    ) != safety_rules.match_keys(before):
        strictest = safety_rules.strictest_action(after)
        kind = {
            safety_rules.AUTO_BLOCK_AND_INCIDENT: SafetyRuleDecision.Kind.AUTO_BLOCK_AND_INCIDENT,
            safety_rules.AUTO_BLOCK: SafetyRuleDecision.Kind.AUTO_BLOCK,
            safety_rules.CUSTOMER_RELEASE_QUEUE: SafetyRuleDecision.Kind.RELEASE_HOLD,
        }.get(strictest)
        if kind is not None:
            kinds.append(kind)
    release = fields.get("safety_release")
    if release == Page.SafetyRelease.REVOKED:
        kinds.append(SafetyRuleDecision.Kind.RELEASE_REVOKED)
    elif release == Page.SafetyRelease.NONE and existing is not None:
        kinds.append(SafetyRuleDecision.Kind.DEQUEUED)
    return kinds


def record_rule_events(
    page: Page, kinds: Sequence[str], terms: Sequence[safety.CompiledTerm]
) -> None:
    """Write the audit rows for `rule_events`, with the rule set version they were decided by."""
    if not kinds:
        return
    version = record_rule_set(terms)
    matches = page.safety_matches if isinstance(page.safety_matches, list) else []
    SafetyRuleDecision.objects.bulk_create(
        SafetyRuleDecision(
            page=page,
            kind=kind,
            status=page.safety_status,
            terms=matches,
            rule_version=page.safety_rule_version or version,
        )
        for kind in kinds
    )


def rules_follow_up(page: Page, kinds: Sequence[str]) -> bool:
    """Whether the project layer must act on this stored page (`index.signals`): open or update
    an incident for a page an incident rule blocked, or (re)decide the customer release queue."""
    if kinds and any(
        kind in (SafetyRuleDecision.Kind.RELEASE_REVOKED, SafetyRuleDecision.Kind.DEQUEUED)
        for kind in kinds
    ):
        return True
    if page.safety_status != safety.BLOCKED:
        return False
    return safety_rules.opens_incident(page.safety_matches) or (
        safety_rules.has_release_queue_match(page.safety_matches)
    )


def send_rules_follow_up(page_ids: Sequence[int]) -> None:
    """Hand pages to the project layer after commit; a receiver failure never breaks the caller."""
    if not page_ids:
        return
    results = signals.safety_rules_applied.send_robust(sender=Page, page_ids=list(page_ids))
    for receiver, result in results:
        if isinstance(result, Exception):
            # The pages stay blocked either way; `safety_incidents` and `safety_scan` catch up.
            logger.error(
                "safety rule follow-up %s failed for %s page(s)",
                getattr(receiver, "__name__", receiver),
                len(page_ids),
                exc_info=result,
            )


def store_parse_result(
    task: CrawlTask,
    result: ParseResult,
    *,
    creators: Sequence[Creator] | None = None,
    safety_terms: Sequence[safety.CompiledTerm] | None = None,
) -> Page | None:
    """Upsert the page, its media and creator links, and enqueue discovered links. Idempotent.

    Page text is checked against the safety terms first (docs/safety.md). Blocked pages keep only
    reporting metadata: no media, no creator links, no discovered links. Pages under review are
    indexed with their media queued, but the media worker skips them until a reviewer clears them.

    **A page that is still deactivated as untargeted stays scrubbed** (ADR 0029 amendment, issue
    #148): title, tags and the performer credits in `raw_extraction` were deleted when it was
    deactivated, so this upsert leaves all three as they are instead of writing them back — the
    deactivation decides what the row may hold, not the crawl. Everything else about the page
    (availability, safety verdict, `duration_s`, the adapter's other extraction keys, creator links)
    is stored as usual, which is what lets the same page be reactivated later; from the moment it is
    active again the next parse fills the three fields normally.
    """
    with transaction.atomic():
        page = None
        blocked = False
        if result.page is not None:
            data = result.page
            url = normalize_url(data.url)
            if not is_on_site(url, task.site.domain):
                url = task.url
            now = timezone.now()
            terms = safety_terms if safety_terms is not None else cached_safety_terms()
            page_texts_now = page_data_texts(data)
            verdict = safety.check(page_texts_now, terms)
            existing = Page.objects.select_for_update().filter(url=url).first()
            safety_fields, newly_flagged = _safety_fields(
                existing,
                verdict,
                now,
                texts=page_texts_now,
                risk=site_risk(task.site, now=now),
                rule_version=safety.rule_version(terms),
            )
            events = rule_events(existing, safety_fields)
            blocked = safety_fields["safety_status"] == safety.BLOCKED
            # A deactivation deleted title, tags and the credits of this page; keep them deleted
            # (issue #148). `raw_extraction`'s other keys are not about people and are refreshed.
            scrubbed = stays_scrubbed(existing)
            raw_extraction: dict[str, object] = {
                **({} if blocked else dict(data.extra)),
                "performers": list(data.performers),
                "duration_s": data.duration_s,
            }
            page, _ = Page.objects.update_or_create(
                url=url,
                defaults={
                    **(
                        {"availability": Page.Availability.PLAYABLE, "availability_checked_at": now}
                        if task.kind == CrawlTask.Kind.DETAIL
                        else {}
                    ),
                    **safety_fields,
                    "site": task.site,
                    "extraction_source": data.extracted_by,
                    **({} if scrubbed else {"title": data.title, "tags": list(data.tags)}),
                    "uploaded_at": data.uploaded_at,
                    "raw_extraction": (
                        without_performer_credits(raw_extraction) if scrubbed else raw_extraction
                    ),
                },
            )
            _remember_page_url(task, page.url)
            record_rule_events(page, events, terms)
            if rules_follow_up(page, events):
                # Incidents and the customer release queue (index/signals.py), after commit.
                transaction.on_commit(partial(send_rules_follow_up, [page.pk]))
            if newly_flagged:
                log_flag(page, verdict.categories)
                if blocked:
                    # Active safety notification (issue #40, crawler/alerts.py): the moderation
                    # queue and admin are the source of truth; this is the operator's push nudge.
                    # Sent after commit: never for a block that rolled back, and a slow mail
                    # server never holds this page's row lock.
                    transaction.on_commit(partial(alerts.notify_block, page))
            if blocked:
                cancel_page_media([page.pk])
            else:
                context_media = _media_from_context(task.context)
                fresh = [
                    spec
                    for spec in context_media
                    if (expiry := _token_expiry(spec.url)) is None or expiry > now.timestamp()
                ]
                if len(fresh) != len(context_media):
                    task.context = {**task.context, EXPIRED_CONTEXT_KEY: now.isoformat()}
                    task.save(update_fields=("context", "updated_at"))
                # This is the moment the detail page's own text has been parsed and scored, so
                # it is the only moment its media may become eligible (ADR 0018). Card media
                # carried in the task context becomes eligible here too — never on the listing.
                allowed = page_media_allowed(page.safety_status, page.safety_score)
                _attach_media(page, [*fresh, *result.media], eligible_at=now if allowed else None)
                if allowed:
                    # Both halves of "this page may have media again": skipped rows come back,
                    # and rows that waited unskipped (a page under review keeps its media
                    # pending with no eligibility) become eligible — the same rule as a rescore.
                    release_page_media([page.pk], now=now)
                    allow_page_media([page.pk], now=now)
                else:
                    # A page under review keeps its media pending as before; only a page that is
                    # clear but not clearly safe is skipped with the gate reason.
                    gate_page_media(
                        [page.pk],
                        reason=(
                            SAFETY_GATE_REASON
                            if page.safety_status == Page.SafetyStatus.CLEAR
                            else None
                        ),
                    )
                all_creators = creators if creators is not None else cached_creators()
                links = creator_links(
                    data.title, data.tags, data.performers, data.extra, all_creators
                )
                page.creators.add(*links.linked)
                # A re-crawl that now flags the page (compilation, noise credit) takes back the
                # links its flags rule out; nothing else ever writes `Page.creators`.
                unlink_creators(page, [creator.pk for creator in links.ruled_out])
                # Site x creator breadcrumbs (issue #71): every creator, target or not.
                record_page_links(page, [creator.pk for creator in links.linked], now=now)
        if not blocked:
            # A walk is a chain of listing pages. Its detail tasks carry the walk as provenance,
            # but a detail page's own links (related videos and the like) are judged as they have
            # always been: the gate is about which cards of a catalogue page to fetch.
            walk = walk_of(task.context) if task.kind != CrawlTask.Kind.DETAIL else None
            discovered = result.links
            context: Mapping[str, object] | None = None
            if walk is not None:
                # A breadth-first catalogue walk (crawler.listing_walk): fetch the cards that
                # plausibly name a creator we know, hand the walk down to the next page only.
                plan = walk_links(result.links, cached_title_gate())
                _log_walk(task, plan)
                discovered, context = plan.links, walk_context(walk)
            # Card media of every card, gated or not: a page we already indexed may need the fresh
            # link for its expired media, and attaching one costs no request (ADR 0018 decides
            # what may be fetched, and an untargeted page's media is never claimed anyway).
            _attach_card_media(task.site, result.links, now=timezone.now())
            enqueue(task.site, discovered, discovered_from=task, context=context)
            if walk is not None:
                adopt_walk_successors(task.site, walk, discovered)
    return page


def _log_walk(task: CrawlTask, plan: WalkPlan) -> None:
    logger.info(
        "listing walk crawl task id=%s site=%s: %d of %d card(s) admitted, %d page(s) ahead",
        task.pk,
        task.site.domain,
        plan.admitted,
        plan.cards,
        plan.ahead,
    )


def rescore_page(
    page: Page,
    *,
    safety_terms: Sequence[safety.CompiledTerm] | None = None,
    now: datetime | None = None,
) -> safety_score.PageScore:
    """Recompute one page's score from its stored text and re-apply the media gate (ADR 0018).

    Used after a human decision: clearing a page answers the terms it matched, and the score then
    decides whether its media may be fetched at all. The safety status itself is not changed.
    """
    now = now or timezone.now()
    terms = safety_terms if safety_terms is not None else load_safety_terms()
    texts = page_texts(page)
    verdict = safety.check(texts, terms)
    keep_review = page.safety_reviewed_at is not None and bool(verdict.matches)
    score = _page_score(
        page.safety_status, keep_review, texts, verdict, site_risk(page.site, now=now)
    )
    fields: dict[str, object] = {
        "safety_score": score.score,
        "safety_score_reason": score.reason,
        "safety_score_signals": list(score.signals),
        "safety_scored_at": now,
    }
    Page.objects.filter(pk=page.pk).update(**fields)
    for key, value in fields.items():
        setattr(page, key, value)
    if page_media_allowed(page.safety_status, score.score):
        release_page_media([page.pk], now=now)
        allow_page_media([page.pk], now=now)
    else:
        gate_page_media(
            [page.pk],
            reason=(SAFETY_GATE_REASON if page.safety_status == Page.SafetyStatus.CLEAR else None),
        )
    return score


@dataclass
class SafetyScanReport:
    dry_run: bool
    scanned: int = 0
    by_status: Counter[str] = field(default_factory=Counter)
    changed: Counter[str] = field(default_factory=Counter)
    by_category: Counter[tuple[str, str]] = field(default_factory=Counter)
    by_site: Counter[tuple[str, str]] = field(default_factory=Counter)
    media_cancelled: int = 0
    hashes_deleted: int = 0
    flagged: list[tuple[int, str, str, list[str]]] = field(default_factory=list)
    # Media-gate cost (ADR 0018): per site, how many pages may be fingerprinted and how many
    # stay text-only, plus the deciding reason for the text-only ones.
    hashed_by_site: Counter[str] = field(default_factory=Counter)
    text_only_by_site: Counter[str] = field(default_factory=Counter)
    text_only_reasons: Counter[str] = field(default_factory=Counter)
    media_gated: int = 0
    media_released: int = 0
    # Media that was never eligible and belongs to a page the gate allows: the backfill for
    # everything stored before the gate existed.
    media_eligible: int = 0
    # Rule actions (catalog/safety_rules.py): blocked pages per (strictest action, category), and
    # how many of them this scan blocks (they were not blocked before).
    rule_version: str = ""
    rule_blocked: Counter[tuple[str, str]] = field(default_factory=Counter)
    rule_newly_blocked: Counter[tuple[str, str]] = field(default_factory=Counter)
    releases_revoked: int = 0
    # Pages for the project layer (`marceline.safety_rules`): blocked by an incident rule, and
    # blocked by / queued for the customer release queue. In a dry run these are unsaved copies
    # carrying the verdict the scan would store; otherwise the stored pages.
    incident_pages: list[Page] = field(default_factory=list)
    release_pages: list[Page] = field(default_factory=list)
    # PDQ hashes taken off the pages this scan blocked by an incident rule, on their way to the
    # case (ADR 0011 amendment 2026-09-17). They are read immediately before the same step deletes
    # them from `FrameHash` and travel no further than this report: the command hands them to
    # `marceline.safety_incidents.sync_incidents`, which keeps them on the incident. Nothing is
    # fetched for them, and a run that dies in between simply loses them.
    retained_hashes: dict[int, list[RetainedHash]] = field(default_factory=dict)
    hashes_retained: int = 0


def scan_pages(
    *,
    site_domain: str | None = None,
    dry_run: bool = False,
    safety_terms: Sequence[safety.CompiledTerm] | None = None,
    now: datetime | None = None,
) -> SafetyScanReport:
    """Re-check stored pages against the safety terms, apply the statuses and rescore them.

    Blocked pages lose their queued media and stored frame hashes. A page blocked by a rule that
    opens a case keeps its hashes in one place only: the report hands them to the incident, which
    uses them to recognise the same material again (ADR 0011 amendment 2026-09-17). They are gone
    from `FrameHash` either way. Scores are recomputed from the
    current terms and the site's current risk factor (ADR 0018), and the media gate is re-applied:
    pages that are no longer clearly safe lose their eligibility, pages that became clearly safe
    get their held-back media released. With `dry_run` nothing is written; media and hash counts
    are what would be cancelled and deleted.

    This is also the backfill: pages indexed before the gate existed have no score, so the first
    run scores all of them and gates whatever is not clearly safe.

    The scan runs beside live crawlers and reviewers, so nothing it read is trusted when it
    writes: a verdict is stored only while the page is still as read (otherwise the page is judged
    again under a row lock), and the media gate is applied from the pages' current, locked rows.
    A block that commits while the scan runs is never overwritten and its media never released.
    """
    now = now or timezone.now()
    terms = safety_terms if safety_terms is not None else load_safety_terms()
    report = SafetyScanReport(dry_run=dry_run, rule_version=safety.rule_version(terms))
    pages = Page.objects.select_related("site").order_by("pk")
    if site_domain:
        pages = pages.filter(site__domain=site_domain)
    blocked_ids: list[int] = []
    allowed_ids: list[int] = []
    gated_ids: list[int] = []
    scanned_ids: list[int] = []
    risks: dict[int, safety_score.SiteRisk] = {}
    for page in pages.iterator(chunk_size=1000):
        report.scanned += 1
        risk = risks.get(page.site_id)
        if risk is None:
            risk = site_risk(page.site, now=now)
            risks[page.site_id] = risk
        scan = _scan_page(page, terms, now, risk)
        if not dry_run:
            written = _store_scan(scan, terms, now, risk)
            if written is None:
                continue  # deleted while the scan ran
            scan = written
        page, fields = scan.page, scan.fields
        after = _count_rules(report, page, fields)
        if not dry_run and not scan.unchanged:
            record_rule_events(after, rule_events(page, fields), terms)
        status = str(fields["safety_status"])
        score = fields["safety_score"]
        scanned_ids.append(page.pk)
        if page_media_allowed(status, score if isinstance(score, float) else None):
            report.hashed_by_site[page.site.domain] += 1
            allowed_ids.append(page.pk)
        else:
            report.text_only_by_site[page.site.domain] += 1
            report.text_only_reasons[str(fields["safety_score_reason"])] += 1
            if status == safety.CLEAR:
                gated_ids.append(page.pk)
        if status != page.safety_status:
            report.changed[f"{page.safety_status}->{status}"] += 1
        if status != safety.CLEAR:
            matches = fields["safety_matches"]
            categories = sorted(
                {str(m.get("category")) for m in matches if isinstance(m, dict)}
                if isinstance(matches, list)
                else set()
            ) or ["manual"]
            report.by_status[status] += 1
            report.by_site[(status, page.site.domain)] += 1
            report.by_category.update((status, category) for category in categories)
            report.flagged.append((page.pk, page.site.domain, status, categories))
        if status == safety.BLOCKED:
            blocked_ids.append(page.pk)
        if dry_run or not scan.newly_flagged:
            continue
        for key, value in fields.items():
            setattr(page, key, value)
        log_flag(page, scan.verdict.categories)
        if status == safety.BLOCKED:
            # Active safety notification (issue #40, crawler/alerts.py), sent only after the
            # verdict is committed. Rate-limited in process; `flush_pending` below guarantees
            # this one-shot command still mails whatever is left queued before it exits.
            alerts.notify_block(page)
    if dry_run:
        pending = Media.objects.filter(page_id__in=blocked_ids)
        report.media_cancelled = pending.exclude(status=Media.Status.SKIPPED).count()
        report.hashes_deleted = FrameHash.objects.filter(media__page_id__in=blocked_ids).count()
        would_retain = collect_page_hashes([page.pk for page in report.incident_pages])
        report.hashes_retained = sum(len(hashes) for hashes in would_retain.values())
        report.media_gated = (
            Media.objects.filter(page_id__in=gated_ids).exclude(eligible_at__isnull=True).count()
        )
        report.media_eligible = (
            Media.objects.filter(page_id__in=allowed_ids, eligible_at__isnull=True)
            .exclude(status=Media.Status.SKIPPED)
            .count()
        )
        return report
    for start in range(0, len(scanned_ids), 1000):
        _apply_scan_gate(scanned_ids[start : start + 1000], report, now=now)
    alerts.flush_pending(now=now)
    return report


@dataclass(frozen=True)
class _PageScan:
    """One page's fresh verdict, and whether storing it newly flags the page."""

    page: Page
    verdict: safety.Verdict
    fields: dict[str, object]
    newly_flagged: bool

    @property
    def unchanged(self) -> bool:
        # `safety_scored_at` moves on every scan, so it never counts as a change by itself.
        return all(
            getattr(self.page, key) == value
            for key, value in self.fields.items()
            if key != "safety_scored_at"
        )


def _scan_page(
    page: Page, terms: Sequence[safety.CompiledTerm], now: datetime, risk: safety_score.SiteRisk
) -> _PageScan:
    texts = page_texts(page)
    verdict = safety.check(texts, terms)
    fields, newly_flagged = _safety_fields(
        page, verdict, now, texts=texts, risk=risk, rule_version=safety.rule_version(terms)
    )
    return _PageScan(page, verdict, fields, newly_flagged)


def _count_rules(report: SafetyScanReport, page: Page, fields: Mapping[str, object]) -> Page:
    """Rule counts for one scanned page, and the pages the project layer follows up.

    `page` is the row as it was before this scan's write (a written scan keeps the page it read),
    `fields` the verdict stored — or, in a dry run, the verdict that would be.
    """
    status = fields.get("safety_status")
    matches = fields.get("safety_matches", page.safety_matches)
    after = copy(page)
    for key, value in fields.items():
        setattr(after, key, value)
    if fields.get("safety_release") == Page.SafetyRelease.REVOKED:
        report.releases_revoked += 1
    if status == safety.BLOCKED:
        action = safety_rules.strictest_action(matches)
        if action and action != safety_rules.REVIEW:
            categories = sorted(
                {
                    str(m.get("category"))
                    for m in (matches if isinstance(matches, list) else [])
                    if isinstance(m, dict)
                    and safety_rules.canonical_action(m.get("action")) == action
                }
            )
            for category in categories:
                report.rule_blocked[(action, category)] += 1
                if page.safety_status != safety.BLOCKED:
                    report.rule_newly_blocked[(action, category)] += 1
        if safety_rules.opens_incident(matches):
            report.incident_pages.append(after)
    if (
        status == safety.BLOCKED and safety_rules.has_release_queue_match(matches)
    ) or after.safety_release == Page.SafetyRelease.QUEUED:
        report.release_pages.append(after)
    return after


def _store_scan(
    scan: _PageScan,
    terms: Sequence[safety.CompiledTerm],
    now: datetime,
    risk: safety_score.SiteRisk,
) -> _PageScan | None:
    """Store a scan verdict without overwriting a safety decision made after the page was read.

    The write is conditional on the status, review and score stamps the verdict was computed
    from. If any of them moved (a crawl stored the page, a reviewer decided, a rescore ran), the
    page is read again under a row lock and judged from that row instead. `safety.resolve` never
    unblocks a page, so a block that committed in between always stands. Returns the scan that
    was stored (`newly_flagged` only when this write flagged the page), or None for a page that
    no longer exists.
    """
    if scan.unchanged:
        return replace(scan, newly_flagged=False)
    page = scan.page
    written = Page.objects.filter(
        pk=page.pk,
        safety_status=page.safety_status,
        safety_reviewed_at=page.safety_reviewed_at,
        safety_scored_at=page.safety_scored_at,
    ).update(**scan.fields)
    if written:
        return scan
    with transaction.atomic():
        fresh = (
            Page.objects.select_for_update(no_key=True, of=("self",))
            .select_related("site")
            .filter(pk=page.pk)
            .first()
        )
        if fresh is None:
            return None
        scan = _scan_page(fresh, terms, now, risk)
        if scan.unchanged:
            return replace(scan, newly_flagged=False)
        Page.objects.filter(pk=fresh.pk).update(**scan.fields)
        return scan


def _apply_scan_gate(page_ids: Sequence[int], report: SafetyScanReport, *, now: datetime) -> None:
    """Apply the media gate to these pages from their current rows, locked for the duration.

    Never from what the scan read earlier: a page blocked (or put under review) since then must
    not have its media released or made eligible. Blocked pages are cancelled, pages under review
    only lose eligibility (their media keeps waiting, docs/safety.md), clear pages below the
    score gate are skipped with the gate reason, and only clearly safe pages get media back.

    `FOR NO KEY UPDATE`: every safety write to these pages waits, but a crawl inserting media for
    one of them (a foreign-key share lock) does not, so it cannot deadlock against this batch.
    """
    with transaction.atomic():
        rows = list(
            Page.objects.select_for_update(no_key=True)
            .filter(pk__in=page_ids)
            .order_by("pk")
            .values_list("pk", "safety_status", "safety_score")
        )
        blocked = [pk for pk, status, _ in rows if status == Page.SafetyStatus.BLOCKED]
        review = [pk for pk, status, _ in rows if status == Page.SafetyStatus.REVIEW]
        clear = [(pk, score) for pk, status, score in rows if status == Page.SafetyStatus.CLEAR]
        allowed = [pk for pk, score in clear if page_media_allowed(Page.SafetyStatus.CLEAR, score)]
        gated = [
            pk for pk, score in clear if not page_media_allowed(Page.SafetyStatus.CLEAR, score)
        ]
        if blocked:
            # Move the hashes of pages a rule blocks with a case onto the report first, then
            # delete them with everything else (ADR 0011 amendment 2026-09-17). Read here, under
            # the same lock as the deletion, so no hash is retained that the deletion misses.
            incident_ids = {page.pk for page in report.incident_pages}
            for page_id, hashes in collect_page_hashes(
                [pk for pk in blocked if pk in incident_ids]
            ).items():
                report.retained_hashes[page_id] = hashes
                report.hashes_retained += len(hashes)
            cancelled, deleted = cancel_page_media(blocked)
            report.media_cancelled += cancelled
            report.hashes_deleted += deleted
        report.media_gated += gate_page_media(gated)
        gate_page_media(review, reason=None)
        if allowed:
            report.media_released += release_page_media(allowed, now=now)
            report.media_eligible += allow_page_media(allowed, now=now)


def _attach_card_media(site: Site, links: Sequence[TaskSpec], *, now: datetime) -> None:
    """Attach listing-card media to pages that are already indexed.

    New pages get card media from the task context when their detail page is parsed. Indexed
    pages only gain media of kinds without usable media, e.g. fresh links for expired ones.

    A listing never makes media eligible by itself (ADR 0018). Media attached here inherits the
    gate state of the page it belongs to, which was decided when *that page* was parsed: fresh
    links for a page already judged clearly safe are fetchable, everything else is recorded and
    left inert.
    """
    media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
    for link in links:
        if link.media:
            media_by_url.setdefault(normalize_url(link.url), link.media)
    if not media_by_url:
        return
    pages = Page.objects.filter(site=site, url__in=media_by_url).exclude(
        safety_status=Page.SafetyStatus.BLOCKED
    )
    for page in pages:
        allowed = page_media_allowed(page.safety_status, page.safety_score)
        _attach_media(page, media_by_url[page.url], eligible_at=now if allowed else None)


def _attach_media(page: Page, specs: Sequence[MediaSpec], *, eligible_at: datetime | None) -> None:
    covered_kinds = set(
        Media.objects.filter(page=page, status__in=_USABLE_MEDIA).values_list("kind", flat=True)
    )
    seen: set[str] = set()
    for spec in specs:
        if spec.kind in covered_kinds:
            continue
        url = normalize_url(spec.url)
        if url in seen or not is_http_url(url):
            continue
        seen.add(url)
        Media.objects.get_or_create(
            page=page,
            url=url,
            defaults={
                "kind": spec.kind,
                "eligible_at": eligible_at,
                "sprite_columns": spec.sprite_columns,
                "sprite_rows": spec.sprite_rows,
            },
        )


def _media_context(media: Sequence[MediaSpec]) -> dict[str, object]:
    if not media:
        return {}
    return {
        "media": [
            {
                "kind": Media.Kind(spec.kind).value,
                "url": spec.url,
                **(
                    {"sprite_columns": spec.sprite_columns, "sprite_rows": spec.sprite_rows}
                    if spec.sprite_columns is not None and spec.sprite_rows is not None
                    else {}
                ),
            }
            for spec in media
        ]
    }


def _media_from_context(context: object) -> list[MediaSpec]:
    if not isinstance(context, dict):
        return []
    specs: list[MediaSpec] = []
    for item in context.get("media") or []:
        if isinstance(item, dict) and item.get("kind") in Media.Kind.values:
            url = item.get("url")
            columns, rows = item.get("sprite_columns"), item.get("sprite_rows")
            if isinstance(url, str):
                specs.append(
                    MediaSpec(
                        Media.Kind(item["kind"]),
                        url,
                        sprite_columns=columns if isinstance(columns, int) else None,
                        sprite_rows=rows if isinstance(rows, int) else None,
                    )
                )
    return specs


def recover_expired_media(
    *,
    site_domain: str | None = None,
    max_age: timedelta | None = MEDIA_RECOVERY_WINDOW,
    now: datetime | None = None,
) -> int:
    """Re-crawl the tasks that produced expired media links, so their pages get fresh URLs.

    A media kind of a (not removed) page qualifies when all its media are FAILED/SKIPPED and one
    tokenized link (EXPIRING_URL) was skipped as expired within `max_age` (None: any age). A plain
    URL that answered 403/404/410 is gone, not expired: re-crawling would not change it. Its
    producer is refreshed: the listing whose card carried that kind (the detail task's
    `discovered_from`), or once that listing was fetched after the expiry, the detail page itself.
    Producers that are queued, or were all fetched after the link expired, are left alone. Each
    page gets at most MAX_MEDIA_RECOVERIES rounds, counted in its detail task's context, and one
    run re-queues at most MAX_RECOVERY_TASKS_PER_RUN tasks. `enqueue` keeps the dead-URL cooldown;
    the daily budget applies when the tasks are claimed.
    Returns the number of tasks put back into the queue.

    **An inactive page is never recovered** (issue #148): `matching.media.claimable_media` filters
    `page__active=True`, so its media cannot be fetched anyway — the re-crawl could only spend page
    budget on a page targeted indexing has deactivated. It was also the path by which most
    deactivated pages were re-crawled at all (`crawl_recheck` skips them): 649 of the 667 pages
    measured on 2026-09-19 as re-seen after their deactivation carried this function's own
    `MEDIA_RECOVERIES_KEY` counter.
    """
    now = now or timezone.now()
    usable = Media.objects.filter(
        page=OuterRef("page"), kind=OuterRef("kind"), status__in=_USABLE_MEDIA
    )
    expired = (
        Media.objects.filter(
            status=Media.Status.SKIPPED,
            last_error__endswith=LINK_EXPIRED,
            url__regex=EXPIRING_URL_REGEX,
            page__site__enabled=True,
            page__active=True,
        )
        .exclude(page__availability=Page.Availability.REMOVED)
        .filter(page__safety_status=Page.SafetyStatus.CLEAR)
        .exclude(Exists(usable))
        .select_related("page__site")
        .order_by("page_id", "id")
    )
    if site_domain:
        expired = expired.filter(page__site__domain=site_domain)
    if max_age is not None:
        expired = expired.filter(fetched_at__gte=now - max_age)
    by_page: dict[int, list[Media]] = {}
    for media in expired:
        by_page.setdefault(media.page_id, []).append(media)
    # Context expiries have no Media rows; reuse producer selection and the recovery cap.
    # A context expiry is looked at once: its marker is cleared when it has been handled (review
    # PERF — it used to be re-read, one page and one media query per task, every idle tick).
    context_tasks = CrawlTask.objects.filter(
        site__enabled=True, context__has_key=EXPIRED_CONTEXT_KEY
    ).only("pk", "site_id", "url", "context")
    if site_domain:
        context_tasks = context_tasks.filter(site__domain=site_domain)
    evaluated: dict[tuple[int, str], tuple[CrawlTask, datetime]] = {}
    handled: set[int] = set()
    for task in context_tasks:
        try:
            expired_at = datetime.fromisoformat(str(task.context[EXPIRED_CONTEXT_KEY]))
        except ValueError:
            handled.add(task.pk)
            continue
        if max_age is None or expired_at >= now - max_age:
            evaluated[(task.site_id, task.url)] = (task, expired_at)
    pages = (
        Page.objects.filter(
            url__in={url for _, url in evaluated},
            safety_status=Page.SafetyStatus.CLEAR,
            active=True,
        )
        .exclude(availability=Page.Availability.REMOVED)
        .select_related("site")
    )
    page_by_key = {(page.site_id, page.url): page for page in pages}
    covered: dict[int, set[str]] = {}
    for page_id, kind in Media.objects.filter(
        page_id__in=[page.pk for page in page_by_key.values()], status__in=_USABLE_MEDIA
    ).values_list("page_id", "kind"):
        covered.setdefault(page_id, set()).add(kind)
    context_task_ids: dict[int, list[int]] = {}
    for key, (task, expired_at) in evaluated.items():
        page = page_by_key.get(key)
        if page is None:
            handled.add(task.pk)
            continue
        for spec in _media_from_context(task.context):
            expiry = _token_expiry(spec.url)
            if (
                expiry is not None
                and expiry <= expired_at.timestamp()
                and spec.kind not in covered.get(page.pk, set())
            ):
                by_page.setdefault(page.pk, []).append(
                    Media(page=page, kind=spec.kind, url=spec.url, fetched_at=expired_at)
                )
        if page.pk in by_page:
            context_task_ids.setdefault(page.pk, []).append(task.pk)
        else:
            handled.add(task.pk)
    requeued = 0
    for page_id, page_media in by_page.items():
        if requeued >= MAX_RECOVERY_TASKS_PER_RUN:
            break  # the rest keep their markers for the next run
        requeued += _recover_page_media(page_media[0].page, page_media, now)
        handled.update(context_task_ids.get(page_id, ()))
    if handled:
        # Remove only the marker, in the database: the rest of the context may have moved on.
        CrawlTask.objects.filter(pk__in=handled).update(
            context=Func(
                F("context"),
                Cast(Value(EXPIRED_CONTEXT_KEY), TextField()),
                template="(%(expressions)s)",
                arg_joiner=" - ",
                output_field=JSONField(),
            )
        )
    return requeued


def _recover_page_media(page: Page, expired: Sequence[Media], now: datetime) -> int:
    with transaction.atomic():
        detail = (
            CrawlTask.objects.select_for_update(of=("self",))
            .select_related("discovered_from")
            .filter(site=page.site, url=page.url)
            .first()
        )
        if detail is None:
            return 0
        context = detail.context if isinstance(detail.context, dict) else {}
        rounds = context.get(MEDIA_RECOVERIES_KEY, 0)
        if not isinstance(rounds, int) or rounds >= MAX_MEDIA_RECOVERIES:
            return 0
        card_kinds = {spec.kind for spec in _media_from_context(context)}
        by_kind: dict[str, list[Media]] = {}
        for media in expired:
            by_kind.setdefault(media.kind, []).append(media)
        producers: dict[int, CrawlTask] = {}
        for kind, kind_media in by_kind.items():
            producer = _stale_producer(detail, kind in card_kinds, kind_media)
            if producer is not None:
                producers[producer.pk] = producer
        if not producers:
            return 0
        for producer in producers.values():
            spec = TaskSpec(producer.url, CrawlTask.Kind(producer.kind), producer.priority)
            enqueue(page.site, [spec], refresh=True, now=now)
        requeued = CrawlTask.objects.filter(pk__in=producers, status=CrawlTask.Status.PENDING)
        count = requeued.count()
        if count:
            detail.context = {**context, MEDIA_RECOVERIES_KEY: rounds + 1}
            detail.save(update_fields=("context", "updated_at"))
        return count


def _stale_producer(detail: CrawlTask, on_card: bool, media: Sequence[Media]) -> CrawlTask | None:
    """The task to re-crawl for one page's expired media of one kind, or None.

    The linking listing first when its card carried this kind, then the detail page (it may not
    produce the kind; the round cap bounds that wasted request).
    """
    expired_at = max(item.fetched_at or item.created_at for item in media)
    candidates: list[CrawlTask] = []
    if on_card and detail.discovered_from is not None:
        candidates.append(detail.discovered_from)
    candidates.append(detail)
    if any(task.status in _ACTIVE_TASK for task in candidates):
        return None  # a queued crawl will attach fresh links anyway
    for task in candidates:
        if task.fetched_at is None or task.fetched_at < expired_at:
            return task
    return None


def _remember_page_url(task: CrawlTask, page_url: str) -> None:
    """Link a detail task to its page when the page lives under a different (canonical) URL."""
    if task.kind != CrawlTask.Kind.DETAIL:
        return
    context = task.context if isinstance(task.context, dict) else {}
    wanted = page_url if page_url != task.url else None
    if context.get(PAGE_URL_KEY) == wanted:
        return
    context = {key: value for key, value in context.items() if key != PAGE_URL_KEY}
    task.context = {**context, PAGE_URL_KEY: wanted} if wanted else context
    task.save(update_fields=("context", "updated_at"))


def mark_removed(task: CrawlTask) -> bool:
    """Record a removal without changing any previously indexed data.

    The page is found under the task URL or under the canonical URL the task stored it at
    (review L10: site11 and site22 pages whose removal was never recorded).
    """
    if task.kind != CrawlTask.Kind.DETAIL:
        return False
    urls = {task.url}
    context = task.context if isinstance(task.context, dict) else {}
    if isinstance(page_url := context.get(PAGE_URL_KEY), str):
        urls.add(page_url)
    return bool(
        Page.objects.filter(url__in=urls, site=task.site).update(
            availability=Page.Availability.REMOVED, availability_checked_at=timezone.now()
        )
    )


def recheck_pages(
    *,
    site_domain: str | None = None,
    older_than_hours: int = 24,
    limit: int | None = None,
    include_removed: bool = False,
    never_checked: bool = False,
    priority: int | None = None,
    include_inactive: bool = False,
    only_removed: bool = False,
) -> int:
    """Enqueue stale pages, reusing finished tasks and leaving active tasks alone.

    `never_checked` selects only pages without any availability check (the age cutoff does not
    apply); `priority` sets the priority of the queued tasks (default: keep the task's own).
    Inactive pages (targeted indexing, issue #71) are skipped unless `include_inactive`.
    `only_removed` selects removed pages only: a re-upload at the same URL is only noticed by
    checking the removed page again. Least recently checked pages come first.
    """
    if never_checked:
        stale = Q(availability_checked_at__isnull=True)
    else:
        cutoff = timezone.now() - timedelta(hours=older_than_hours)
        stale = Q(availability_checked_at__isnull=True) | Q(availability_checked_at__lt=cutoff)
    pages = (
        Page.objects.select_related("site")
        # Only the URL and its site's domain are read below; `index_page` rows are wide
        # (title, tags, raw_extraction), and a whole site's stale pages are held at once.
        .only("url", "site__domain")
        .filter(stale)
        .exclude(
            url__in=CrawlTask.objects.filter(
                status__in=(CrawlTask.Status.PENDING, CrawlTask.Status.RUNNING)
            ).values("url")
        )
        .order_by("availability_checked_at", "pk")
    )
    if site_domain:
        pages = pages.filter(site__domain=site_domain)
    if only_removed:
        pages = pages.filter(availability=Page.Availability.REMOVED)
    elif not include_removed:
        pages = pages.exclude(availability=Page.Availability.REMOVED)
    if not include_inactive:
        pages = pages.filter(active=True)
    count = 0
    batch: list[Page] = []
    for page in pages[:limit]:
        if not is_on_site(page.url, page.site.domain):
            continue
        batch.append(page)
        if len(batch) == RECHECK_BATCH:
            count += _requeue_pages(batch, priority=priority)
            batch = []
    return count + _requeue_pages(batch, priority=priority)


def _requeue_pages(pages: list[Page], *, priority: int | None) -> int:
    """Queue a DETAIL task per page and reset the rows a worker will pick up; returns how many.

    One transaction and a handful of statements per batch, not per page: a page cost eight
    queries and its own COMMIT, which is what made the scheduled recheck take 95 s.
    """
    if not pages:
        return 0
    extra = {} if priority is None else {"priority": priority}
    with transaction.atomic():
        by_site: dict[int, tuple[Site, list[TaskSpec]]] = {}
        for page in pages:
            _, specs = by_site.setdefault(page.site_id, (page.site, []))
            specs.append(TaskSpec(page.url, CrawlTask.Kind.DETAIL))
        for site, specs in by_site.values():
            enqueue(site, specs, refresh=True)
        urls = {normalize_url(page.url) for page in pages}
        return CrawlTask.objects.filter(url__in=urls, status=CrawlTask.Status.PENDING).update(
            kind=CrawlTask.Kind.DETAIL,
            decision_reason="",
            last_error="",
            locked_at=None,
            **extra,
        )


@dataclass
class TargetedRecheck:
    """Outcome per page id of `recheck_page_ids`."""

    queued: list[int] = field(default_factory=list)
    in_progress: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


def recheck_page_ids(
    page_ids: Iterable[int], *, priority: int = RECHECK_PRIORITY
) -> TargetedRecheck:
    """Queue DETAIL availability checks for specific pages (a reviewer asked for them).

    Only enqueues: the per-site crawler fetches them under robots.txt, delay and daily budget.
    Pages of disabled sites or with off-site URLs are skipped (domain allowlist). Finished tasks
    are put back into the queue like `recheck_pages`; pending tasks keep their retry schedule and
    get at least `priority`; running tasks are left alone. Removed pages are checked too.
    """
    result = TargetedRecheck()
    pages = Page.objects.select_related("site").filter(pk__in=set(page_ids)).order_by("pk")
    for page in pages:
        url = normalize_url(page.url)
        if not page.site.enabled or not is_on_site(url, page.site.domain):
            result.skipped.append(page.pk)
            continue
        with transaction.atomic():
            task = CrawlTask.objects.select_for_update().filter(url=url).first()
            if task is not None and task.status == CrawlTask.Status.RUNNING:
                result.in_progress.append(page.pk)
                continue
            enqueue(
                page.site, [TaskSpec(url, CrawlTask.Kind.DETAIL, priority=priority)], refresh=True
            )
            updated = CrawlTask.objects.filter(
                url=url, site=page.site, status=CrawlTask.Status.PENDING
            ).update(
                kind=CrawlTask.Kind.DETAIL,
                decision_reason="",
                last_error="",
                locked_at=None,
                priority=Greatest(F("priority"), Value(priority)),
            )
        (result.queued if updated else result.skipped).append(page.pk)
    return result
