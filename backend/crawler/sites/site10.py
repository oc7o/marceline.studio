"""site10.example adapter (bespoke PHP CMS, not KVS).

- Seeds: `/search?q={name}` per creator name and alias, on the `www` host (the apex redirects).
  A name query resolves to the performer's own catalog: `/search?q=lena+hartwell` returns the same
  28 cards as `/lena-hartwell/`. The bare-slug performer page is therefore *not* seeded as well —
  the two URLs keep different canonicals, so seeding both would index every video twice, and the
  root namespace is shared with tags (`/outdoor/` is a tag), so a slug guess can silently land on a
  tag page. Search never 404s, so a miss degrades to ordinary search results.
- Listing / search / performer / tag pages: one link per `div.video_div > a.video_img`
  (`/vid-{id}-{slug}/`) plus pagination. Pagination differs by page type and is taken from the
  `div.pagIno` block as the site writes it: a path segment on listings and performer pages
  (`/page=2`, `/lena-hartwell/page=2`) and a query parameter on search (`&page=2`).
- **A 404 body still renders 20 recommendation cards**, so cards are not an availability signal on
  this site. The 404 template is recognised and yields nothing at all; only the HTTP status marks a
  page removed, so `removed` is never set here.
- Video pages: title from `<h1>` without its trailing `<span> - 17 min</span>`, duration from
  `meta[property="video:duration"]` (seconds), performers/tags/categories from the
  `div.catlinedata` blocks labelled `Performers:` / `Tags:` / `Categories:`. Video pages yield no
  links, which keeps the crawl on the seeds.
- **No upload date exists anywhere on the site** (no JSON-LD, no microdata, no absolute date); cards
  carry only relative text and adapters have no clock, so `uploaded_at` stays unset.
- Media is thin: the card thumbnail (JPEG on `k.site10.example`) and the video page's `og:image`
  poster (AVIF on a third-party CDN host). Only the two above-the-fold cards of a listing
  carry a real thumbnail URL; the rest are lazy and hold a base64 GIF placeholder with no
  alternative attribute, so most cards contribute no media.
"""

import re
from collections.abc import Sequence
from urllib.parse import urlencode, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from catalog.models import Creator, Site
from crawler.models import CrawlTask
from crawler.sites.base import (
    PAGINATION_PRIORITY,
    SEED_PRIORITY,
    MediaSpec,
    PageData,
    ParseResult,
    TaskSpec,
    detail_priority,
)
from crawler.sites.common import (
    absolute_url,
    canonical_url,
    clean_text,
    creator_names,
    dedupe,
    meta_content,
    normalize_name,
)
from index.models import Media

_VIDEO_PATH = re.compile(r"^/vid-\d+-[^/]*/?$")
_CARD_ANCHORS = "div.video_div a.video_img[href]"
_PAGINATION_ANCHORS = "div.pagIno a[href]"
_PAGE_PATH = re.compile(r"(?:^|/)page=(\d+)/?$")
_PAGE_QUERY = re.compile(r"(?:^|&)page=(\d+)(?:&|$)")
# The 404 template ("404 Error! Page Not Found!") still renders recommendation cards.
_NOT_FOUND_HEADING = "404 Error! Page Not Found!"
_NOT_FOUND_TITLE = "Page Not Found"
# The site logo, served as og:image on pages that have no poster of their own.
_LOGO_PATH_PREFIX = "/style/"
_CATLINE_LABELS = {"performers": "performers", "tags": "tags", "categories": "categories"}


