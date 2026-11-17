"""site07.example adapter (custom PHP site, Materialize CSS).

- **No search.** robots.txt disallows `/search?`, so `search_tasks` is always empty and free-text
  seeds never reach this site.
- Seeds: the known performer pages (`/{slug}-p{id}`) of the tracked creators. Performer ids are
  not derivable from a name — they only appear in the sitemap — so `PERFORMER_PAGES` holds the
  ones the reconnaissance found. Every other creator is only reachable through the date-sorted
  home listing, which paginates over the whole catalogue (over a thousand pages); since
  targeted indexing (issue #71) that is `listing_tasks`, enqueued only on request.
- Listing and performer pages: cards `#list_grid a.video-link` link `/{slug}-v{id}` and carry the
  video's only poster (`div.video-image img`, webp or older jpg) — the video page itself has no
  image of its own, so the poster has to travel with the task. Pagination is plain
  `ul.pagination` links (`?page=N`); only pages after the current one are followed.
- Video pages: title from the `h2` heading ("{Performers} - {Title}"), categories (`-c{id}`) and
  tags (`-t{id}`) from the chip links, and the external player/download links (one file hoster) in
  `extra["external_links"]` for later discovery. There is no JSON-LD, no og tag, no upload date and
  no duration. The related cards belong to other videos, so video pages yield no links and no media.
- Missing videos: not recorded during reconnaissance, so there is no removal marker to look for and
  `removed` is never set; the worker handles the HTTP status.
"""

import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlsplit

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
    normalize_name,
)
from index.models import Media

# Performer pages found in the sitemap; the id cannot be guessed.
PERFORMER_PAGES: dict[str, str] = {
    "lena hartwell": "/lena-hartwell-p14",
    "nina ashton": "/nina-ashton-p15",
    "elena marsh": "/elena-marsh-p5",
}
MAX_PERFORMER_NAME_WORDS = 4  # "Kira Rowan", "Dana Forde" — longer parts are not names

_VIDEO_PATH = re.compile(r"^/[^/]+-v\d+$")
_CATEGORY_PATH = re.compile(r"^/[^/]+-c\d+$")
_TAG_PATH = re.compile(r"^/[^/]+-t\d+$")
_TITLE_SEPARATOR = " - "


class Site07Adapter:
    domain = "site07.example"
    #: This site's video pages carry no image of their own: every poster and frame comes from
    #: the card that linked the page (`TaskSpec.media`). Declaring it keeps breakage detection
    #: from reading an empty detail-page media tuple as a break (ADR 0042, `has_card_media_only`).
    card_media_only = True

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        base = f"https://{site.domain}"
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        paths = dedupe(PERFORMER_PAGES.get(name, "") for name in names)
        return [TaskSpec(f"{base}{path}", CrawlTask.Kind.LISTING, SEED_PRIORITY) for path in paths]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """The catalogue listing: the only way to creators without a performer page, and everyone
        else's uploads with them — so only on request since issue #71."""
        return [TaskSpec(f"https://{site.domain}/", CrawlTask.Kind.LISTING, SEED_PRIORITY)]

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """Always empty: robots.txt disallows `/search?` and it is not worked around."""
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        return _parse_listing(kind, url, tree)


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    page_number = _page_number(url)
    media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
    for anchor in tree.css("#list_grid a.video-link"):
        href = absolute_url(url, anchor.attributes.get("href"))
        if href is None or not _VIDEO_PATH.match(urlsplit(href).path) or href in media_by_url:
            continue
        media_by_url[href] = _card_media(url, anchor)
    if not media_by_url:
        return ParseResult()
    links = [
        TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=page_number < 2), media)
        for i, (href, media) in enumerate(media_by_url.items())
    ]
    pages = (absolute_url(url, a.attributes.get("href")) for a in tree.css("ul.pagination a[href]"))
    for page in dedupe(p for p in pages if p and _page_number(p) > page_number):
        links.append(TaskSpec(page, kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _page_number(url: str) -> int:
    """Page number from `?page=N`; 1 if absent."""
    page = dict(parse_qsl(urlsplit(url).query)).get("page", "")
    return int(page) if page.isdigit() else 1


def _card_media(base: str, anchor: LexborNode) -> tuple[MediaSpec, ...]:
    """The card image: the video's only poster — video pages carry no image of their own."""
    image = anchor.css_first("div.video-image img[src]")
    poster = absolute_url(base, image.attributes.get("src")) if image else None
    return (MediaSpec(Media.Kind.POSTER, poster),) if poster else ()


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    heading = tree.css_first("h2")
    title = clean_text(heading.text()) if heading else ""
    if not title:
        return ParseResult()

    categories = _chip_texts(url, tree, _CATEGORY_PATH)
    tags = _chip_texts(url, tree, _TAG_PATH)
    page = PageData(
        url=canonical_url(tree, url) or url,
        title=title,
        tags=tuple(dedupe([*categories, *tags])),
        performers=tuple(_performers(title)),
        extra={
            "categories": categories,
            "tags": tags,
            "external_links": _external_links(url, tree),
        },
    )
    # No poster on the video page; it comes from the listing card with the task.
    return ParseResult(page=page)


def _performers(title: str) -> list[str]:
    """Names from the "{Performers} - {Title}" heading; empty when it has no such prefix.

    Titles without the separator ("Nina Ashton Vs Lena Hartwell") keep no performers; creator linking
    falls back to the title text, which carries the names either way.
    """
    head, separator, _ = title.partition(_TITLE_SEPARATOR)
    if not separator:
        return []
    names = (clean_text(part) for part in head.split(","))
    return dedupe(name for name in names if 0 < len(name.split()) <= MAX_PERFORMER_NAME_WORDS)


def _chip_texts(base: str, tree: LexborHTMLParser, path: re.Pattern[str]) -> list[str]:
    texts: list[str] = []
    for anchor in tree.css("a[href]"):
        href = absolute_url(base, anchor.attributes.get("href"))
        if href and path.match(urlsplit(href).path):
            texts.append(clean_text(anchor.text()))
    return dedupe(texts)


def _external_links(url: str, tree: LexborHTMLParser) -> list[str]:
    """The external player iframe and the download links on the same hoster."""
    host = urlsplit(url).netloc
    hosters: set[str] = set()
    links: list[str] = []
    for frame in tree.css("div.video-container iframe[src]"):
        link = absolute_url(url, frame.attributes.get("src"))
        if link and urlsplit(link).netloc != host:
            hosters.add(urlsplit(link).netloc)
            links.append(link)
    for anchor in tree.css("a[href]"):
        link = absolute_url(url, anchor.attributes.get("href"))
        if link and urlsplit(link).netloc in hosters:
            links.append(link)
    return dedupe(links)
