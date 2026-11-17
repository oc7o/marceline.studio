"""site14.example adapter (bespoke; a German skin of a network of sites).

- Seeds are per creator, two per name: the performer page `/performer/{slug}/` — the slug is the
  plain name slug, verified against the site's own A-Z performer index — and the site's GET
  search `/suchen/?q={words}`, which really filters (two different queries return different,
  matching card lists and their own result counts).
- Listing, performer and search pages share one theme: `div.box-escena` cards linking
  `/filme/{slug}/`, a lazily loaded `<img data-src>` thumbnail (the `src` is a base64
  placeholder), the card title in `h4 a` and a `div.duracion` "mm:ss min" label. Pagination is a
  `nav.pagination-container` of plain `…/page{N}.html` links; only links naming a later page than
  the current one are followed, and a page without cards yields nothing, which stops the walk.
- Video pages carry no JSON-LD; their metadata is schema.org microdata in `<meta itemprop>` tags
  (`name`, `duration` as `T11M32S` — no leading `P` —, `uploadDate` as `2019-01-10UTC10:35:12
  +00:00`, `thumbnailUrl`) beside the usual og tags. Categories (`/filme/{slug}/`) and tags
  (`/tag/{slug}/`) live in `#data-title`, performer credits (`/performer/{slug}/`) in
  `#data-video`; both blocks are scoped, so the site's navigation never becomes a credit.
- Media: the video page's own poster (`…/{hash}.mp4-preview-3.jpg`) and, from listing cards, one
  numbered timeline frame per video (`…/{hash}.mp4-{n}.jpg`, a different `n` per video). Both are
  on the network's `pics.network.example` image hosts. Nothing on the site declares how many
  numbered frames exist, so none are guessed (as site12.example and site13.example).
- Missing videos answer HTTP 404 (the worker marks them removed); the 404 body is the full site
  layout with suggestion cards, but it has no video metadata, so a parse of it yields no page.
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
    parse_datetime,
    parse_iso_duration,
    slugify,
)
from index.models import Media

DOMAIN = "site14.example"
# Seeds and pages live on the www host; the apex redirects there (robots.txt included).
HOST = "www.site14.example"
_VIDEO_PATH = re.compile(r"^/filme/[^/]+/?$")
_CARD_SELECTOR = "div.box-escena"
_PAGINATION_ANCHORS = "nav.pagination-container a.btn-pagination[href]"
_PAGE_NUMBER = re.compile(r"/page(\d+)\.html$")
_IMAGE_ATTRIBUTES = ("data-src", "data-original", "src")


class Site14Adapter:
    domain = DOMAIN
    # Every card shows its video's title in `h4 a`, so a broad listing may be walked breadth
    # first with a title gate (`crawler.listing_walk`, `crawl_seed --listings`).
    card_text = True

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks: list[TaskSpec] = []
        for name in names:
            if slug := slugify(name):
                url = f"https://{HOST}/performer/{slug}/"
                tasks.append(TaskSpec(url, CrawlTask.Kind.LISTING, SEED_PRIORITY))
            tasks.extend(self.search_tasks(site, name))
        return list({task.url: task for task in tasks}.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/suchen/?q=` with the normalized words; empty for text with no ASCII words."""
        words = normalize_name(query)
        if not words:
            return []
        url = f"https://{HOST}/suchen/?{urlencode({'q': words})}"
        return [TaskSpec(url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """The newest-uploads front page, only on request (issue #71)."""
        return [TaskSpec(f"https://{HOST}/", CrawlTask.Kind.LISTING, SEED_PRIORITY)]

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        return _parse_listing(kind, url, tree)


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    """A front page, a `/performer/{slug}/` catalogue or a `/suchen/?q=` result page."""
    page_number = _page_number(url)
    cards: dict[str, tuple[tuple[MediaSpec, ...], str]] = {}
    for card in tree.css(_CARD_SELECTOR):
        anchor = card.css_first('a[href*="/filme/"]')
        href = absolute_url(url, anchor.attributes.get("href")) if anchor else None
        if href is None or not _VIDEO_PATH.match(urlsplit(href).path):
            continue
        cards.setdefault(href, (_card_media(url, card), _card_title(card)))
    # No cards is the stop condition: nothing is known about past-the-end pages.
    if not cards:
        return ParseResult()
    links = [
        TaskSpec(
            href,
            CrawlTask.Kind.DETAIL,
            detail_priority(position, first_page=page_number < 2),
            media,
            text=title,
        )
        for position, (href, (media, title)) in enumerate(cards.items())
    ]
    links.extend(_pagination_links(kind, url, tree, page_number))
    return ParseResult(links=tuple(links))


def _card_media(base: str, card: LexborNode) -> tuple[MediaSpec, ...]:
    """The card's one numbered timeline frame; its `src` is a base64 placeholder, never media."""
    image = card.css_first("img")
    src = _image_src(base, image) if image is not None else None
    return (MediaSpec(Media.Kind.FRAME, src),) if src else ()


def _image_src(base: str, image: LexborNode) -> str | None:
    candidates = (absolute_url(base, image.attributes.get(key)) for key in _IMAGE_ATTRIBUTES)
    return next((candidate for candidate in candidates if candidate), None)


def _card_title(card: LexborNode) -> str:
    """The card's visible title (the `card_text` capability); the thumbnail's `alt` repeats it."""
    anchor = card.css_first("h4 a")
    if anchor is not None and (text := clean_text(anchor.text())):
        return text
    image = card.css_first("img")
    return clean_text(image.attributes.get("alt")) if image is not None else ""


def _page_number(url: str) -> int:
    match = _PAGE_NUMBER.search(urlsplit(url).path)
    return int(match.group(1)) if match else 1


def _pagination_links(
    kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser, page_number: int
) -> list[TaskSpec]:
    """Every paginator link naming a later page than this one (the list is truncated, so deeper
    pages are reached gradually as each page is crawled in turn)."""
    pages = (
        absolute_url(url, anchor.attributes.get("href")) for anchor in tree.css(_PAGINATION_ANCHORS)
    )
    ahead = dedupe(page for page in pages if page and _page_number(page) > page_number)
    return [TaskSpec(page, kind, PAGINATION_PRIORITY) for page in ahead]


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    title = meta_content(tree, "og:title") or _itemprop(tree, "name")
    if not title:
        return ParseResult()
    tags = _credits(tree, "#data-title", "/tag/")
    categories = _credits(tree, "#data-title", "/filme/")
    page = PageData(
        url=canonical_url(tree, url) or absolute_url(url, meta_content(tree, "og:url")) or url,
        title=title,
        tags=tuple(dedupe([*tags, *categories])),
        performers=tuple(_credits(tree, "#data-video", "/performer/")),
        uploaded_at=parse_datetime(_upload_date(tree)),
        duration_s=_duration_s(tree),
        extra={"tags": tags, "categories": categories},
    )
    poster = absolute_url(url, meta_content(tree, "og:image") or _itemprop(tree, "thumbnailUrl"))
    media = (MediaSpec(Media.Kind.POSTER, poster),) if poster else ()
    return ParseResult(page=page, media=media)


def _itemprop(tree: LexborHTMLParser, key: str) -> str:
    node = tree.css_first(f'meta[itemprop="{key}"]')
    return clean_text(node.attributes.get("content")) if node is not None else ""


def _credits(tree: LexborHTMLParser, container: str, prefix: str) -> list[str]:
    """Link texts under one scoped info block; the site navigation is never one of them."""
    block = tree.css_first(container)
    if block is None:
        return []
    return dedupe(clean_text(anchor.text()) for anchor in block.css(f'a[href*="{prefix}"]'))


def _duration_s(tree: LexborHTMLParser) -> int | None:
    """`T11M32S`: an ISO 8601 duration without its leading `P`, which the theme drops."""
    value = _itemprop(tree, "duration")
    if value.upper().startswith("T"):
        value = f"P{value}"
    return parse_iso_duration(value)


def _upload_date(tree: LexborHTMLParser) -> str:
    """`2019-01-10UTC10:35:12+00:00`: ISO with "UTC" where the date/time separator belongs."""
    return _itemprop(tree, "uploadDate").replace("UTC", "T", 1)