class Site10Adapter:
    domain = "site10.example"

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks = [task for name in names for task in self.search_tasks(site, name)]
        return list({task.url: task for task in tasks}.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/search?q=` with the normalized text, spaces as `+`, as the site's form writes it."""
        name = normalize_name(query)
        if not name:
            return []
        url = f"{_base(site)}/search?{urlencode({'q': name})}"
        return [TaskSpec(url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """None: every seed of this site is a creator or search page (issue #71)."""
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if _is_not_found(tree):
            return ParseResult()
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        return _parse_listing(kind, url, tree)


def _base(site: Site) -> str:
    """Seeds use the `www` host: the apex redirects there and `www` is the canonical host."""
    return f"https://www.{site.domain.lower().removeprefix('www.')}"


def _is_not_found(tree: LexborHTMLParser) -> bool:
    """True for the site's 404 template, which renders 20 unrelated recommendation cards."""
    heading = tree.css_first("h1")
    if heading is not None and _NOT_FOUND_HEADING in clean_text(heading.text()):
        return True
    title = tree.css_first("title")
    return title is not None and clean_text(title.text()).startswith(_NOT_FOUND_TITLE)


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    first_page = _page_number(url) < 2
    media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
    for anchor in tree.css(_CARD_ANCHORS):
        href = absolute_url(url, anchor.attributes.get("href"))
        if href is None or not _VIDEO_PATH.match(urlsplit(href).path):
            continue
        if href not in media_by_url:
            media_by_url[href] = _card_media(url, anchor)
    # No cards is the stop condition for pagination: nothing is known about past-the-end pages.
    if not media_by_url:
        return ParseResult()
    links = [
        TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=first_page), media)
        for i, (href, media) in enumerate(media_by_url.items())
    ]
    pages = dedupe(
        absolute_url(url, anchor.attributes.get("href")) or ""
        for anchor in tree.css(_PAGINATION_ANCHORS)
    )
    links.extend(TaskSpec(page, kind, PAGINATION_PRIORITY) for page in pages if page != url)
    return ParseResult(links=tuple(links))


def _card_media(base: str, anchor: LexborNode) -> tuple[MediaSpec, ...]:
    """The card thumbnail. Lazy cards hold a base64 GIF placeholder and carry no real URL."""
    image = anchor.css_first("img")
    if image is None:
        return ()
    # `data-src` carries the URL on lazily loaded blocks elsewhere on the page (live-stream cards); video
    # cards use `src` only, so read the lazy attribute first and fall back to `src`.
    candidates = (absolute_url(base, image.attributes.get(key)) for key in ("data-src", "src"))
    src = next((candidate for candidate in candidates if candidate), None)
    return (MediaSpec(Media.Kind.POSTER, src),) if src else ()


def _page_number(url: str) -> int:
    """Page number from the `/page=2` path segment or `&page=2` search parameter; 1 if absent."""
    parts = urlsplit(url)
    path_match = _PAGE_PATH.search(parts.path)
    if path_match:
        return int(path_match.group(1))
    query_match = _PAGE_QUERY.search(parts.query)
    return int(query_match.group(1)) if query_match else 1


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    heading = tree.css_first("h1")
    if heading is None:
        return ParseResult()
    # The duration sits in a nested `<span> - 17 min</span>`; the shallow text is the clean title.
    title = clean_text(heading.text(deep=False))
    if not title:
        return ParseResult()

    labelled = _catline_links(tree)
    tags = labelled.get("tags", [])
    categories = labelled.get("categories", [])
    duration = meta_content(tree, "video:duration")
    page = PageData(
        url=canonical_url(tree, url) or absolute_url(url, meta_content(tree, "og:url")) or url,
        title=title,
        tags=tuple(dedupe([*tags, *categories])),
        performers=tuple(labelled.get("performers", [])),
        # The site publishes no upload date at all, and adapters have no clock.
        uploaded_at=None,
        duration_s=int(duration) if duration.isdigit() else None,
        extra={"tags": tags, "categories": categories},
    )
    return ParseResult(page=page, media=_video_media(url, tree))


def _catline_links(tree: LexborHTMLParser) -> dict[str, list[str]]:
    """Link texts of the `div.catlinedata` blocks, keyed by their leading `<b>` label."""
    found: dict[str, list[str]] = {}
    for block in tree.css("div.catlinedata"):
        label = block.css_first("b")
        if label is None:
            continue
        key = _CATLINE_LABELS.get(clean_text(label.text()).rstrip(":").lower())
        if key is None:
            continue
        names = dedupe(clean_text(anchor.text()) for anchor in block.css("a[href]"))
        found.setdefault(key, []).extend(name for name in names if name not in found.get(key, []))
    return found


def _video_media(url: str, tree: LexborHTMLParser) -> tuple[MediaSpec, ...]:
    """The `og:image` poster (AVIF on a shared third-party CDN); the site logo is not a poster."""
    poster = absolute_url(url, meta_content(tree, "og:image"))
    if poster is None or urlsplit(poster).path.startswith(_LOGO_PATH_PREFIX):
        return ()
    return (MediaSpec(Media.Kind.POSTER, poster),)
