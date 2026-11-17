"""Real request accounting and request budgets (ADR 0007 amendment 2026-09-17, issue #74).

Every outgoing HTTP request made through `core.http.PoliteClient` reaches `RequestAccounting`,
the client's observer. It keeps counters in memory per (UTC hour, site, host, host role, kind,
request type, outcome) and upserts them in batches into `catalog.RequestCount` — one row per
combination per hour, never a row per request. Hourly rows of days older than `HOURLY_RETENTION`
are rolled up into `RequestCountDaily` (`rollup_request_counts`), which is kept forever.

**Host roles.** `origin` is a site's own servers (its domain and subdomains), `cdn` a host serving
the site's media from elsewhere, `third_party` traffic that belongs to no site (RDAP registries,
the search API). `SiteHost` rows override the rule per site: reviewed CDN subdomains
(`SitePolicy.cdn_hosts`) and operator edits in the admin.

**Budgets** are keyed by (site or host, role, kind) and count real requests — retries and
redirect hops of that kind included, refused attempts excluded — in a rolling window of
`BUDGET_WINDOW_HOURS` hour buckets (the current hour and the 23 before it):
- `origin`/`page`: pages and robots.txt on the site's own hosts, `Site.daily_request_budget`;
- `origin`/`media`: media served from the site's own hosts, `Site.daily_media_request_budget`;
- `cdn`: every request to one CDN *host*, summed over all sites using it — a CDN shared by several
  sites has one budget: the lowest `SiteHost.daily_request_budget` set for it, else
  `CDN_HOST_DAILY_REQUEST_BUDGET`, far above the origin budgets (a CDN is built to serve media).
Other kinds (discovery probes, RDAP, review image passthrough) are counted but never refused.

**Overshoot.** `admit` reads the counters from the database at most every `USAGE_CACHE_S` seconds
(flushing its own buffer first) and adds the requests this process made since. What it cannot see
is what *other* processes sent since that read and have not flushed yet, and every process flushes
at least every `FLUSH_INTERVAL_S` while it sends. A budget is therefore overshot by at most
(USAGE_CACHE_S + FLUSH_INTERVAL_S) / per-host delay requests per other process using it — with one
crawler and one media worker per site and a 3 s delay, about 30 requests on a budget of thousands.
A crashed process loses at most its last `FLUSH_INTERVAL_S` of counts.

Names here and in the counter tables are stable: ADR 0030 (single-process runtime) reads them.
"""

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from django.db import DatabaseError, connection, transaction
from django.db.models import Min, Q, Sum
from django.utils import timezone

from catalog.models import HostRole, RequestCount, RequestCountDaily, Site, SiteHost
from catalog.sources import CDN_HOST_DAILY_REQUEST_BUDGET
from core.http import (
    OUTCOME_REFUSED,
    BudgetExhaustedError,
    RequestAttempt,
    RequestLabel,
    outcome_class,
)

logger = logging.getLogger(__name__)

BUDGET_WINDOW_HOURS = 24
HOURLY_RETENTION = timedelta(days=30)
FLUSH_INTERVAL_S = 30.0
FLUSH_MAX_KEYS = 500
USAGE_CACHE_S = 60.0
HOST_ROLES_CACHE_S = 300.0

# Request kinds callers name (`RequestLabel.kind`); `retry` and `redirect_hop` are request types
# the client derives for repeated attempts and followed redirects.
PAGE = "page"
ROBOTS = "robots"
MEDIA = "media"
PROBE = "probe"
RDAP = "rdap"
SEARCH_API = "search_api"
REVIEW_IMAGE = "review_image"
# The crawler's LLM provider (ADR 0042, issue #22): a paid API with a credential, like
# `search_api`. Counted for the record, never against a site's crawl budget — it is not the
# site's server being asked.
LLM_API = "llm_api"
# One-off download of the pinned embedding model (ADR 0037). Traffic of no site, like `rdap`:
# counted for the record, never against a site's budget.
MODEL = "model"

