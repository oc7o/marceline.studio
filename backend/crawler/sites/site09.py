"""site09.example adapter (bespoke Laravel/Bootstrap leak index).

The site indexes by FanHub-style handle, which is the shape we seed from: `/{handle}/video` is a
creator's whole video catalogue and the handle is usually the creator's name without separators.

- Seeds: `/{handle}/video` per creator name and alias (a wrong guess answers HTTP 404, which the
  worker handles) and a `/search?search={name}` seed per name. `/videos` (the newest uploads of
  everyone) is `listing_tasks`, enqueued only on request (issue #71).
- Search (`/search?search={words}`) returns **performer cards only — never a video link**, so it is
  a handle-discovery endpoint: parsing one enqueues `/{handle}/video` for each creator card found.
  That is also what free-text `search_tasks` seeds buy, one hop later.
- Cards (`div.item > article.movie-item > a[href]`) appear on `/videos`, performer pages and search
  with identical markup; hrefs are absolute on `/videos` and search but relative on performer pages.
  Only `/{handle}/video/{id}` cards become detail tasks — photo cards and the performer cards of the
  homepage are skipped, as is the site-wide `ul.movies-comming` sidebar widget (its items are
  `li.movie-item`, not `div.item > article.movie-item`).
- Pagination has two rules: `/videos` has a real Bootstrap paginator (`ul.pagination`, `?page=N`),
  while `/{handle}/video?page=N` serves cards but contains no paginator markup at all, so the next
  page is synthesized and the walk stops on the first page without cards (MAX_SYNTHETIC_PAGE is a
  backstop in case the site never serves an empty page).
- Video pages `/{handle}/video/{id}`: no JSON-LD, no `video:*` meta, no duration, no tags and no
  absolute date — `span.updated_at` is relative ("2 years ago") and adapters have no clock, so
  `uploaded_at` stays None rather than being invented. The listing card's absolute `span.date` is
  the only absolute date on the site, but `TaskSpec` carries media, not dates, so it cannot travel
  to the detail task. Title is the generated `h1`; the real FanHub caption goes to
  `extra["caption"]` (its selector is fragile, and extra is still safety-scanned).
- Media: exactly one image per video, the `_thumb.webp` frame under `/storage/images/` on
  image-cdn.site09.example, stored as the page's single POSTER. `og:image` is the **performer avatar**
  (`/storage/models/…`) and must never be stored, so every media URL is checked for the image path.
- Missing videos answer HTTP 404; there is no soft-removal marker on a 200 page, so `removed` is
  never set. Video pages yield no links: the prev/next chain only re-walks a performer's catalogue,
  which the section pages already enumerate.
"""

import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

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

# Backstop for synthesized pagination; a performer with 349 videos has ~8 pages of 48 cards.
MAX_SYNTHETIC_PAGE = 100

_VIDEO_PATH = re.compile(r"^/(?P<handle>[^/]+)/video/(?P<id>\d+)/?$")
_SECTION_PATH = re.compile(r"^/(?P<handle>[^/]+)/video/?$")
_PERFORMER_PATH = re.compile(r"^/(?P<handle>[^/]+)/?$")
_HANDLE = re.compile(r"^[A-Za-z0-9._-]+$")
# Single-segment paths that are sections of the site, not performer handles.
_RESERVED_PATHS = frozenset(
    {
        "videos",
        "photos",
        "photo",
        "video",
        "hot",
        "creators",
        "search",
        "dmca",
        "login",
        "logout",
        "register",
        "contact",
        "privacy",
        "terms",
        "storage",
        "asset",
        "api",
    }
)
_CARD_SELECTOR = "div.item > article.movie-item"
_CARD_IMAGE_ATTRIBUTES = ("src", "data-thumb")
# Video frames live here; performer avatars live under /storage/models/ and are never stored.
_VIDEO_IMAGE_PATH = "/storage/images/"
_CAPTION_SELECTOR = "h1.title-post-detail + div:not([class]) > span"
_PERFORMER_SELECTOR = "div.actor-section-post-detail .actor-title-port a[href]"


