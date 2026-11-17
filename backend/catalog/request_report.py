"""Plain-text request metrics per site: origin vs CDN, by type and outcome (issue #74).

Shared by `manage.py request_report` and the Site admin, so both always say the same. Totals
exclude refused attempts (nothing was sent); the outcome breakdown lists them separately.
"""

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from catalog.catalogue_size import size_lines
from catalog.media_shapes import shape_lines
from catalog.models import HostRole, Site
from catalog.request_accounting import (
    ORIGIN_BUDGET_KINDS,
    UsageRow,
    cdn_host_budget,
    cdn_host_usage,
    site_budget,
    usage_rows,
    window_start,
)
from catalog.robots_policy import policy_lines
from core.http import OUTCOME_REFUSED


@dataclass
class Breakdown:
    requests: int = 0
    bytes: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    by_outcome: Counter[str] = field(default_factory=Counter)

    def add(self, row: UsageRow) -> None:
        self.by_outcome[row.outcome] += row.requests
        if row.outcome == OUTCOME_REFUSED:
            return
        self.requests += row.requests
        self.bytes += row.bytes
        self.by_type[row.request_type] += row.requests


def breakdown(rows: Iterable[UsageRow], keep: Callable[[UsageRow], bool]) -> Breakdown:
    result = Breakdown()
    for row in rows:
        if keep(row):
            result.add(row)
    return result


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{key} {value:,}" for key, value in sorted(counter.items())) or "none"


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:,.1f} MB"


def _summary(label: str, recent: Breakdown, period: Breakdown, days: int, budget: str) -> list[str]:
    return [
        f"  {label} 24h: {recent.requests:,}{budget} ({_megabytes(recent.bytes)}); "
        f"{days}d: {period.requests:,} ({_megabytes(period.bytes)})",
        f"    24h by type: {_counts(recent.by_type)}; by outcome: {_counts(recent.by_outcome)}",
        f"    {days}d by type: {_counts(period.by_type)}; by outcome: {_counts(period.by_outcome)}",
    ]


def site_lines(
    site: Site, window: list[UsageRow], period: list[UsageRow], *, days: int, now: datetime
) -> list[str]:
    """One site's origin budgets and CDN hosts, in the budget window and over `days`."""
    lines = [f"{site.domain}{'' if site.enabled else ' (disabled)'}"]

    def mine(role: str, kinds: Iterable[str] | None = None) -> Callable[[UsageRow], bool]:
        wanted = None if kinds is None else set(kinds)
        return lambda row: (
            row.site_id == site.pk and row.role == role and (wanted is None or row.kind in wanted)
        )

    for budget, kinds in ORIGIN_BUDGET_KINDS.items():
        keep = mine(HostRole.ORIGIN, kinds)
        limit = f"/{site_budget(site, budget):,}"
        lines += _summary(
            f"origin {budget}", breakdown(window, keep), breakdown(period, keep), days, limit
        )
    budgeted = {kind for kinds in ORIGIN_BUDGET_KINDS.values() for kind in kinds}
    origin = mine(HostRole.ORIGIN)

    def unbudgeted(row: UsageRow) -> bool:
        return origin(row) and row.kind not in budgeted

    if any(unbudgeted(row) for row in period):
        lines += _summary(
            "origin, no budget",
            breakdown(window, unbudgeted),
            breakdown(period, unbudgeted),
            days,
            "",
        )
    hosts = sorted({row.host for row in period if mine(HostRole.CDN)(row)})
    cdn = mine(HostRole.CDN)
    cdn_now, cdn_period = breakdown(window, cdn), breakdown(period, cdn)
    lines.append(
        f"  cdn 24h: {cdn_now.requests:,} ({_megabytes(cdn_now.bytes)}); {days}d: "
        f"{cdn_period.requests:,} ({_megabytes(cdn_period.bytes)}) on {len(hosts)} host(s)"
    )
    for host in hosts:

        def on_host(row: UsageRow, host: str = host) -> bool:
            return cdn(row) and row.host == host

        recent, total = breakdown(window, on_host), breakdown(period, on_host)
        lines.append(
            f"    {host}: 24h {recent.requests:,} (all sites {cdn_host_usage(host, now):,}/"
            f"{cdn_host_budget(host):,}); {days}d {total.requests:,}; "
            f"24h by type: {_counts(recent.by_type)}; by outcome: {_counts(recent.by_outcome)}"
        )
    # Which media URL *shapes* of this site keep answering "not here" (ADR 0041). Shapes, never
    # URLs: the starred form is what an operator needs to fix an adapter, and the identifiers that
    # would make it a real piracy URL are exactly what the shaping removes.
    lines += shape_lines(site, now)
    # How much of the site's own catalogue we hold (issue #107, ADR 0049): the denominator of the
    # catalogue share, with where the figure came from. Counts and a share only.
    lines += size_lines(site)
    # What this site's robots.txt reserves, and against whom (tripwire T2, issue #105). Tokens the
    # site itself publishes, never a path or a line of the file.
    lines += policy_lines(site, now)
    return lines


def report_lines(now: datetime, *, days: int = 7, domain: str | None = None) -> list[str]:
    """The whole report: every site (or the one named), then traffic that belongs to no site."""
    sites = list(Site.objects.order_by("domain"))
    if domain:
        sites = [site for site in sites if site.domain == domain]
    since = now - timedelta(days=days)
    site_ids = None if domain is None else [site.pk for site in sites]
    period = usage_rows(since, site_ids=site_ids)
    window = usage_rows(window_start(now), site_ids=site_ids)
    lines = [
        f"Requests: budget window since {window_start(now):%Y-%m-%d %H:%M} UTC (24h), "
        f"period since {since:%Y-%m-%d %H:%M} UTC ({days}d). Refused attempts are not requests."
    ]
    for site in sites:
        if site.enabled or any(row.site_id == site.pk for row in period):
            lines.extend(site_lines(site, window, period, days=days, now=now))
    if domain is None:
        siteless = sorted({(row.role, row.host) for row in period if row.site_id is None})
        if siteless:
            lines.append("no site")
        for role, host in siteless:

            def keep(row: UsageRow, role: str = role, host: str = host) -> bool:
                return row.site_id is None and row.role == role and row.host == host

            recent, total = breakdown(window, keep), breakdown(period, keep)
            lines.append(
                f"  {host} ({role}): 24h {recent.requests:,}; {days}d {total.requests:,}; "
                f"{days}d by type: {_counts(total.by_type)}; by outcome: "
                f"{_counts(total.by_outcome)}"
            )
    return lines
