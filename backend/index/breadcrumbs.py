"""Site x creator breadcrumbs (issue #71): which creators a site roughly carries, and since when.

Counts and dates only — never a URL or page content. Every creator counts, target or not: the
trail is what lets a creator who signs later be hunted on the sites that already carry them.
The source of truth is `Page.creators`, restricted to **active** pages (ADR 0029 amendment
2026-09-17): a deactivated page's credit link stays on the page itself (`marceline.targets
.apply_activity` needs it for the `linked` reactivation rule, so it is never cleared), but it
stops counting here the moment the page deactivates — the point of the amendment is that a
human browsing breadcrumbs sees no trace of who a now-untargeted page named. A page that is
inactive from before this change (never scrubbed of anything) is excluded the same way, at read
time, with no backfill needed.

One creator never counts either, however active its pages are: a name under an active suppression
(docs/suppression.md). A breadcrumb is a statement that a site carries someone's content, so it is
exactly what a suppression must not re-create — every writer here filters suppressed creators out,
whatever it was handed.
"""

from collections.abc import Iterable
from datetime import datetime

from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from catalog.suppression import suppressed_creator_ids, without_suppressed
from index.models import Page, SiteCreatorBreadcrumb

_BATCH = 5000


def record_page_links(
    page: Page, creator_ids: Iterable[int], *, now: datetime | None = None
) -> int:
    """Refresh the breadcrumbs of `page`'s site for these creators after the crawler linked them.

    Recounts the site's linked pages per creator (so a re-crawl of the same page never inflates
    the count), keeps the first sighting and moves the last one to `now`. Returns rows written.
    Suppressed creators are dropped here, so a re-crawl of a page that credits one writes nothing.
    """
    ids = without_suppressed(creator_ids)
    if not ids:
        return 0
    now = now or timezone.now()
    counts = {
        row["creator_id"]: row["pages"]
        for row in Page.creators.through.objects.filter(
            page__site_id=page.site_id, creator_id__in=ids, page__active=True
        )
        .values("creator_id")
        .annotate(pages=Count("page_id"))
    }
    rows = [
        SiteCreatorBreadcrumb(
            site_id=page.site_id,
            creator_id=creator_id,
            page_count=counts.get(creator_id, 0),
            first_seen=min(page.first_seen or now, now),
            last_seen=now,
        )
        for creator_id in ids
    ]
    SiteCreatorBreadcrumb.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=("site", "creator"),
        update_fields=("page_count", "last_seen", "updated_at"),
    )
    return len(rows)


def recount_breadcrumbs(site_id: int, creator_ids: Iterable[int]) -> int:
    """Recount existing breadcrumbs of one site after links were removed. Dates stay: the
    sightings happened. A pair without pages keeps its row at 0, as `rebuild_breadcrumbs` does.
    Returns rows updated."""
    ids = sorted(set(creator_ids))
    if not ids:
        return 0
    counts = {
        row["creator_id"]: row["pages"]
        for row in Page.creators.through.objects.filter(
            page__site_id=site_id, creator_id__in=ids, page__active=True
        )
        .values("creator_id")
        .annotate(pages=Count("page_id"))
    }
    now = timezone.now()
    rows = list(SiteCreatorBreadcrumb.objects.filter(site_id=site_id, creator_id__in=ids))
    for row in rows:
        row.page_count = counts.get(row.creator_id, 0)
        row.updated_at = now
    return SiteCreatorBreadcrumb.objects.bulk_update(rows, ("page_count", "updated_at"))


def breadcrumb_rows() -> list[SiteCreatorBreadcrumb]:
    """Read-only: the breadcrumbs the index supports right now, one per (site, creator).

    Suppressed creators are left out even if a stale link still exists, so a rebuild can never
    resurrect a breadcrumb a suppression removed.
    """
    suppressed = suppressed_creator_ids()
    return [
        SiteCreatorBreadcrumb(
            site_id=row["page__site_id"],
            creator_id=row["creator_id"],
            page_count=row["pages"],
            first_seen=row["first"],
            last_seen=row["last"],
        )
        for row in Page.creators.through.objects.filter(page__active=True)
        .exclude(creator_id__in=suppressed)
        .values("page__site_id", "creator_id")
        .annotate(
            pages=Count("page_id"), first=Min("page__first_seen"), last=Max("page__last_seen")
        )
        .order_by("page__site_id", "creator_id")
    ]


@transaction.atomic
def rebuild_breadcrumbs() -> int:
    """Recompute every breadcrumb from `Page.creators`, active pages only. Idempotent; returns
    rows written.

    A pair whose pages are all gone, or all deactivated, keeps its row with `page_count` 0: the
    sighting happened, and a page's own link is never touched by this — it just stops counting.
    """
    rows = breadcrumb_rows()
    SiteCreatorBreadcrumb.objects.update(page_count=0)
    SiteCreatorBreadcrumb.objects.bulk_create(
        rows,
        batch_size=_BATCH,
        update_conflicts=True,
        unique_fields=("site", "creator"),
        update_fields=("page_count", "first_seen", "last_seen", "updated_at"),
    )
    return len(rows)