class Site09Adapter:
    domain = "site09.example"

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        """`/{handle}/video` per name and alias and a search seed per name."""
        base = f"https://{site.domain}"
        names = [name for creator in creators for name in creator_names(creator)]
        handles = dedupe(_handle_from_name(name) for name in names)
        tasks = [
            TaskSpec(f"{base}/{handle}/video", CrawlTask.Kind.LISTING, SEED_PRIORITY)
            for handle in handles
        ]
        # Search maps a creator name to the handles we cannot guess (see search_tasks).
        tasks.extend(task for name in names for task in self.search_tasks(site, name))
        return list({task.url: task for task in tasks}.values())

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """The newest-videos listing of every creator, only on request (issue #71)."""
        return [TaskSpec(f"https://{site.domain}/videos", CrawlTask.Kind.LISTING, SEED_PRIORITY)]

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/search?search={words}`; empty when the text has no ASCII words.

        The page answers with performer cards and zero video links, so this seed is not a video
        search: it is the site's only handle-discovery endpoint. Parsing it enqueues the
        `/{handle}/video` catalogue of every creator card it found, which is where the videos are.
        """
        words = normalize_name(query)
        if not words:
            return []
        url = f"https://{site.domain}/search?{urlencode({'search': words})}"
        return [TaskSpec(url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        return _parse_listing(kind, url, tree)


def _handle_from_name(name: str) -> str:
    """Handle for a creator name: "Lena Hartwell" -> "lenahartwell" (handles carry no separators)."""
    handle = normalize_name(name).replace(" ", "")
    return handle if _is_handle(handle) else ""


def _is_handle(handle: str) -> bool:
    if not handle or handle.lower() in _RESERVED_PATHS:
        return False
    return _HANDLE.match(handle) is not None


def _page_number(url: str) -> int:
    """Page number from `?page=N`; 1 if absent."""
    page = dict(parse_qsl(urlsplit(url).query)).get("page", "")
    return int(page) if page.isdigit() else 1


def _page_url(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "page"]
    query.append(("page", str(page)))
    return parts._replace(query=urlencode(query), fragment="").geturl()


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    page_number = _page_number(url)
    media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
    performer_sections: list[str] = []
    for card in tree.css(_CARD_SELECTOR):
        anchor = card.css_first("a[href]")
        href = absolute_url(url, anchor.attributes.get("href")) if anchor else None
        if href is None:
            continue
        path = urlsplit(href).path
        if _VIDEO_PATH.match(path):
            media_by_url.setdefault(href, _card_media(url, card))
        elif kind == CrawlTask.Kind.SEARCH:
            # Search results are performer cards: follow them to the creator's video catalogue.
            section = _section_url(href)
            if section:
                performer_sections.append(section)
    links = [
        TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=page_number < 2), media)
        for i, (href, media) in enumerate(media_by_url.items())
    ]
    links.extend(
        TaskSpec(section, CrawlTask.Kind.LISTING, SEED_PRIORITY)
        for section in dedupe(performer_sections)
    )
    links.extend(_section_links(url, tree))
    links.extend(_pagination(kind, url, tree, page_number, has_cards=bool(media_by_url)))
    return ParseResult(links=tuple(links))


def _section_url(performer_url: str) -> str:
    """`https://host/{handle}` -> `https://host/{handle}/video`; empty for non-performer paths."""
    parts = urlsplit(performer_url)
    match = _PERFORMER_PATH.match(parts.path)
    if match is None or not _is_handle(match.group("handle")):
        return ""
    return parts._replace(path=f"/{match.group('handle')}/video", query="", fragment="").geturl()