# Origin budgets and the kinds they cover.
PAGE_BUDGET = "page"
MEDIA_BUDGET = "media"
ORIGIN_BUDGET_KINDS: dict[str, tuple[str, ...]] = {
    PAGE_BUDGET: (PAGE, ROBOTS),
    MEDIA_BUDGET: (MEDIA,),
}
# The CDN host budget covers every enforced kind.
ANY_KIND = "*"

# (hour, site id, host, role, kind, request type, outcome)
CounterKey = tuple[datetime, int | None, str, str, str, str, str]
# ("origin", site id, "page" | "media") or ("cdn", host, "*")
BudgetScope = tuple[str, int | str, str]

_HOSTNAME = re.compile(r"^[a-z0-9.-]{1,255}$")


def hour_floor(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def window_start(now: datetime) -> datetime:
    """The first hour bucket of the rolling budget window containing `now`."""
    return hour_floor(now) - timedelta(hours=BUDGET_WINDOW_HOURS - 1)


def next_window_change(now: datetime) -> datetime:
    """When the oldest hour bucket leaves the window: the earliest a used-up budget can free up."""
    return hour_floor(now) + timedelta(hours=1)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_site_host(host: str, domain: str) -> bool:
    host, domain = host.lower(), domain.lower()
    return host == domain or host.endswith(f".{domain}")


def default_role(host: str, domain: str) -> str:
    """The rule without a `SiteHost` row: the site's domain and its subdomains are origin."""
    return HostRole.ORIGIN if is_site_host(host, domain) else HostRole.CDN


def origin_budget_of(kind: str) -> str | None:
    """Which origin budget a request kind uses, or None when it is never refused."""
    for budget, kinds in ORIGIN_BUDGET_KINDS.items():
        if kind in kinds:
            return budget
    return None


def site_budget(site: Site, budget: str) -> int:
    return site.daily_media_request_budget if budget == MEDIA_BUDGET else site.daily_request_budget


# --- counting, read side -----------------------------------------------------------------------


def origin_usage(
    site_ids: Iterable[int], now: datetime, *, budget: str = PAGE_BUDGET
) -> dict[int, int]:
    """Origin requests per site in the budget window for one origin budget (refusals excluded)."""
    rows = (
        RequestCount.objects.filter(
            site_id__in=list(site_ids),
            role=HostRole.ORIGIN,
            kind__in=ORIGIN_BUDGET_KINDS[budget],
            hour__gte=window_start(now),
        )
        .exclude(outcome=OUTCOME_REFUSED)
        .values("site_id")
        .annotate(total=Sum("requests"))
        .order_by()
    )
    return {row["site_id"]: int(row["total"] or 0) for row in rows}


def cdn_host_usage(host: str, now: datetime) -> int:
    """CDN requests to `host` in the budget window, across every site."""
    total = (
        RequestCount.objects.filter(host=host, role=HostRole.CDN, hour__gte=window_start(now))
        .exclude(outcome=OUTCOME_REFUSED)
        .aggregate(total=Sum("requests"))["total"]
    )
    return int(total or 0)


def cdn_host_budget(host: str) -> int:
    """The lowest budget any site set for this CDN host, else the default."""
    lowest = SiteHost.objects.filter(
        host=host, role=HostRole.CDN, daily_request_budget__isnull=False
    ).aggregate(lowest=Min("daily_request_budget"))["lowest"]
    return int(lowest) if lowest is not None else CDN_HOST_DAILY_REQUEST_BUDGET


def over_budget_site_ids(
    now: datetime, *, site_domain: str | None = None, budget: str = PAGE_BUDGET
) -> list[int]:
    """Enabled sites (or the one named) whose origin requests used up one origin budget."""
    sites = list(Site.objects.filter(enabled=True))
    if site_domain:
        sites = [site for site in sites if site.domain == site_domain]
    usage = origin_usage([site.pk for site in sites], now, budget=budget)
    return sorted(site.pk for site in sites if usage.get(site.pk, 0) >= site_budget(site, budget))


# --- host roles --------------------------------------------------------------------------------


class HostRoles:
    """Per-process view of each site's domain and `SiteHost` rows, refreshed every few minutes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sites: dict[int, tuple[float, str, dict[str, str]]] = {}

    def role(self, label: RequestLabel, host: str) -> str:
        if label.role:
            return label.role
        if label.site_id is None:
            return HostRole.THIRD_PARTY
        domain, overrides = self._site(label.site_id)
        return overrides.get(host) or default_role(host, domain)

    def _site(self, site_id: int) -> tuple[str, dict[str, str]]:
        cached = self._sites.get(site_id)
        if cached is not None and self._clock() - cached[0] < HOST_ROLES_CACHE_S:
            return cached[1], cached[2]
        domain = Site.objects.filter(pk=site_id).values_list("domain", flat=True).first() or ""
        overrides = dict(SiteHost.objects.filter(site_id=site_id).values_list("host", "role"))
        self._sites[site_id] = (self._clock(), domain, overrides)
        return domain, overrides


# --- the observer ------------------------------------------------------------------------------


@dataclass
class _Usage:
    read_at: float
    window: datetime
    used: int
    budget: int


def budget_scope(role: str, attempt: RequestAttempt) -> BudgetScope | None:
    """The budget a request uses: one of its site's origin budgets, or its CDN host's budget.

    Traffic of no site and kinds without a budget (probes, RDAP, review images) use none.
    """
    label = attempt.label
    if label.site_id is None:
        return None
    if role == HostRole.ORIGIN:
        budget = origin_budget_of(label.kind)
        return None if budget is None else (HostRole.ORIGIN, label.site_id, budget)
    if role == HostRole.CDN:
        return (HostRole.CDN, attempt.host, ANY_KIND)
    return None


class RequestAccounting:
    """`core.http.RequestObserver` backed by Postgres; one per client."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._clock = clock
        self._now = now
        self._roles = HostRoles(clock)
        self._pending: dict[CounterKey, list[int]] = {}
        self._first_pending_at: float | None = None
        self._usage: dict[BudgetScope, _Usage] = {}

    def admit(self, attempt: RequestAttempt) -> None:
        """Refuse when the request's budget is used up (enforced labels only)."""
        if not attempt.label.enforce:
            return
        scope = budget_scope(self._roles.role(attempt.label, attempt.host), attempt)
        if scope is None:
            return
        now = self._now()
        usage = self._current_usage(scope, now)
        if usage.used < usage.budget:
            return
        # Queue filters read the database (`over_budget_site_ids`): after this flush they know at
        # least what this refusal knew, so a worker does not claim work it will only refuse.
        self.flush()
        role, key, budget = scope
        what = f"site {key} origin {budget}" if role == HostRole.ORIGIN else f"CDN host {key}"
        raise BudgetExhaustedError(
            f"daily request budget used up: {what} {usage.used}/{usage.budget}",
            scope=role if role == HostRole.CDN else f"{role}:{budget}",
            host=attempt.host,
            resume_at=next_window_change(now),
        )

    def record(self, attempt: RequestAttempt, outcome: str, size: int) -> None:
        role = self._roles.role(attempt.label, attempt.host)
        key: CounterKey = (
            hour_floor(self._now()),
            attempt.label.site_id,
            attempt.host[:255],
            role,
            attempt.label.kind[:32],
            attempt.request_type[:32],
            outcome[:16],
        )
        counter = self._pending.setdefault(key, [0, 0])
        counter[0] += 1
        counter[1] += max(size, 0)
        if self._first_pending_at is None:
            self._first_pending_at = self._clock()
        scope = budget_scope(role, attempt)
        if outcome != OUTCOME_REFUSED and scope is not None and scope in self._usage:
            self._usage[scope].used += 1
        if (
            len(self._pending) >= FLUSH_MAX_KEYS
            or self._clock() - self._first_pending_at >= FLUSH_INTERVAL_S
        ):
            self.flush()

    def flush(self) -> None:
        """Upsert the buffered counters; on a database error keep them for the next try."""
        if not self._pending:
            return
        try:
            upsert_counts(self._pending)
        except DatabaseError:
            logger.exception("could not store request counters; keeping them for the next flush")
            return
        self._pending = {}
        self._first_pending_at = None

    def _current_usage(self, scope: BudgetScope, now: datetime) -> _Usage:
        usage = self._usage.get(scope)
        window = window_start(now)
        if (
            usage is not None
            and usage.window == window
            and self._clock() - usage.read_at < USAGE_CACHE_S
        ):
            return usage
        self.flush()
        role, key, budget = scope
        if role == HostRole.ORIGIN and isinstance(key, int):
            used = origin_usage([key], now, budget=budget).get(key, 0)
            site = Site.objects.filter(pk=key).first()
            limit = site_budget(site, budget) if site is not None else 0
        else:
            used, limit = cdn_host_usage(str(key), now), cdn_host_budget(str(key))
        usage = _Usage(read_at=self._clock(), window=window, used=used, budget=limit)
        self._usage[scope] = usage
        return usage


def upsert_counts(counts: dict[CounterKey, list[int]]) -> None:
    """Add counters to their hour rows.

    Keys are written in sorted order, so concurrent flushes of several processes lock rows in the
    same order and cannot deadlock. A savepoint when the caller has a transaction open, so a
    failed flush cannot break it.
    """
    keys = sorted(counts, key=lambda key: (key[0], key[1] or 0, *key[2:]))
    table = RequestCount._meta.db_table
    rows = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(keys))
    params: list[Any] = []
    for key in keys:
        params.extend((*key, counts[key][0], counts[key][1]))
    sql = (
        f"INSERT INTO {table} "
        "(hour, site_id, host, role, kind, request_type, outcome, requests, bytes) "
        f"VALUES {rows} ON CONFLICT ON CONSTRAINT uniq_request_count_hour DO UPDATE SET "
        f"requests = {table}.requests + EXCLUDED.requests, bytes = {table}.bytes + EXCLUDED.bytes"
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


class Flushable(Protocol):
    def flush(self) -> None: ...


def idle_sleep(
    client: Flushable, sleep: Callable[[float], None] = time.sleep
) -> Callable[[float], None]:
    """A worker's idle sleep that first hands its buffered counters to the database, so an idle
    worker's last requests show up in reports and in other processes' budget checks."""

    def idle(seconds: float) -> None:
        client.flush()
        sleep(seconds)

    return idle


def record_request(
    url: str, *, kind: str, status: int | None, size: int = 0, role: str = HostRole.THIRD_PARTY
) -> None:
    """Count one request made without `PoliteClient` (the DataForSEO API client)."""
    accounting = RequestAccounting()
    attempt = RequestAttempt(url, host_of(url), RequestLabel(kind, role=role, enforce=False))
    accounting.record(attempt, outcome_class(status), size)
    accounting.flush()


# --- retention ---------------------------------------------------------------------------------


def rollup_request_counts(now: datetime | None = None) -> int:
    """Move hourly rows of whole UTC days older than `HOURLY_RETENTION` into daily rows.

    One statement: rows are deleted and added to their day together, so a failed run changes
    nothing and a second run finds nothing left to move. Returns the hourly rows moved.
    """
    now = now or timezone.now()
    cutoff = datetime.combine(_day(now - HOURLY_RETENTION), datetime.min.time(), tzinfo=UTC)
    hourly, daily = RequestCount._meta.db_table, RequestCountDaily._meta.db_table
    columns = "site_id, host, role, kind, request_type, outcome"
    sql = f"""
        WITH moved AS (DELETE FROM {hourly} WHERE hour < %s RETURNING *),
        totals AS (
            INSERT INTO {daily} (day, {columns}, requests, bytes)
            SELECT (hour AT TIME ZONE 'UTC')::date, {columns}, SUM(requests), SUM(bytes)
            FROM moved GROUP BY 1, 2, 3, 4, 5, 6, 7
            ON CONFLICT ON CONSTRAINT uniq_request_count_day DO UPDATE SET
                requests = {daily}.requests + EXCLUDED.requests,
                bytes = {daily}.bytes + EXCLUDED.bytes
        )
        SELECT COUNT(*) FROM moved
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, [cutoff])
        row = cursor.fetchone()
    return int(row[0]) if row else 0


# --- site hosts --------------------------------------------------------------------------------


def record_observed_hosts(pairs: Iterable[tuple[int, str]]) -> int:
    """Store (site id, host) pairs seen in media URLs with the rule's role; returns new rows.

    Existing rows are left alone: a policy or operator decision is never overwritten. The pairs
    come from `index.services.media_hosts_by_site` (catalog may not import index).
    """
    domains = dict(Site.objects.values_list("pk", "domain"))
    existing = set(SiteHost.objects.values_list("site_id", "host"))
    new: dict[tuple[int, str], SiteHost] = {}
    for site_id, raw_host in pairs:
        host = raw_host.lower()
        if site_id not in domains or (site_id, host) in existing or not _HOSTNAME.match(host):
            continue
        new[(site_id, host)] = SiteHost(
            site_id=site_id,
            host=host,
            role=default_role(host, domains[site_id]),
            source=SiteHost.Source.OBSERVED,
        )
    SiteHost.objects.bulk_create(new.values(), ignore_conflicts=True)
    return len(new)


def apply_policy_cdn_hosts(site: Site, hosts: Iterable[str]) -> None:
    """Mark a site's reviewed CDN hosts (`SitePolicy.cdn_hosts`); an operator's row wins."""
    for host in hosts:
        row = SiteHost.objects.filter(site=site, host=host).first()
        if row is None:
            SiteHost.objects.create(
                site=site, host=host, role=HostRole.CDN, source=SiteHost.Source.POLICY
            )
        elif row.source != SiteHost.Source.OPERATOR and (
            row.role != HostRole.CDN or row.source != SiteHost.Source.POLICY
        ):
            row.role, row.source = HostRole.CDN, SiteHost.Source.POLICY
            row.save(update_fields=("role", "source", "updated_at"))


# --- reporting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRow:
    site_id: int | None
    host: str
    role: str
    kind: str
    request_type: str
    outcome: str
    requests: int
    bytes: int


UsageKey = tuple[int | None, str, str, str, str, str]
_DIMENSIONS = ("site_id", "host", "role", "kind", "request_type", "outcome")


def usage_rows(since: datetime, *, site_ids: Iterable[int] | None = None) -> list[UsageRow]:
    """Counters since `since`, summed over time.

    Hourly rows, plus daily rows of days already rolled up; a range reaching past the hourly
    retention is therefore accurate to the day, not the hour.
    """
    site_filter = Q() if site_ids is None else Q(site_id__in=list(site_ids))
    totals: dict[UsageKey, list[int]] = {}
    hourly = RequestCount.objects.filter(site_filter, hour__gte=since)
    daily = RequestCountDaily.objects.filter(site_filter, day__gte=_day(since))
    for queryset in (hourly.values(*_DIMENSIONS), daily.values(*_DIMENSIONS)):
        for row in queryset.annotate(total=Sum("requests"), size=Sum("bytes")).order_by():
            key: UsageKey = (
                row["site_id"],
                row["host"],
                row["role"],
                row["kind"],
                row["request_type"],
                row["outcome"],
            )
            total = totals.setdefault(key, [0, 0])
            total[0] += int(row["total"] or 0)
            total[1] += int(row["size"] or 0)
    return [
        UsageRow(*key, *values)
        for key, values in sorted(totals.items(), key=lambda item: _sort_key(item[0]))
    ]


def _day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


def _sort_key(key: UsageKey) -> tuple[int, str, str, str, str, str]:
    site_id, host, role, kind, request_type, outcome = key
    return (site_id or 0, role, host, kind, request_type, outcome)
