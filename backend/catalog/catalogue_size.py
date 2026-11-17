"""The denominator: how large a site's catalogue is, and how much of it we hold (issue #107).

How much of a site's catalogue we hold is a share, and a share needs a denominator. On
2026-09-18 we could name one for only two of the enabled sites, so every sentence about "a small
share of what those sites publish" rested on two data points.

**No new request type, ever.** A figure reaches `catalog.SiteCatalogueSize` two ways and no other:

1. from a page the crawler was going to fetch anyway, because the site states the number in its own
   page furniture (`SiteAdapter.catalogue_total` → `crawler.catalogue_totals.record`);
2. from a person typing in what a recorded reconnaissance note already holds
   (`manage.py catalogue_size --record`), which is how a sitemap-derived figure gets in without
   anyone fetching a sitemap.

Three estimation methods were tried and failed (URL-id spans, per-tag counts in fixtures, stored
listing pagination). None of them is a method here.

**The share is monitored, never enforced.** `CATALOGUE_SHARE_ALERT_PERCENT` is a *monitoring*
threshold: crossing it mails the operator once and changes nothing about the crawl. It is not a
legal line and must not be read as one; the default is set where it cannot fire on today's
practice and still arrives early (see `DEFAULT_ALERT_PERCENT` in `marceline/settings.py`).

This module owns the record. Counting pages is the `index` side's job (`index.catalogue_share`):
`catalog` does not import `index`.
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from catalog.models import CatalogueSizeMethod, Site, SiteCatalogueSize

#: Which method may replace which. A figure a person entered, or one counted from the site's own
#: sitemap, is never overwritten by a number scraped off a listing page; a fresher figure of the
#: same standing is. Higher wins.
METHOD_RANK: dict[str, int] = {
    CatalogueSizeMethod.LISTING_PAGES: 0,
    CatalogueSizeMethod.STATED_ON_SITE: 1,
    CatalogueSizeMethod.SITEMAP_COUNT: 2,
    CatalogueSizeMethod.MANUAL: 3,
}

#: An unchanged figure is re-stamped at most this often. The crawler sees the same number on every
#: page of a site that prints it in its footer; without this, every parsed page would be an UPDATE.
RESTAMP_AFTER = timedelta(hours=6)


def alert_percent() -> float:
    return float(getattr(settings, "CATALOGUE_SHARE_ALERT_PERCENT", 0.0) or 0.0)


def may_replace(stored: str, incoming: str) -> bool:
    """May a figure obtained by `incoming` overwrite one obtained by `stored`?"""
    return METHOD_RANK.get(incoming, 0) >= METHOD_RANK.get(stored, 0)


def record_total(
    site: Site,
    total: int,
    method: str,
    *,
    source_path: str = "",
    note: str = "",
    recorded_by: str = "",
    now: datetime | None = None,
) -> bool:
    """Store (or refresh) this site's catalogue total. True when the row was written.

    Idempotent and safe to call on every parsed page: an unchanged figure of the same standing is
    re-stamped at most once per `RESTAMP_AFTER`, and a figure of lower standing than the stored one
    is dropped without a write.
    """
    moment = now or timezone.now()
    if total <= 0:
        return False
    record = SiteCatalogueSize.objects.filter(site=site).first()
    if record is None:
        SiteCatalogueSize.objects.create(
            site=site,
            total=total,
            method=method,
            source_path=source_path,
            note=note,
            recorded_by=recorded_by,
            observed_at=moment,
        )
        return True
    if not may_replace(record.method, method):
        return False
    if (
        record.total == total
        and record.method == method
        and moment - record.observed_at < RESTAMP_AFTER
    ):
        return False
    record.total = total
    record.method = method
    record.observed_at = moment
    if source_path:
        record.source_path = source_path
    if note:
        record.note = note
    if recorded_by:
        record.recorded_by = recorded_by
    record.save(
        update_fields=["total", "method", "observed_at", "source_path", "note", "recorded_by"]
    )
    return True


def over_threshold(record: SiteCatalogueSize, *, percent: float | None = None) -> bool:
    """Is the share we have **ever** indexed at or above the monitoring threshold?

    Ever-indexed, not active: a page we deactivated and scrubbed was still taken once. A threshold of 0 switches the alert off.
    """
    limit = alert_percent() if percent is None else percent
    share = record.share_indexed
    return bool(limit > 0 and share is not None and share >= limit)


def _percent(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f} %"


def size_lines(site: Site) -> list[str]:
    """One site's catalogue share, for `request_report` and the Site admin. Never a URL."""
    record = SiteCatalogueSize.objects.filter(site=site).first()
    if record is None:
        return [
            "  catalogue: total unknown — no share can be computed "
            "(manage.py catalogue_size --record)"
        ]
    counted = (
        f"counted {record.counted_at:%Y-%m-%d}" if record.counted_at else "pages never counted"
    )
    flag = " — OVER the monitoring threshold" if over_threshold(record) else ""
    return [
        f"  catalogue: {record.total:,} video(s) by the site's own account "
        f"({record.method}, {record.source_path or 'no path recorded'}, "
        f"seen {record.observed_at:%Y-%m-%d})",
        f"    our share: ever indexed {_percent(record.share_indexed)} "
        f"({record.pages_indexed:,}), active {_percent(record.share_active)} "
        f"({record.pages_active:,}); {counted}{flag}",
    ]