def _section_links(url: str, tree: LexborHTMLParser) -> list[TaskSpec]:
    """The `/{handle}/video` nav link of a performer landing page (its full catalogue)."""
    current = urlsplit(url).path.rstrip("/")
    sections: list[str] = []
    for anchor in tree.css("a[href]"):
        href = absolute_url(url, anchor.attributes.get("href"))
        if href is None:
            continue
        path = urlsplit(href).path
        match = _SECTION_PATH.match(path)
        if match and _is_handle(match.group("handle")) and path.rstrip("/") != current:
            sections.append(href)
    return [
        TaskSpec(section, CrawlTask.Kind.LISTING, SEED_PRIORITY) for section in dedupe(sections)
    ]


def _pagination(
    kind: CrawlTask.Kind,
    url: str,
    tree: LexborHTMLParser,
    page_number: int,
    *,
    has_cards: bool,
) -> list[TaskSpec]:
    """Paginator links when the page has one, otherwise the synthesized next section page."""
    pages = (absolute_url(url, a.attributes.get("href")) for a in tree.css("ul.pagination a[href]"))
    ahead = dedupe(page or "" for page in pages if page and _page_number(page) > page_number)
    if ahead:
        return [TaskSpec(page, kind, PAGINATION_PRIORITY) for page in ahead]
    if has_cards and _SECTION_PATH.match(urlsplit(url).path) and page_number < MAX_SYNTHETIC_PAGE:
        return [TaskSpec(_page_url(url, page_number + 1), kind, PAGINATION_PRIORITY)]
    return []


def _card_media(base: str, card: LexborNode) -> tuple[MediaSpec, ...]:
    """The card image: the video's only frame. Avatars and placeholders are never stored."""
    image = card.css_first("img.post-thumbnail")
    if image is None:
        return ()
    candidates = (absolute_url(base, image.attributes.get(key)) for key in _CARD_IMAGE_ATTRIBUTES)
    src = next((c for c in candidates if c and _VIDEO_IMAGE_PATH in urlsplit(c).path), None)
    return (MediaSpec(Media.Kind.POSTER, src),) if src else ()


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    heading = tree.css_first("h1.title-post-detail")
    title = clean_text(heading.text()) if heading else ""
    if not title:
        return ParseResult()

    canonical = canonical_url(tree, url) or absolute_url(url, meta_content(tree, "og:url")) or url
    match = _VIDEO_PATH.match(urlsplit(canonical).path) or _VIDEO_PATH.match(urlsplit(url).path)
    handle = match.group("handle") if match else ""
    performer_node = tree.css_first(_PERFORMER_SELECTOR)
    performer = clean_text(performer_node.text()) if performer_node else ""
    page = PageData(
        url=canonical,
        title=title,
        # The site has no tags, categories, duration or absolute upload date on video pages, and
        # `span.updated_at` is relative ("2 years ago") — an adapter has no clock to resolve it.
        performers=tuple(dedupe([performer, handle])),
        extra={
            "handle": handle,
            "video_id": match.group("id") if match else "",
            "caption": _caption(tree),
            "views": _text(tree, "div.more-info-post-detail span.view"),
            "updated_at_text": _text(tree, "div.more-info-post-detail span.updated_at"),
            "performer": performer,
            "performer_url": absolute_url(url, performer_node.attributes.get("href"))
            if performer_node
            else "",
        },
    )
    return ParseResult(page=page, media=_video_media(url, tree))


def _caption(tree: LexborHTMLParser) -> str:
    """The original FanHub caption below the generated heading; missing on some videos."""
    return _text(tree, _CAPTION_SELECTOR)


def _text(tree: LexborHTMLParser, selector: str) -> str:
    node = tree.css_first(selector)
    return clean_text(node.text()) if node else ""


def _video_media(url: str, tree: LexborHTMLParser) -> tuple[MediaSpec, ...]:
    """The single `_thumb.webp` frame in the player.

    Never `og:image`: that is the performer's avatar (`/storage/models/…`), not a frame of this
    video, and storing it would hash one creator portrait onto every one of their pages.
    """
    image = tree.css_first("#media-player img.thumbnail")
    src = absolute_url(url, image.attributes.get("src")) if image else None
    if src is None or _VIDEO_IMAGE_PATH not in urlsplit(src).path:
        return ()
    return (MediaSpec(Media.Kind.POSTER, src),)
