"""site02.example adapter (custom PHP site).

- Seeds: `/artist/{slug}` and `/?q={name}` search per creator name and alias (creators without
  an artist page 404 there; search matches the same artist tags; duplicates are deduped by URL).
  Free-text search seeds (`search_tasks`, e.g. video titles) use the same `/?q={words}` search.
- Listing/artist/search pages: video cards (poster + 10 hover frames) and pagination. Cards after
  the main list belong to a random "… HD videos" block and are ignored.
- Video pages: title, performers, categories, duration. Video pages carry no images of their own
  (media come from the linking card) and yield no further links in v0.
- The upload date is relative ("today", "2 weeks ago"); adapters have no clock, so it is kept as
  text in `extra["date_text"]` and `uploaded_at` stays empty.
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
    CatalogueTotal,
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

_VIDEO_PATH = re.compile(r"^/hdvideo/(\d+)-[^/]+\.html$")
_PAGE_NUMBER_PATH = re.compile(r"/(\d+)$")
_FRAME_IMAGE = re.compile(r"""changeImage\(["']([^"']+_\d+\.jpg)["']""")
_DURATION = re.compile(r"^(?:(?P<h>\d+)h)?\s*(?:(?P<m>\d+)m)?\s*(?:(?P<s>\d+)s)?$")
_TITLE_SUFFIX = " - site02.example"
_CATALOGUE_TOTAL = re.compile(r"^([\d]{3,9})\s+videos$", re.IGNORECASE)


class Site02Adapter:
    domain = "site02.example"
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
                TaskSpec(f"{base}/artist/{slugify(name)}", CrawlTask.Kind.LISTING, SEED_PRIORITY)
            )
            tasks.extend(self.search_tasks(site, name))
        return tasks

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/?q=` search with the normalized text (lower-case ASCII words), as the site's form."""
        name = normalize_name(query)
        if not name:
            return []
        url = f"https://{site.domain}/?{urlencode({'q': name})}"
        return [TaskSpec(url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """None: every seed of this site is a creator or search page (issue #71)."""
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        return _parse_listing(kind, url, tree)

    def catalogue_total(self, kind: CrawlTask.Kind, url: str, html: str) -> CatalogueTotal | None:
        """ "N videos" out of the site's own header meta, on every page kind (issue #107).

        The site prints its catalogue size in the page furniture (`<li class="icon fa-film">12345
        videos</li>`), so the denominator of the catalogue share costs no request at all. Only
        the header block is read: a per-tag or per-creator count elsewhere on the page is a
        different number and must never be mistaken for the catalogue.
        """
        for node in LexborHTMLParser(html).css("ul.header-meta li.fa-film"):
            match = _CATALOGUE_TOTAL.match(clean_text(node.text()))
            if match:
                return CatalogueTotal(
                    total=int(match.group(1)),
                    source_path="/* (header meta)",
                    note="The site's own `N videos` counter in its page header.",
                )
        return None


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    # Fixtures have monotonic IDs. Sort explicitly before assigning the newest-card priorities.
    first_page = _page_number(url) < 2
    links: list[TaskSpec] = []
    cards: list[tuple[int, str, LexborNode]] = []
    for card in _main_cards(tree):
        href = absolute_url(url, card.attributes.get("href"))
        match = _VIDEO_PATH.match(urlsplit(href).path) if href else None
        if href is None or match is None:
            continue
        cards.append((int(match.group(1)), href, card))
    for _, href, card in sorted(cards, key=lambda item: item[0], reverse=True):
        priority = detail_priority(len(links), first_page=first_page)
        links.append(TaskSpec(href, CrawlTask.Kind.DETAIL, priority, _card_media(url, card)))
    pages = (absolute_url(url, a.attributes.get("href")) for a in tree.css("ul.pagination a[href]"))
    for href in dedupe(page for page in pages if page and page != url):
        links.append(TaskSpec(href, kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _page_number(url: str) -> int:
    """Page number from `?q=…&p=2` or paths like `/hdvideo/2`, `/artist/{slug}/2`; 1 if absent."""
    parts = urlsplit(url)
    query_page = parse_qs(parts.query).get("p", [""])[0]
    if query_page.isdigit():
        return int(query_page)
    match = _PAGE_NUMBER_PATH.search(parts.path)
    return int(match.group(1)) if match else 1


def _main_cards(tree: LexborHTMLParser) -> list[LexborNode]:
    """Video card anchors of the main list, in page order, up to the random related block."""
    body = tree.body
    if body is None:
        return []
    cards: list[LexborNode] = []
    for node in body.traverse():
        classes = (node.attributes.get("class") or "").split()
        if node.tag == "h2" and "major" in classes and cards:
            break
        if node.tag == "a" and "featured" in classes and node.css_first('img[id^="cover_"]'):
            cards.append(node)
    return cards


def _card_media(base: str, card: LexborNode) -> tuple[MediaSpec, ...]:
    media: list[MediaSpec] = []
    cover = card.css_first('img[id^="cover_"]')
    poster = absolute_url(base, cover.attributes.get("src")) if cover else None
    if poster:
        media.append(MediaSpec(Media.Kind.POSTER, poster))
    handlers = (node.attributes.get("onmouseover") or "" for node in card.css("[onmouseover]"))
    frames = dedupe(
        match.group(1) for handler in handlers for match in _FRAME_IMAGE.finditer(handler)
    )
    for frame in frames:
        frame_url = absolute_url(base, frame)
        if frame_url:
            media.append(MediaSpec(Media.Kind.FRAME, frame_url))
    return tuple(media)


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    heading = tree.css_first("h1.main-h1")
    if heading is None:
        return ParseResult()
    title_node = tree.css_first("title")
    title = clean_text(title_node.text() if title_node else "").removesuffix(_TITLE_SUFFIX)
    title = title or clean_text(heading.text())
    if not title:
        return ParseResult()

    # The video's own `ul.meta` sits next to the heading; sidebar teasers use `ul.meta` too.
    header = heading.parent
    meta = header.css_first("ul.meta") if header else None
    performers = dedupe(
        clean_text(a.text())
        for a in (meta.css('li.fa-star-o a[href^="/artist/"]') if meta else [])
    )
    categories = dedupe(clean_text(a.text()) for a in tree.css('a.tag-link[href^="/category/"]'))
    duration = meta.css_first("li.fa-clock-o") if meta else None
    date = meta.css_first("li.fa-calendar") if meta else None

    page = PageData(
        url=canonical_url(tree, url) or url,
        title=title,
        tags=tuple(categories),
        performers=tuple(performers),
        duration_s=_parse_duration(clean_text(duration.text()) if duration else ""),
        extra={"categories": categories, "date_text": clean_text(date.text()) if date else ""},
    )
    return ParseResult(page=page)


def _parse_duration(value: str) -> int | None:
    """Duration text such as "1h 5m 32s" or "17m 10s" -> seconds."""
    match = _DURATION.match(value)
    if not value or not match:
        return None
    parts = match.groupdict()
    return int(parts["h"] or 0) * 3600 + int(parts["m"] or 0) * 60 + int(parts["s"] or 0)
