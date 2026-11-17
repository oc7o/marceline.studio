"""Capture a site's own catalogue total from a page the crawler already fetched (issue #107).

The crawl side of `catalog.catalogue_size`. It exists so that the worker's hot path has exactly one
line of new behaviour and one guarantee to keep: **this never causes a request**. It is handed the
HTML the fetch already produced, asks the adapter's optional `catalogue_total` hook whether the
site states its own size on that page, and writes the figure if it does.

Best effort in both directions: a hook that raises, a figure that is nonsense, or a database error
must never cost the page the worker just parsed — the caller swallows, and so does this.
"""

import logging

from catalog.catalogue_size import record_total
from catalog.models import Site
from crawler.models import CrawlTask
from crawler.sites.base import SiteAdapter, catalogue_total

logger = logging.getLogger(__name__)

#: A figure below this is not a catalogue: it is a per-tag, per-creator or per-page count that an
#: adapter picked up by accident. Sites we crawl publish tens of thousands of videos; the smallest
#: catalogue we have ever recorded a total for is 26,347. Rejecting here rather than in each
#: adapter keeps the rule in one place.
MIN_PLAUSIBLE_TOTAL = 1_000


def record(site: Site, adapter: SiteAdapter, kind: CrawlTask.Kind, url: str, html: str) -> bool:
    """Store the catalogue total this page states, if it states one. True when a row was written."""
    found = catalogue_total(adapter, kind, url, html)
    if found is None or found.total < MIN_PLAUSIBLE_TOTAL:
        return False
    return record_total(
        site,
        found.total,
        found.method,
        source_path=found.source_path,
        note=found.note,
        recorded_by="crawler",
    )
