"""Crawl worker: claim a task, obey robots.txt, fetch politely, parse with the adapter, store."""

import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from catalog import robots_policy
from catalog.models import Site
from catalog.request_accounting import PAGE, ROBOTS
from catalog.services import record_robots_crawl_delay
from catalog.sources import AgeConfirmation, AgeConfirmationError, parse_age_confirmation
from core.http import (
    BudgetExhaustedError,
    FetchResponse,
    HostBusyError,
    PoliteClient,
    RequestLabel,
    RequestRefusedError,
    ResponseTooLargeError,
)
from core.log_scrub import scrub_urls
from crawler import adapter_health, catalogue_totals, repair, services
from crawler.blocking import AGE_SELF_DECLARATION, AGE_VERIFICATION_PROVIDER, detect_block
from crawler.models import CrawlTask
from crawler.robots import (
    InvalidRobotsUrlError,
    RobotsCache,
    RobotsFacts,
    RobotsRules,
    RobotsUnavailableError,
    effective_delay_s,
)
from crawler.sites.base import ParseResult, SiteAdapter, has_card_media_only
from crawler.sites.registry import get_adapter

logger = logging.getLogger(__name__)

NOT_FOUND_STATUSES = frozenset({404, 410})
# Errors that say the site itself could not be reached, not that this URL is bad (review M6).
UNREACHABLE_ERRORS: tuple[type[httpx.HTTPError], ...] = (
    httpx.NetworkError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)
# Look for expired media links to recover at start and then at most this often (review PERF: it
# ran on every idle tick, every 10 s, in each of the 17 crawler processes).
MEDIA_RECOVERY_INTERVAL_S = 10 * 60


