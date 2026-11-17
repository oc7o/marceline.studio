"""site05.example adapter (own video platform).

- Seeds: `/search/{slug}/` per creator name and alias. A search that matches a tag redirects to the
  tag page `/tag/{slug}-{suffix}/`; `parse` receives the final URL. Performer pages
  (`/performer/{slug}-{suffix}/`) carry an unpredictable suffix, so they are not seeded; search
  pages link them in the `#performersearch` box, and that link is followed as a LISTING task.
  Free-text search seeds (`search_tasks`, e.g. video titles) use the same `/search/{slug}/` page.
- Listing, tag, performer and search pages: video cards `#vidresults div.mb` link
  `/video-{id}/{slug}/` with the card image (`static-eu-cdn.site05.example/thumbs/…/{n}_240.jpg`) as
  poster; hover previews are JS-built clips and are ignored. Pagination is plain `div.numlist2`
  links (`/tag/{slug}/2/`, `/performer/{slug}/2/`); links back to page 1 are dropped (page 1 is
  always the entry point). Pages without cards yield no links, so pagination stops there.
- Video pages: title, duration, upload date and actors from JSON-LD `VideoObject`; performers,
  categories and tags from `#video-info-tags`; poster from `og:image`. The related cards are
  ignored and video pages yield no links.
- Missing videos answer HTTP 404 ("Error 404 Page not found!"; the worker marks them removed);
  there is no removal marker on 200 pages, so `removed` is never set.
"""

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

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
    slugify,
    str_field,
)
from index.models import Media

HOST = "www.site05.example"  # the apex redirects here
_VIDEO_PATH = re.compile(r"^/video-[A-Za-z0-9]+/[^/]+/$")
_PERFORMER_PATH = re.compile(r"^/performer/[^/]+/$")
_PAGE_PATH = re.compile(r"^/(?:search|tag|performer|cat)/[^/]+/(\d+)/$")
_IMAGE_ATTRIBUTES = ("data-src", "src")
_TITLE_SUFFIX = " - SITE05"


class Site05Adapter:
    domain = "site05.example"

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks = [task for name in names for task in self.search_tasks(site, name)]
        return list({task.url: task for task in tasks}.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`/search/{slug}/` for a name or free text; empty when the text has no ASCII words."""
        slug = slugify(query)
        if not slug:
            return []
        return [TaskSpec(f"https://{HOST}/search/{slug}/", CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """None: every seed of this site is a creator or search page (issue #71)."""
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_video(url, tree)
        return _parse_listing(kind, url, tree)


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    first_page = _page_number(url) < 2
    media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
    for card in tree.css("#vidresults div.mb"):
        anchor = card.css_first('a[href^="/video-"]')
        href = absolute_url(url, anchor.attributes.get("href")) if anchor else None
        if href is None or not _VIDEO_PATH.match(urlsplit(href).path) or href in media_by_url:
            continue
        media_by_url[href] = _card_media(url, card)
    if not media_by_url:
        return ParseResult()
    links = [
        TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=first_page), media)
        for i, (href, media) in enumerate(media_by_url.items())
    ]
    if first_page:
        stars = (absolute_url(url, a.attributes.get("href")) for a in tree.css("#performersearch a"))
        for star in dedupe(s for s in stars if s and _PERFORMER_PATH.match(urlsplit(s).path)):
            links.append(TaskSpec(star, CrawlTask.Kind.LISTING, SEED_PRIORITY))
    pages = (absolute_url(url, a.attributes.get("href")) for a in tree.css("div.numlist2 a"))
    for page in dedupe(p for p in pages if p and p != url and _page_number(p) >= 2):
        links.append(TaskSpec(page, kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _page_number(url: str) -> int:
    """Page number from paths such as `/tag/{slug}/2/` or `/performer/{slug}/3/`; 1 if absent."""
    match = _PAGE_PATH.match(urlsplit(url).path)
    return int(match.group(1)) if match else 1


def _card_media(base: str, card: LexborNode) -> tuple[MediaSpec, ...]:
    image = card.css_first("img")
    if image is None:
        return ()
    candidates = (absolute_url(base, image.attributes.get(key)) for key in _IMAGE_ATTRIBUTES)
    poster = next((c for c in candidates if c and not c.startswith("data:")), None)
    return (MediaSpec(Media.Kind.POSTER, poster),) if poster else ()


def _parse_video(url: str, tree: LexborHTMLParser) -> ParseResult:
    video = first_json_ld(tree, "VideoObject")
    heading = tree.css_first("#video-info h1")
    if not video and heading is None:
        return ParseResult()
    title = str_field(video, "name") or meta_content(tree, "og:title").removesuffix(_TITLE_SUFFIX)
    if not title:
        return ParseResult()

    info = tree.css_first("#video-info-tags")
    performers = _link_texts(info, "li.vit-performer a") or _actor_names(video)
    categories = _link_texts(info, "li.vit-category a")
    tags = _link_texts(info, "li.vit-tag a")
    duration_meta = meta_content(tree, "og:duration")
    duration_s = (
        int(duration_meta)
        if duration_meta.isdigit()
        else parse_iso_duration(str_field(video, "duration"))
    )
    page = PageData(
        url=canonical_url(tree, url) or url,
        title=title,
        tags=tuple(dedupe([*categories, *tags])),
        performers=tuple(performers),
        uploaded_at=parse_datetime(str_field(video, "uploadDate")),
        duration_s=duration_s,
        extra={"categories": categories, "tags": tags},
    )
    poster = absolute_url(url, meta_content(tree, "og:image"))
    return ParseResult(page=page, media=(MediaSpec(Media.Kind.POSTER, poster),) if poster else ())


def _link_texts(node: LexborNode | None, selector: str) -> list[str]:
    return dedupe(clean_text(a.text()) for a in node.css(selector)) if node else []


def _actor_names(video: dict[str, object]) -> list[str]:
    actors = video.get("actor")
    items = actors if isinstance(actors, list) else [actors]
    return dedupe(str_field(item, "name") for item in items if isinstance(item, dict))
