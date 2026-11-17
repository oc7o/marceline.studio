"""site04.example adapter (mirror of site02).

- Seeds: `/performer/{slug}` and `/search?q={name}` per creator name and alias (creators without a
  performer page get HTTP 404 there, which the worker handles). Free-text search seeds
  (`search_tasks`, e.g. video titles) use the same `/search?q={words}` search.
- Listing/performer/search pages: `div.video-card` links to `/watch/{24-hex}` with the card image as
  poster (search cards carry the large site02-style `_main.jpg`, other cards a small `/thumb/`),
  and `ul.pagination` links (`/home/N`, `/performer/{slug}/N`, `/search?q=…&p=N`). Pages past the
  end return 200 with no cards but still link to the next page, so pagination is only followed
  from pages that have cards.
- Video pages: title, performers (lower-case, as the site shows them), categories, upload date
  (unix timestamp) and duration. They carry no image of their own (the player is a JS iframe);
  media come from the linking card. The "More" cards on video pages are ignored.
- Missing videos return HTTP 404 ("404 Not Found" page); there is no removal marker on 200 pages.
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime
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
    normalize_name,
    slugify,
)
from index.models import Media

_VIDEO_PATH = re.compile(r"^/watch/[0-9a-f]{24}$")
_PAGE_SUFFIX = re.compile(r"^(?P<base>/.+)/(?P<page>\d+)$")
_CLOCK = re.compile(r"^(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2})$")
_TITLE_SUFFIX = " - site04.example | site04.example"


class Site04Adapter:
    domain = "site04.example"
    #: This site's video pages carry no image of their own: every poster and frame comes from
    #: the card that linked the page (`TaskSpec.media`). Declaring it keeps breakage detection
    #: from reading an empty detail-page media tuple as a break (ADR 0042, `has_card_media_only`).
    card_media_only = True

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        base = f"https://{site.domain}"
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks: list[TaskSpec] = []
        for name in names:
            tasks.append(
                TaskSpec(f"{base}/performer/{slugify(name)}", CrawlTask.Kind.LISTING, SEED_PRIORITY)
            )
            tasks.extend(self.search_tasks(site, name))
        return tasks

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/search?q=` with the normalized text (lower-case ASCII words)."""
        name = normalize_name(query)
        if not name:
            return []
        url = f"https://{site.domain}/search?{urlencode({'q': name})}"
        return [TaskSpec(url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """None: every seed of this site is a creator or search page (issue #71)."""
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        return _parse_listing(kind, url, tree)


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    current = _page_url(url)
    first_page = _page_number(current) < 2
    links: list[TaskSpec] = []
    seen: set[str] = set()
    for card in tree.css("div.video-card"):
        anchor = card.css_first('a[href^="/watch/"]')
        href = absolute_url(url, anchor.attributes.get("href")) if anchor else None
        if href is None or not _VIDEO_PATH.match(urlsplit(href).path) or href in seen:
            continue
        seen.add(href)
        priority = detail_priority(len(links), first_page=first_page)
        links.append(TaskSpec(href, CrawlTask.Kind.DETAIL, priority, _card_media(url, card)))
    if not links:
        # Pages past the end still link to the "next" page; stop here.
        return ParseResult()
    pages = (absolute_url(url, a.attributes.get("href")) for a in tree.css("ul.pagination a[href]"))
    for href in dedupe(_page_url(page) for page in pages if page):
        if href != current:
            links.append(TaskSpec(href, kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _page_url(url: str) -> str:
    """Canonical form of a listing page URL, so pagination links match seed URLs.

    Search hrefs contain raw spaces (`/search?q=lena hartwell&p=1`); they are re-encoded, and page 1
    drops its page number (`/search?q=lena+hartwell`, `/performer/{slug}`, `/` for `/home/1`).
    """
    parts = urlsplit(url)
    if parts.path == "/search":
        params = parse_qs(parts.query)
        query = {"q": clean_text(params.get("q", [""])[0])}
        page = params.get("p", [""])[0]
        if page.isdigit() and int(page) > 1:
            query["p"] = page
        return parts._replace(query=urlencode(query), fragment="").geturl()
    match = _PAGE_SUFFIX.match(parts.path)
    if match and int(match.group("page")) == 1:
        path = "/" if match.group("base") == "/home" else match.group("base")
        return parts._replace(path=path, fragment="").geturl()
    return url


def _page_number(url: str) -> int:
    """Page number from `?p=2` or paths like `/home/2`, `/performer/{slug}/2`; 1 if absent."""
    parts = urlsplit(url)
    query_page = parse_qs(parts.query).get("p", [""])[0]
    if query_page.isdigit():
        return int(query_page)
    match = _PAGE_SUFFIX.match(parts.path)
    return int(match.group("page")) if match else 1


def _card_media(base: str, card: LexborNode) -> tuple[MediaSpec, ...]:
    """The card image as poster, preferring the large `_main.jpg` over a small `/thumb/`."""
    candidates = [node.attributes.get("data-src") for node in card.css("img[data-src]")]
    candidates += [node.attributes.get("href") for node in card.css('link[as="image"]')]
    urls = dedupe(url for url in (absolute_url(base, c) for c in candidates) if url)
    if not urls:
        return ()
    poster = next((url for url in urls if url.endswith("_main.jpg")), urls[0])
    return (MediaSpec(Media.Kind.POSTER, poster),)


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    box = tree.css_first(".single-video-title")
    heading = box.css_first("h2") if box else None
    if box is None or heading is None:
        return ParseResult()
    title = clean_text(heading.text())
    if not title:
        title_node = tree.css_first("title")
        title = clean_text(title_node.text() if title_node else "").removesuffix(_TITLE_SUFFIX)
    if not title:
        return ParseResult()

    categories = dedupe(
        clean_text(a.text()).removeprefix("#") for a in box.css('.tag-link a[href^="/category/"]')
    )
    performers = dedupe(clean_text(a.text()) for a in tree.css('a.fullname[href^="/performer/"]'))
    info = box.css_first(".video-info")
    created = info.css_first("span.create") if info else None
    # Duration is the `.video-info` child div that does not hold the date.
    info_texts = [
        clean_text(child.text())
        for child in (info.iter() if info else [])
        if child.tag == "div" and child.css_first("span.create") is None
    ]
    duration = next((s for s in map(_parse_clock, info_texts) if s is not None), None)

    page = PageData(
        url=canonical_url(tree, url) or url,
        title=title,
        tags=tuple(categories),
        performers=tuple(performers),
        uploaded_at=_parse_timestamp(clean_text(created.text()) if created else ""),
        duration_s=duration,
        extra={"categories": categories},
    )
    return ParseResult(page=page)


def _parse_timestamp(value: str) -> datetime | None:
    """Unix timestamp text such as "1789393613" -> aware UTC datetime."""
    if not value.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_clock(value: str) -> int | None:
    """Duration text such as "01:08:53" or "50:00" -> seconds."""
    match = _CLOCK.match(value)
    if not match:
        return None
    parts = match.groupdict()
    return int(parts["h"] or 0) * 3600 + int(parts["m"]) * 60 + int(parts["s"])
