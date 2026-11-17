"""site13.example adapter (bespoke; one skin of a network of German sites).

- The site's own `<form method="post" action="/search/videos">` (`name=search_query`) is
  POST-only: a GET with the same query string answers 200 but ignores the parameter and returns
  the same default listing regardless of the text (confirmed by comparing two different queries
  byte-for-byte on the card list) — as unusable as site11.example's POST-only search.
- The real, usable search is a second, GET form on `/member` (`name=q`), a substring match over
  performer handles/display names: `/member?q={words}` returns `div.member-card` cards, one per
  matching performer, each linking their own `/member/{handle}` catalogue. Handles use three
  different, unpredictable separator styles (`lena-h`, `kira_r`, `ninaashton`), so a handle
  is never guessed from a creator's display name — `seed_tasks` always goes through this search,
  the same role `/search` plays for site09.example.
- Because of that, our target performers have no uploader profile here and the search answers
  nothing for them (zero pages, 2026-09-18). What names them is the *title* of
  somebody else's upload, and every catalogue card carries it (`span.content-title`,
  `Handle - Title`, 50 per request over a few thousand pages), so this adapter declares the `card_text`
  capability and the broad catalogue is walked breadth first with a title gate
  (`crawler.listing_walk`, `crawl_seed --listings`, ADR 0029 amendment).
- `/member/{handle}` (a creator's whole catalogue) and `/videos` (the broad newest catalogue,
  `listing_tasks` only) share one theme: `div.avs8-thumb-box` cards and a `div.pagination-center`
  paginator of plain `<a class="page-link" href="…?page=N">` links, already absolute and already
  truncated (first few, `…`, last two) — `_pagination_links` follows every link naming a later
  page than the current one, same truncation-tolerant approach as site09.example.
- Video pages carry a JSON-LD `VideoObject` (`name`, `uploadDate`, `duration` as `PT..S`) and,
  separately, `div.video-actions a.vact-btn` pills: `/videos/{tag}` hrefs are category tags,
  `/member/{handle}` hrefs are performer credits (its `fa-user` icon is cosmetic; the href prefix
  is what is actually keyed on). A pill's visible label lives in one of two responsive spans
  (`span.d-none.d-md-inline` desktop, `span.vact-label` mobile) with identical text.
- Media: the theme shards a video's frame across `tmb`, `tmb1`, `tmb2`, `tmb3`, … subfolders by
  id, consistently reused by every reference to that video's frame *except* the JSON-LD
  `thumbnailUrl`, which always says `tmb` regardless of the real shard (verified against the same
  video's own card thumbnail, `og:image`, and unrelated videos' cards, all of which agree with
  each other and disagree with their own JSON-LD). `og:image` is used; `thumbnailUrl` is only a
  fallback if `og:image` is ever missing. No frame count is declared anywhere, so only the one
  frame is stored (as site12.example and site09.example, no rotator, no guessed numbering).
- Missing videos answer HTTP 404 (the worker marks them removed); a 404 body has no JSON-LD
  `VideoObject`, so it never yields a page here either.
"""

import re
from collections.abc import Sequence
from urllib.parse import parse_qs, urlencode, urlsplit

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
    first_json_ld,
    meta_content,
    normalize_name,
    parse_datetime,
    parse_iso_duration,
    str_field,
)
from index.models import Media

DOMAIN = "site13.example"
# A cap only on the discovery search (a short common substring could otherwise match hundreds of
# performers); the creator/broad catalogues below have no cap, their own last page stops them.
MAX_SEARCH_PAGE = 10
_VIDEO_PATH = re.compile(r"^/video/\d+/[^/]+/?$")
_MEMBER_PATH = re.compile(r"^/member/[^/]+/?$")
_CARD_SELECTOR = "div.avs8-thumb-box"
_MEMBER_CARD_SELECTOR = "div.member-card"


