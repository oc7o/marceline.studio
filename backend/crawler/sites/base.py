"""Site adapter interface.

An adapter turns one site's HTML into crawl results. Adapters are pure: no network, no database,
no clock. The worker fetches, calls `parse`, and `crawler.services` stores the result.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from catalog.models import CatalogueSizeMethod, Creator, Site
from crawler.models import CrawlTask
from index.models import Media

# Crawl priorities shared by all adapters. The worker claims by priority (desc), then creation
# order, so this schedules breadth first: every creator's seed pages, then the top results of each
# seed, then the rest of page 1, then deeper pages.
EXPIRING_DETAIL_PRIORITY = 40
SEED_PRIORITY = 30
# Seeds of watchlist entries marked `crawl` (issue #71): below every rights holder and every
# co-performer of an owned original (marceline.targets), above ordinary details.
WATCHLIST_SEED_PRIORITY = 11
# Seeds of prospect research entries (watchlist `purpose=prospect` with `crawl`): the lowest
# target priority, tied with crawled interest entries and never above them, and additionally
# capped per prospect per day (`PROSPECT_DAILY_SEED_CAP`, marceline.targets).
PROSPECT_SEED_PRIORITY = WATCHLIST_SEED_PRIORITY
TOP_DETAIL_PRIORITY = 20
DETAIL_PRIORITY = 10
DEEP_DETAIL_PRIORITY = 5
PAGINATION_PRIORITY = 0
TOP_DETAIL_COUNT = 12


# Credit flags an adapter may put into `PageData.extra` (issue #3); `crawler.services.creator_links`
# reads them when it links creators. The page itself is always indexed.
# - `COMPILATION_KEY`: `{"reasons": [...], "performer_count": n}` on a compilation or multi-scene
#   post. Its names are no evidence that a creator performs in *this* page: no creator links.
# - `DROPPED_PERFORMERS_KEY`: `[{"name": ..., "reason": ...}]`, credits the adapter judged to be
#   noise (a title word, a fragment of a longer name). A creator is not linked on such a name alone.
COMPILATION_KEY = "compilation"
DROPPED_PERFORMERS_KEY = "dropped_performers"


def is_compilation(extra: Mapping[str, object]) -> bool:
    return bool(extra.get(COMPILATION_KEY))


def dropped_performer_names(extra: Mapping[str, object]) -> list[str]:
    entries = extra.get(DROPPED_PERFORMERS_KEY)
    if not isinstance(entries, list):
        return []
    return [
        name
        for entry in entries
        if isinstance(entry, dict) and isinstance(name := entry.get("name"), str) and name
    ]


def detail_priority(position: int, *, first_page: bool) -> int:
    """Priority of the detail link at 0-based `position` among a listing page's detail links."""
    if not first_page:
        return DEEP_DETAIL_PRIORITY
    return TOP_DETAIL_PRIORITY if position < TOP_DETAIL_COUNT else DETAIL_PRIORITY


@dataclass(frozen=True)
class MediaSpec:
    """A preview asset URL (poster, frame, sprite, preview clip). URLs may be on CDNs.

    `sprite_columns`/`sprite_rows` are for `Kind.SPRITE` only: an adapter that read the sheet's
    tile grid off the site's own markup (a JSON player config, a WebVTT thumbnail track's
    `#xywh=` cues) states it here rather than leaving the media worker to guess it from the
    fetched image's geometry (issue #14, `matching.sprite_grid`) — "believe the site" is exact
    and free. Both set or both left None; a partial pair is rejected where the spec is used.
    """

    kind: Media.Kind
    url: str
    sprite_columns: int | None = None
    sprite_rows: int | None = None


