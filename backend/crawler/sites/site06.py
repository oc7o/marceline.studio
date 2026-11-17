"""site06.example adapter (mirror of videos from a social network).

- Seeds: `/video/{words+joined+by+plus}` search per creator name and alias (the format of the
  site's own search and trend links). There are no performer pages. Free-text search seeds
  (`search_tasks`, e.g. video titles) use the same search page.
- Listing and search pages: video cards `#list_videos div.item a.item_link` link
  `/watch/-{owner}_{id}`; the card image `data-src` is the poster, either
  `cdn2.media-cdn.example/videos/{owner}/{id}/preview_320.jpg` or a signed
  `img.media-cdn.example/preview/…` proxy of the social network's image. Hover trailers (`data-trailer_url`, `?secure=` tokens) are ignored.
- Pagination: the only marker is the JS "Show more" button `div.more[data-page=N]` (the one without
  `data-action="prev"`), whose plain-page equivalent is `?p=N` (0-based; page 0 has no parameter).
  Search never runs out (page 50 of a name search still had 24 loosely related cards), so pages
  from `MAX_PAGES` on are not followed. Pages without cards yield no links.
- Video pages: title, upload date, duration and poster from JSON-LD `VideoObject` and
  `video:duration` / `og:image`; tags from `.video-tags-bar a.vtag` (fallback `video:tag`). No
  performers. The related cards are ignored and video pages yield no links.
- Deleted videos answer HTTP 200 with `<title>Video deleted</title>` and no `VideoObject`; that
  page is reported as `removed`.
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
    first_json_ld,
    meta_content,
    normalize_name,
    parse_datetime,
    parse_iso_duration,
    str_field,
)
from index.models import Media

MAX_PAGES = 10  # pages 0..9 (240 results) per search
_VIDEO_PATH = re.compile(r"^/watch/-?\d+_\d+$")
_DELETED_TITLE = "Video deleted"


class Site06Adapter:
    domain = "site06.example"

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks = [task for name in names for task in self.search_tasks(site, name)]
        return list({task.url: task for task in tasks}.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/video/{words+joined+by+plus}`; empty when the text has no ASCII words."""
        words = normalize_name(query).split()
        if not words:
            return []
        url = f"https://{site.domain}/video/{'+'.join(words)}"
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
    first_page = _page_number(url) == 0
    media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
    for anchor in tree.css("#list_videos div.item a.item_link"):
        href = absolute_url(url, anchor.attributes.get("href"))
        if href is None or not _VIDEO_PATH.match(urlsplit(href).path) or href in media_by_url:
            continue
        media_by_url[href] = _card_media(url, anchor)
    if not media_by_url:
        return ParseResult()
    links = [
        TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=first_page), media)
        for i, (href, media) in enumerate(media_by_url.items())
    ]
    for button in tree.css("div.more[data-page]"):
        page = button.attributes.get("data-page") or ""
        if button.attributes.get("data-action") == "prev" or not page.isdigit():
            continue
        if 0 < int(page) < MAX_PAGES and int(page) > _page_number(url):
            links.append(TaskSpec(_page_url(url, int(page)), kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _page_number(url: str) -> int:
    """0-based page number from `?p=N`; 0 if absent."""
    page = dict(parse_qsl(urlsplit(url).query)).get("p", "")
    return int(page) if page.isdigit() else 0


def _page_url(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "p"]
    if page > 0:
        query.append(("p", str(page)))
    return parts._replace(query=urlencode(query), fragment="").geturl()


def _card_media(base: str, anchor: LexborNode) -> tuple[MediaSpec, ...]:
    image = anchor.css_first("img[data-src]")
    poster = absolute_url(base, image.attributes.get("data-src")) if image else None
    return (MediaSpec(Media.Kind.POSTER, poster),) if poster else ()


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    video = first_json_ld(tree, "VideoObject")
    if not video:
        title_node = tree.css_first("title")
        deleted = clean_text(title_node.text() if title_node else "") == _DELETED_TITLE
        return ParseResult(removed=deleted)
    title = _clean_title(str_field(video, "name") or meta_content(tree, "og:title"))
    if not title:
        return ParseResult()

    tags = dedupe(clean_text(a.text()) for a in tree.css(".video-tags-bar a.vtag"))
    if not tags:
        tags = dedupe(clean_text(tag) for tag in meta_content(tree, "video:tag").split(","))
    duration_meta = meta_content(tree, "video:duration")
    duration_s = (
        int(duration_meta)
        if duration_meta.isdigit()
        else parse_iso_duration(str_field(video, "duration"))
    )
    page = PageData(
        url=canonical_url(tree, url) or url,
        title=title,
        tags=tuple(tags),
        uploaded_at=parse_datetime(str_field(video, "uploadDate")),
        duration_s=duration_s,
        extra={"tags": tags},
    )
    poster = absolute_url(url, str_field(video, "thumbnailUrl") or meta_content(tree, "og:image"))
    return ParseResult(page=page, media=(MediaSpec(Media.Kind.POSTER, poster),) if poster else ())


def _clean_title(title: str) -> str:
    """Drop zero-width no-break spaces that uploaders paste into titles."""
    return clean_text(title.replace("\ufeff", ""))