class Site13Adapter:
    domain = DOMAIN
    # Every catalogue card shows its video's title (`Handle - Title`), so the broad `/videos`
    # catalogue can be walked breadth first with a title gate (`crawler.listing_walk`): this is
    # the site where the searchable index (uploader handles) and what we hunt (performers named
    # in somebody else's title) are two different things.
    card_text = True

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks = [task for name in names for task in self.search_tasks(site, name)]
        return list({task.url: task for task in tasks}.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/member?q=` with the normalized words; the site's only usable search (see module
        docstring). Empty for text with no ASCII words."""
        words = normalize_name(query)
        if not words:
            return []
        url = f"https://{site.domain}/member?{urlencode({'q': words})}"
        return [TaskSpec(url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """The newest-uploads catalogue, only on request (issue #71)."""
        return [TaskSpec(f"https://{site.domain}/videos", CrawlTask.Kind.LISTING, SEED_PRIORITY)]

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        if kind == CrawlTask.Kind.SEARCH:
            return _parse_search(url, tree)
        return _parse_listing(kind, url, tree)


def _parse_search(url: str, tree: LexborHTMLParser) -> ParseResult:
    """`/member?q=` results: one `/member/{handle}` catalogue seed per matching performer card.

    The always-present A-Z browse nav (`div.member-az-nav a.member-az-btn`) is a different
    element entirely and is never selected here.
    """
    profiles: list[str] = []
    for card in tree.css(_MEMBER_CARD_SELECTOR):
        href = _card_href(url, card, "/member/")
        if href is not None and _MEMBER_PATH.match(urlsplit(href).path):
            profiles.append(href)
    links = [TaskSpec(href, CrawlTask.Kind.LISTING, SEED_PRIORITY) for href in dedupe(profiles)]
    links.extend(
        _pagination_links(CrawlTask.Kind.SEARCH, url, tree, _page_number(url), MAX_SEARCH_PAGE)
    )
    return ParseResult(links=tuple(links))


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    """A creator's `/member/{handle}` catalogue or the broad `/videos` catalogue: identical
    card and paginator markup."""
    page_number = _page_number(url)
    cards: dict[str, tuple[tuple[MediaSpec, ...], str]] = {}
    for card in tree.css(_CARD_SELECTOR):
        href = _card_href(url, card, "/video/")
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
            detail_priority(i, first_page=page_number < 2),
            media,
            text=title,
        )
        for i, (href, (media, title)) in enumerate(cards.items())
    ]
    links.extend(_pagination_links(kind, url, tree, page_number, None))
    return ParseResult(links=tuple(links))


def _card_href(base: str, card: LexborNode, prefix: str) -> str | None:
    anchor = card.css_first(f'a[href^="{prefix}"]')
    return absolute_url(base, anchor.attributes.get("href")) if anchor else None


def _card_title(card: LexborNode) -> str:
    """The card's visible title, `Handle - Title` on this theme (the `card_text` capability).

    `span.content-title` is the title as the visitor reads it; the thumbnail's `title`/`alt`
    carries the same string and stands in if the theme ever drops the span.
    """
    span = card.css_first("span.content-title")
    if span is not None and (text := clean_text(span.text())):
        return text
    image = card.css_first("img")
    if image is None:
        return ""
    attributes = image.attributes
    return clean_text(attributes.get("title") or attributes.get("alt"))


def _card_media(base: str, card: LexborNode) -> tuple[MediaSpec, ...]:
    """The card thumbnail: a real `src`, never a lazy placeholder on this theme."""
    image = card.css_first("img[src]")
    src = absolute_url(base, image.attributes.get("src")) if image else None
    return (MediaSpec(Media.Kind.POSTER, src),) if src else ()


def _page_number(url: str) -> int:
    value = parse_qs(urlsplit(url).query).get("page", [""])[0]
    return int(value) if value.isdigit() and int(value) > 0 else 1


def _pagination_links(
    kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser, page_number: int, max_page: int | None
) -> list[TaskSpec]:
    """Every paginator link naming a later page than this one (the list is truncated - first few,
    "...", last two - so later pages are reached gradually as each is crawled in turn, as on
    site09.example)."""
    pages = (
        absolute_url(url, a.attributes.get("href"))
        for a in tree.css("div.pagination-center a.page-link[href]")
    )
    ahead = dedupe(page for page in pages if page and _page_number(page) > page_number)
    if max_page is not None:
        ahead = [page for page in ahead if _page_number(page) <= max_page]
    return [TaskSpec(page, kind, PAGINATION_PRIORITY) for page in ahead]


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    video = first_json_ld(tree, "VideoObject")
    title = str_field(video, "name")
    if not title:
        return ParseResult()
    actions = tree.css_first("div.video-actions")
    duration = str_field(video, "duration")
    page = PageData(
        url=canonical_url(tree, url) or absolute_url(url, meta_content(tree, "og:url")) or url,
        title=title,
        tags=tuple(_pill_texts(actions, "/videos/")),
        performers=tuple(_pill_texts(actions, "/member/")),
        uploaded_at=parse_datetime(str_field(video, "uploadDate")),
        duration_s=parse_iso_duration(duration) if duration else None,
    )
    return ParseResult(page=page, media=_video_media(url, tree, video))


def _pill_texts(container: LexborNode | None, prefix: str) -> list[str]:
    if container is None:
        return []
    texts: list[str] = []
    for anchor in container.css(f'a.vact-btn[href^="{prefix}"]'):
        span = anchor.css_first("span.d-none.d-md-inline") or anchor.css_first("span.vact-label")
        text = clean_text(span.text()) if span else clean_text(anchor.text())
        if text:
            texts.append(text)
    return dedupe(texts)


def _video_media(
    url: str, tree: LexborHTMLParser, video: dict[str, object]
) -> tuple[MediaSpec, ...]:
    """`og:image` (matches the video's real shard, see module docstring); `thumbnailUrl` (always
    the buggy `tmb` shard) only if `og:image` is missing."""
    poster = absolute_url(url, meta_content(tree, "og:image")) or absolute_url(
        url, str_field(video, "thumbnailUrl")
    )
    return (MediaSpec(Media.Kind.POSTER, poster),) if poster else ()
