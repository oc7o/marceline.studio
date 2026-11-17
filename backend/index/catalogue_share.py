"""How much of each site's catalogue we hold (issue #107, ADR 0049).

`catalog.catalogue_size` owns the **denominator** — the site's own stated or derivable total, with
its provenance. This module owns the **numerator**, because counting pages means reading
`index.Page`, and `catalog` does not import `index` (module boundaries, root `CLAUDE.md`).

Two numbers per site, always both, because targeted indexing (ADR 0029) makes them very different
and the honest statement needs both:

* **ever indexed** — every page of the site we have ever stored: a page we deactivated and
  scrubbed was still taken once.
* **active** — what is still live under targeted indexing. On 2026-09-18, 58 % of everything ever
  indexed was already deactivated and scrubbed, so the active share is far smaller. Reporting only
  that number would be flattering, and reporting only the other would be pessimistic.

The counts are **stamped into** `catalog.SiteCatalogueSize` (`pages_indexed`, `pages_active`,
`counted_at`) rather than computed live, so the Site admin and `request_report` — which live in
`catalog` and may not count pages — can show the share without a boundary violation. `counted_at`
says how old the number is.

Makes no request, writes no page, and reads nothing but two counts per site.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Q
from django.utils import timezone

from catalog import catalogue_alerts
from catalog.catalogue_size import alert_percent, over_threshold
from catalog.models import Site, SiteCatalogueSize
from index.models import Page


@dataclass(frozen=True)
class SiteShare:
    """One site's denominator, numerator and share. `total is None` means honestly unknown."""

    site_id: int
    domain: str
    enabled: bool
    pages_indexed: int
    pages_active: int
    total: int | None = None
    method: str = ""
    source_path: str = ""
    observed_at: datetime | None = None
    over: bool = False

    @property
    def share_indexed(self) -> float | None:
        return None if not self.total else 100.0 * self.pages_indexed / self.total

    @property
    def share_active(self) -> float | None:
        return None if not self.total else 100.0 * self.pages_active / self.total


def page_counts() -> dict[int, tuple[int, int]]:
    """Pages ever indexed and still active, per site id. Two aggregates, one query."""
    rows = Page.objects.values("site_id").annotate(
        ever=Count("pk"), live=Count("pk", filter=Q(active=True))
    )
    return {int(row["site_id"]): (int(row["ever"]), int(row["live"])) for row in rows}


def measure(*, domain: str | None = None) -> list[SiteShare]:
    """Every site (or the one named) with its counts and, where known, its share. Read-only."""
    sites = Site.objects.all()
    if domain:
        sites = sites.filter(domain=domain)
    counts = page_counts()
    records = {
        record.site_id: record
        for record in SiteCatalogueSize.objects.filter(site__in=sites).select_related("site")
    }
    shares: list[SiteShare] = []
    for site in sites.order_by("domain"):
        ever, live = counts.get(site.pk, (0, 0))
        record = records.get(site.pk)
        if record is None:
            shares.append(
                SiteShare(
                    site_id=site.pk,
                    domain=site.domain,
                    enabled=site.enabled,
                    pages_indexed=ever,
                    pages_active=live,
                )
            )
            continue
        # The stored counts may be older than this measurement; report what we just counted.
        record.pages_indexed, record.pages_active = ever, live
        shares.append(
            SiteShare(
                site_id=site.pk,
                domain=site.domain,
                enabled=site.enabled,
                pages_indexed=ever,
                pages_active=live,
                total=record.total,
                method=record.method,
                source_path=record.source_path,
                observed_at=record.observed_at,
                over=over_threshold(record),
            )
        )
    return shares


def refresh_counts(*, now: datetime | None = None) -> list[SiteCatalogueSize]:
    """Stamp today's page counts into every catalogue-size record. Returns the records."""
    moment = now or timezone.now()
    counts = page_counts()
    records = list(SiteCatalogueSize.objects.select_related("site"))
    for record in records:
        record.pages_indexed, record.pages_active = counts.get(record.site_id, (0, 0))
        record.counted_at = moment
    if records:
        SiteCatalogueSize.objects.bulk_update(
            records, ["pages_indexed", "pages_active", "counted_at"]
        )
    return records


def evaluate(*, now: datetime | None = None) -> tuple[list[SiteCatalogueSize], int]:
    """Refresh the counts and alert the operator about sites that newly crossed the threshold.

    Returns the refreshed records and how many alerts were sent. It alerts and nothing else: no
    site is paused, disabled or slowed down by a share (ADR 0049).
    """
    records = refresh_counts(now=now)
    return records, catalogue_alerts.notify_over_threshold(records)


def _percent(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f} %"


def render_text(shares: list[SiteShare]) -> str:
    """The plain-text report of `manage.py catalogue_size`. Domains and counts, never a URL."""
    known = [share for share in shares if share.total is not None]
    lines = [
        f"Catalogue share, {len(known)} of {len(shares)} site(s) with a known total "
        f"(monitoring threshold {alert_percent():.1f} % of pages ever indexed).",
        "",
    ]
    for share in shares:
        state = "" if share.enabled else " (disabled)"
        lines.append(f"{share.domain}{state}")
        if share.total is None:
            lines.append(
                f"  total unknown; {share.pages_indexed:,} page(s) ever indexed, "
                f"{share.pages_active:,} active — no share can be computed"
            )
            continue
        flag = "  <-- OVER THE THRESHOLD" if share.over else ""
        seen = f", seen {share.observed_at:%Y-%m-%d}" if share.observed_at else ""
        lines.append(
            f"  catalogue {share.total:,} ({share.method}, "
            f"{share.source_path or 'no path recorded'}{seen})"
        )
        lines.append(
            f"  ever indexed {share.pages_indexed:,} = {_percent(share.share_indexed)}; "
            f"active {share.pages_active:,} = {_percent(share.share_active)}{flag}"
        )
    if not known:
        lines.append("")
        lines.append(
            "No site has a recorded catalogue total yet. A total is captured from pages the "
            "crawler already fetches, or entered by hand with --record; it is never fetched."
        )
    return "\n".join(lines)
