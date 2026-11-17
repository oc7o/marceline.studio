"""site12.example adapter (video aggregator on a stock template, not KVS).

- Seeds: `/search/?s={name}` per creator name and alias, as the site's search form writes it (the
  form's action is `/search/`, its field `s`). Search only: the site's `/performer/{slug}/` links
  point at unrelated slugs (a "Lena Hartwell" credit links `/performer/mira-kovacs/`), so a
  performer page cannot be derived from a name.
- Search pages: 60 cards in `ul.thumbs-videos-list`, one `a.thumb-video-link` per video
  (`/video/{id}/{slug}/`) with a lazy `img.thumb[data-original]` thumbnail on `tn1.site12.example`
  (a CDN). Pagination is not a link: `div.paginator[data-url][data-page][data-max]` names the page
  URL template (`…&page=%p%`), the current page and the last page. The search is fuzzy and a
  well-known name reports ~90 pages, so pagination stops after `MAX_SEARCH_PAGE`.
- Video pages: title, duration (seconds), upload date and tags from the og/video meta tags,
  performers from `ul.models-list` link *texts* (their hrefs are unrelated slugs, see above), the
  aggregated source site (`/search/?site=…`) and the embedded player iframe (an external host,
  kept in `extra`, never fetched). The poster is the player config's `thumb` on the CDN host,
  else `og:image` (the same path on the origin). Video pages yield no links.
- Missing videos answer HTTP 404 (the worker marks them removed); a 404 body has no `og:title`,
  so it never yields a page here either.
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
    meta_content,
    meta_contents,
    normalize_name,
    parse_datetime,
)
from index.models import Media

DOMAIN = "site12.example"
# Search is fuzzy: past the first pages results only share a word with the query. 10 pages are
# 600 results, more than any target creator's real catalogue on the site.
MAX_SEARCH_PAGE = 10
_VIDEO_PATH = re.compile(r"^/video/\d+/[^/]+/?$")
_CARD_ANCHORS = "ul.thumbs-videos-list div.thumb-video a.thumb-video-link[href]"
# The player config: `var video = { id: '…', duration: '38:08', thumb: 'https://tn1…/1.jpg' }`.
_PLAYER_THUMB = re.compile(r"""\bvar\s+video\s*=\s*\{[^}]*?\bthumb:\s*['"]([^'"]+)['"]""")


class Site12Adapter:
    domain = DOMAIN

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks = [task for name in names for task in self.search_tasks(site, name)]
        return list({task.url: task for task in tasks}.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/search/?s=` with the normalized words, spaces as `+`, as the search form sends it."""
        text = normalize_name(query)
        if not text:
            return []
        url = f"https://{site.domain}/search/?{urlencode({'s': text})}"
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
    page_number = _page_number(url)
    media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
    for anchor in tree.css(_CARD_ANCHORS):
        href = absolute_url(url, anchor.attributes.get("href"))
        if href is None or not _VIDEO_PATH.match(urlsplit(href).path):
            continue
        media_by_url.setdefault(href, _card_media(url, anchor))
    # No cards is the stop condition: nothing is known about past-the-end pages.
    if not media_by_url:
        return ParseResult()
    links = [
        TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=page_number < 2), media)
        for i, (href, media) in enumerate(media_by_url.items())
    ]
    next_page = _next_page(url, tree, page_number)
    if next_page:
        links.append(TaskSpec(next_page, kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _card_media(base: str, anchor: LexborNode) -> tuple[MediaSpec, ...]:
    """The card thumbnail: `data-original` (the `src` is a base64 GIF placeholder)."""
    image = anchor.css_first("img")
    if image is None:
        return ()
    src = absolute_url(base, image.attributes.get("data-original"))
    return (MediaSpec(Media.Kind.POSTER, src),) if src else ()


def _page_number(url: str) -> int:
    value = parse_qs(urlsplit(url).query).get("page", [""])[0]
    return int(value) if value.isdigit() and int(value) > 0 else 1


def _next_page(url: str, tree: LexborHTMLParser, page_number: int) -> str | None:
    """The next page from the paginator's URL template, up to `data-max` and `MAX_SEARCH_PAGE`."""
    node = tree.css_first("div.paginator[data-url]")
    if node is None:
        return None
    template = node.attributes.get("data-url") or ""
    last = node.attributes.get("data-max") or ""
    if "%p%" not in template or not last.isdigit():
        return None
    following = page_number + 1
    if following > min(int(last), MAX_SEARCH_PAGE):
        return None
    href = absolute_url(url, template.replace("%p%", str(following)))
    return href if href and href != url else None


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    title = meta_content(tree, "og:title")
    if not title:
        return ParseResult()
    info = tree.css_first("div.video-info")
    tags = _link_texts(info, "ul.tags-list a") or meta_contents(tree, "video:tag")
    duration = meta_content(tree, "og:video:duration")
    source = info.css_first('a[href*="site="]') if info else None
    player = tree.css_first("div.player iframe[src]")
    page = PageData(
        url=canonical_url(tree, url) or absolute_url(url, meta_content(tree, "og:url")) or url,
        title=title,
        tags=tuple(tags),
        performers=tuple(_link_texts(info, "ul.models-list a")),
        uploaded_at=parse_datetime(meta_content(tree, "og:video:release_date")),
        duration_s=int(duration) if duration.isdigit() else None,
        extra={
            "source_site": clean_text(source.text()) if source else "",
            "embed_url": absolute_url(url, player.attributes.get("src")) if player else None,
        },
    )
    return ParseResult(page=page, media=_video_media(url, tree))


def _link_texts(node: LexborNode | None, selector: str) -> list[str]:
    return dedupe(clean_text(anchor.text()) for anchor in node.css(selector)) if node else []


def _video_media(url: str, tree: LexborHTMLParser) -> tuple[MediaSpec, ...]:
    """The player config's CDN thumbnail, else `og:image` (the same image on the origin host)."""
    thumb = None
    for script in tree.css("script"):
        match = _PLAYER_THUMB.search(script.text(deep=True))
        if match:
            thumb = absolute_url(url, match.group(1))
            break
    poster = thumb or absolute_url(url, meta_content(tree, "og:image"))
    return (MediaSpec(Media.Kind.POSTER, poster),) if poster else ()