@dataclass(frozen=True)
class TaskSpec:
    """A URL to crawl. Only URLs on the task's own site are enqueued.

    `media` carries assets seen for this URL on the linking page (e.g. a listing thumbnail); they
    are attached to the Page once the URL itself is parsed.

    `text` is the visible text of the card this URL was linked from (a listing card's title), and
    only adapters that declare `card_text` (see `SiteAdapter`) fill it. It is what the
    breadth-first title gate reads (`crawler.listing_walk`) and nothing else: it is never stored,
    a page always gets its text from its own detail page.
    """

    url: str
    kind: CrawlTask.Kind
    priority: int = 0
    media: tuple[MediaSpec, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class PageData:
    """Metadata of one video page. `url` should be the canonical URL."""

    url: str
    title: str
    tags: tuple[str, ...] = ()
    performers: tuple[str, ...] = ()
    uploaded_at: datetime | None = None
    duration_s: int | None = None
    extra: Mapping[str, object] = field(default_factory=dict)  # JSON-serialisable
    # Who produced these fields (ADR 0042, issue #22). Adapters never set it: an adapter is the
    # default, and `crawler.repair` is the only thing that ever hands back `"llm_repair"`. It
    # becomes `index.Page.extraction_source`, so a later cleanup can find every repaired row.
    extracted_by: str = "adapter"


@dataclass(frozen=True)
class ParseResult:
    removed: bool = False
    page: PageData | None = None
    links: tuple[TaskSpec, ...] = ()
    media: tuple[MediaSpec, ...] = ()


@dataclass(frozen=True)
class CatalogueTotal:
    """How many videos a site says it publishes, read off a page we fetched anyway (issue #107).

    The denominator of our share of a site's catalogue (`catalog.catalogue_size`). Many sites
    print the figure in their own page furniture ("12345 videos"), which costs no request at all —
    that is the only way an adapter may produce one. An adapter never fetches a sitemap, never
    walks pagination to find the last page, and never derives this from anything but the HTML it
    was handed.

    `source_path` is a **path shape** (`/`, `/creator/*`, `/video/*`), never the URL it came from:
    the record is a finding and findings name identifiers, not content.
    """

    total: int
    method: str = CatalogueSizeMethod.STATED_ON_SITE
    source_path: str = ""
    note: str = ""


class SiteAdapter(Protocol):
    domain: str

    # Optional capability, declared per site as a plain class attribute `card_text = True`: every
    # detail link this adapter returns from a listing page carries that card's visible text in
    # `TaskSpec.text`. Only then may a broad listing be walked breadth first with a title gate
    # (`crawler.listing_walk`, ADR 0029 amendment) — a site whose cards show no title cannot be.
    # It is not part of the protocol itself, so no adapter has to mention it: ask `has_card_text`.

    # A second optional capability, declared per site as a method
    # `catalogue_total(self, kind, url, html) -> CatalogueTotal | None`: the number of videos the
    # site says it publishes, if it says so on this page (issue #107, ADR 0049). Like `card_text`
    # it is not part of the protocol, so no adapter has to mention it: ask `catalogue_total`
    # below. It may only ever read the HTML it was handed — no request of its own, of any kind.

    # A third optional capability, declared per site as a plain class attribute
    # `card_media_only = True`: this site's video pages carry no image of their own (the player is
    # a script, the related cards belong to other videos), so every poster and frame of a page
    # travels with the task in `TaskSpec.media` from the card that linked it. Such an adapter
    # returns `ParseResult.media == ()` on a detail page *when it is working perfectly*, so
    # breakage detection must not read that as a break (ADR 0042 amendment 2026-09-19: the false
    # alarm on two of these three sites). Ask `has_card_media_only` below.

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        """Start URLs for these creators (performer/tag and search pages), never broad listings."""
        ...

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """SEARCH start URLs for free text such as a video title; empty if nothing searchable."""
        ...

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """Broad start URLs that are about no creator (newest uploads, the whole catalogue).

        Never part of `seed_tasks`: since targeted indexing (issue #71) the scheduler seeds only
        target creators, and these run only when an operator asks (`crawl_seed --listings`).
        """
        ...

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        """Parse a fetched page. `url` is the final URL after redirects."""
        ...


def catalogue_total(
    adapter: SiteAdapter, kind: CrawlTask.Kind, url: str, html: str
) -> CatalogueTotal | None:
    """The catalogue total this adapter reads off this page, or None if it reads none.

    The optional capability above; absent on every adapter that never declares it, which is the
    default answer for a site that states no total anywhere. Pure, like every adapter method.
    """
    reader = getattr(adapter, "catalogue_total", None)
    if reader is None:
        return None
    found = reader(kind, url, html)
    return found if isinstance(found, CatalogueTotal) else None


def has_card_text(adapter: SiteAdapter) -> bool:
    """Does this adapter put its listing cards' visible text into `TaskSpec.text`?

    The optional capability above; absent on every adapter that never declares it, which is the
    default answer for a site whose cards carry no usable title.
    """
    return getattr(adapter, "card_text", False) is True


def has_card_media_only(adapter: SiteAdapter) -> bool:
    """Do this site's detail pages carry no media of their own, so the card's is all there is?

    The optional capability above; False for every adapter that never declares it, which is the
    normal case — a video page usually shows at least its own poster. Only three adapters declare
    it, and for them an empty `ParseResult.media` on a detail page is the parser working, not a
    break (`crawler.adapter_health`).
    """
    return getattr(adapter, "card_media_only", False) is True