class CrawlWorker:
    def __init__(
        self,
        client: PoliteClient,
        user_agent: str,
        *,
        on_result: Callable[[CrawlTask], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        robots: RobotsCache | None = None,
    ) -> None:
        """`robots` shares one robots.txt cache between workers (the crawl runtime's threads);
        every fetch for it still goes through this worker's client."""
        self._client = client
        self._clock = clock
        self._next_media_recovery_at: float | None = None
        # The site of the task being processed: robots.txt requests are counted against it.
        self._site_id: int | None = None
        self._robots = robots or RobotsCache(self._fetch_robots, user_agent)
        self._on_result = on_result
        self._unreachable = False
        # Set by `_on_robots` when this site was just paused for a TDM reservation (issue #105):
        # the task in hand is handed back untouched instead of being fetched.
        self._reservation_pause: str | None = None

    def run(
        self,
        *,
        site_domain: str | None = None,
        limit: int | None = None,
        once: bool = False,
        idle_sleep_s: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        """Process tasks until `limit` is reached; with `once` or `limit`, stop when idle."""
        drain = once or limit is not None
        processed = 0
        services.recover_stale_tasks()
        self._recover_expired_media(site_domain)
        while limit is None or processed < limit:
            task = services.claim_task(site_domain=site_domain)
            if task is None:
                if drain:
                    break
                sleep(idle_sleep_s)
                services.recover_stale_tasks()
                self._recover_expired_media(site_domain)
                continue
            self.process_claimed(task)
            processed += 1
            if self._on_result is not None:
                self._on_result(task)
            self._recover_expired_media(site_domain)
        return processed

    def _fetch_robots(self, url: str, delay_s: float) -> FetchResponse:
        return self._client.get(
            url, min_delay_s=delay_s, label=RequestLabel(ROBOTS, site_id=self._site_id)
        )

    def _rules_for(self, url: str, delay_s: float) -> RobotsRules:
        return self._robots.rules_for(
            url, min_delay_s=delay_s, fetch=self._fetch_robots, observer=self._on_robots
        )

    def _on_robots(self, origin: str, facts: RobotsFacts) -> None:
        """Record what this robots.txt reserves, and against whom (tripwire T2, issue #105).

        Called only when the file was actually fetched, so it costs no request. Never raises: a
        reservation is a finding about the site, and failing to store it must not fail the crawl —
        but a failure is logged at ERROR, because the tripwire going quiet is the thing to avoid.
        """
        if self._site_id is None:
            return
        try:
            host = urlsplit(origin).hostname or origin
            outcome = robots_policy.record(self._site_id, host, facts)
        except Exception:
            logger.exception("recording the robots.txt policy of site %s failed", self._site_id)
            return
        if outcome is not None and outcome.paused:
            self._reservation_pause = (
                f"{robots_policy.TDM_PAUSE_REASON}: robots.txt reserves this site against us"
            )

    def process_claimed(self, task: CrawlTask) -> None:
        """Process a claimed task: always leaves it in a non-RUNNING state, never raises."""
        self._process_safely(task)

    def _process_safely(self, task: CrawlTask) -> None:
        """`process`, but an unexpected error fails this task instead of stopping the worker.

        Without this, a poison task (review M5: an href with a newline) escaped `run`, stayed
        RUNNING, came back after STALE_LOCK_AFTER and killed the worker again.
        """
        fetched_before = task.fetched_at
        self._unreachable = False
        try:
            self.process(task)
        except Exception as exc:
            logger.exception("processing task %s failed", task.pk)
            services.abandon_task(task, exc)
            return
        if task.fetched_at != fetched_before and not self._unreachable:
            services.record_site_reachable(task.site)

    def _recover_expired_media(self, site_domain: str | None) -> None:
        now = self._clock()
        if self._next_media_recovery_at is not None and now < self._next_media_recovery_at:
            return
        self._next_media_recovery_at = now + MEDIA_RECOVERY_INTERVAL_S
        try:
            requeued = services.recover_expired_media(site_domain=site_domain)
        except Exception:  # recovery is best effort and must not stop the worker
            logger.exception("expired media recovery failed")
            return
        if requeued:
            logger.info("re-enqueued %d task(s) to recover expired media links", requeued)

    def process(self, task: CrawlTask) -> None:
        """Handle one claimed task; always leaves it in a non-RUNNING state."""
        site = task.site
        self._site_id = site.pk
        adapter = get_adapter(site.domain)
        if adapter is None:
            services.skip_task(task, f"no adapter for {site.domain}", fetched=False)
            return
        if not services.is_on_site(task.url, site.domain):
            services.skip_task(task, "URL is not on the site's domain", fetched=False)
            return
        if problem := url_problem(task.url):
            services.fail_task(task, f"invalid URL: {problem}", retry=False, fetched=False)
            return

        self._reservation_pause = None
        try:
            rules = self._rules_for(task.url, site.crawl_delay_s)
        except BudgetExhaustedError as exc:
            self._budget_exhausted(task, exc)
            return
        except HostBusyError as exc:
            self._host_busy(task, exc)
            return
        except RobotsUnavailableError as exc:
            self._site_unreachable(task, str(exc), robots=True)
            return
        except InvalidRobotsUrlError as exc:
            services.fail_task(task, f"invalid URL: {exc}", retry=False, fetched=False)
            return
        record_robots_crawl_delay(site, rules.crawl_delay_s)
        if reason := self._reservation_pause:
            # The robots.txt we just read reserves this site against us and the site is now paused
            # (issue #105). Nothing is fetched; the task waits, with its attempt refunded, until the
            # operator has reviewed the reservation.
            logger.warning("%s: %s", site.domain, reason)
            services.release_task(task, reason)
            return
        if not rules.can_fetch(task.url):
            services.skip_task(task, "disallowed by robots.txt", fetched=False)
            return

        confirmation = self._apply_age_confirmation(site)
        try:
            response = self._client.get(
                task.url,
                min_delay_s=effective_delay_s(site.crawl_delay_s, rules),
                on_redirect=self._redirect_policy(site),
                label=RequestLabel(PAGE, site_id=site.pk),
            )
        except BudgetExhaustedError as exc:
            self._budget_exhausted(task, exc)
            return
        except HostBusyError as exc:
            self._host_busy(task, exc)
            return
        except RequestRefusedError as exc:
            services.skip_task(task, exc.reason)
            return
        except RobotsUnavailableError as exc:
            services.fail_task(task, f"redirect target: {exc}")
            return
        except InvalidRobotsUrlError as exc:
            services.fail_task(task, f"invalid redirect target: {exc}", retry=False)
            return
        except ResponseTooLargeError as exc:
            services.fail_task(task, str(exc), retry=False)
            return
        except UNREACHABLE_ERRORS as exc:
            self._site_unreachable(task, f"{type(exc).__name__}: {exc}")
            return
        except httpx.HTTPError as exc:
            services.fail_task(task, f"{type(exc).__name__}: {exc}")
            return

        body = response.text
        block_reason = detect_block(response.status, response.headers, body, response.url)
        if block_reason:
            services.skip_task(task, f"blocked: {_explain_block(block_reason, task, confirmation)}")
        elif response.status in NOT_FOUND_STATUSES:
            if services.mark_removed(task):
                services.complete_task(task, reason=f"removed: HTTP {response.status}")
            else:
                services.skip_task(task, f"HTTP {response.status}")
        elif response.status >= 400:
            retry = response.status >= 500 or response.status == 429
            services.fail_task(task, f"HTTP {response.status}", retry=retry)
        elif not services.is_on_site(response.url, site.domain):
            services.skip_task(task, f"redirected off-site to {response.url}")
        elif not rules.can_fetch(response.url):
            services.skip_task(task, f"redirect target disallowed by robots.txt: {response.url}")
        else:
            self._parse_and_store(task, response.url, body)

    def _budget_exhausted(self, task: CrawlTask, exc: BudgetExhaustedError) -> None:
        """A request budget is used up (issue #74): nothing was sent, the task waits untouched
        until the budget can free up; `claim_task` skips the site's other tasks meanwhile."""
        logger.info("%s: %s", task.site.domain, exc.reason)
        services.release_task(task, exc.reason, not_before=exc.resume_at)

    def _host_busy(self, task: CrawlTask, exc: HostBusyError) -> None:
        """The host's database slot stayed taken (ADR 0030): nothing was sent, the task waits
        with its attempt refunded."""
        logger.info("%s: %s", task.site.domain, exc.reason)
        services.release_task(task, exc.reason, not_before=exc.retry_at)

    def _site_unreachable(self, task: CrawlTask, error: str, *, robots: bool = False) -> None:
        """A connection or robots.txt failure: pause the site once it looks down (review M6).

        Once paused, the task goes back untouched; until then it is an ordinary retry.
        """
        self._unreachable = True
        if services.record_site_failure(task.site, error, robots=robots) is not None:
            services.release_task(task, error)
        else:
            services.fail_task(task, error, fetched=not robots)

    def _redirect_policy(self, site: Site) -> Callable[[str], None]:
        """Allowlist and robots.txt for a redirect target, checked *before* it is requested.

        A target on another host of the site (www <-> apex) is judged by that host's own
        robots.txt (RFC 9309), fetched first if it is not cached. The final-URL checks in
        `process` stay as a second line.
        """

        def check(url: str) -> None:
            if not services.is_on_site(url, site.domain):
                raise RequestRefusedError(f"redirected off-site to {url}")
            if not self._rules_for(url, site.crawl_delay_s).can_fetch(url):
                raise RequestRefusedError(f"redirect target disallowed by robots.txt: {url}")

        return check

    def _apply_age_confirmation(self, site: Site) -> AgeConfirmation | None:
        """Send the site's approved age self-declaration cookies, or none (ADR 0007 amendment)."""
        try:
            confirmation = parse_age_confirmation(site.age_confirmation)
        except AgeConfirmationError as exc:
            # `Site.age_confirmation` is operator-entered config, not page content, but the
            # message-level scrub is applied here too rather than relied on being unnecessary.
            logger.warning(
                "ignoring invalid age_confirmation for %s: %s", site.domain, scrub_urls(str(exc))
            )
            confirmation = None
        self._client.set_domain_cookies(site.domain, confirmation.cookies if confirmation else {})
        return confirmation

    def _parse_and_store(self, task: CrawlTask, url: str, body: str) -> None:
        adapter = get_adapter(task.site.domain)
        assert adapter is not None
        self._watch_catalogue_total(task, url, body, adapter)
        try:
            result = adapter.parse(CrawlTask.Kind(task.kind), url, body)
            if task.kind == CrawlTask.Kind.DETAIL and result.removed:
                services.mark_removed(task)
            else:
                if task.kind == CrawlTask.Kind.DETAIL:
                    result = self._watch_extraction(task, result, body, adapter)
                services.store_parse_result(task, result)
        except Exception as exc:  # an adapter bug must not stop the worker
            logger.exception(
                "parse/store failed for crawl task id=%s site=%s", task.pk, task.site.domain
            )
            services.fail_task(task, f"parse error: {type(exc).__name__}: {exc}", retry=False)
            return
        services.complete_task(
            task, reason="removed" if task.kind == CrawlTask.Kind.DETAIL and result.removed else ""
        )

    def _watch_catalogue_total(
        self, task: CrawlTask, url: str, body: str, adapter: SiteAdapter
    ) -> None:
        """Record the catalogue size this page states, if the site states one (issue #107).

        The denominator of our catalogue share, taken from a page we fetched anyway
        — it costs no request (`crawler.catalogue_totals`, ADR 0049). Best effort: a figure is
        never worth losing a page over.
        """
        try:
            catalogue_totals.record(task.site, adapter, CrawlTask.Kind(task.kind), url, body)
        except Exception:
            logger.exception("recording the catalogue total failed for site=%s", task.site.domain)

    def _watch_extraction(
        self, task: CrawlTask, result: ParseResult, body: str, adapter: SiteAdapter
    ) -> ParseResult:
        """Count what this detail parse yielded (issue #22) and, when the site's adapter is
        recorded broken, let the repair fill in what it left empty (ADR 0042).

        An adapter that has stopped working does not crash, it goes quiet, so the only way to
        notice is to count what it yields (`crawler.adapter_health`). Neither half may ever cost
        a page: any failure here leaves `result` exactly as the adapter produced it.

        The adapter is passed because it decides one half of the rule: on a site whose media come
        from the listing card an empty detail-page media tuple is the parser working
        (`has_card_media_only`), and counting it as blind both alarmed falsely and, with the repair
        on, would have spent its daily cap on pages nothing is wrong with.
        """
        try:
            outcome = adapter_health.outcome_of(
                result, media_counts=not has_card_media_only(adapter)
            )
            adapter_health.record_parse(task.site_id, outcome)
        except Exception:
            logger.exception("recording the parse outcome failed for site=%s", task.site.domain)
            return result
        if not outcome.blind:
            return result
        try:
            repaired = repair.repair_parse(task, result, body)
        except Exception:
            logger.exception("adapter repair failed for crawl task id=%s", task.pk)
            return result
        if repaired is not result:
            try:
                adapter_health.record_repair(task.site_id)
            except Exception:
                logger.exception("recording a repair failed for site=%s", task.site.domain)
        return repaired


def url_problem(url: str) -> str:
    """Why `url` cannot be requested at all (control characters, bad host or port), or "".

    Such a task can never succeed: it is failed at once instead of escaping the worker and being
    re-claimed for ever (review M5). Parsing only — nothing is requested.
    """
    try:
        httpx.URL(url)
    except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def _explain_block(reason: str, task: CrawlTask, confirmation: AgeConfirmation | None) -> str:
    if reason.startswith(AGE_VERIFICATION_PROVIDER):
        logger.warning(
            "%s: skipped, verification is never automated (crawl task id=%s site=%s)",
            reason,
            task.pk,
            task.site.domain,
        )
        return f"{reason} (always skipped, never automated)"
    if reason == AGE_SELF_DECLARATION:
        if confirmation is None:
            return f"{reason} gate, no operator approval (Site.age_confirmation)"
        logger.warning(
            "age gate still shown with approved confirmation (crawl task id=%s site=%s)",
            task.pk,
            task.site.domain,
        )
        return f"{reason} gate still shown with the approved age confirmation cookies"
    return reason
