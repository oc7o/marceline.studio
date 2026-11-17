"""site11.example adapter (bespoke PHP site, photo-first).

The site is organised per performer, and photo posts — not videos — are the point: every post on a
profile grid is one full-resolution image, and the sampled profile had 39 photos and no video at
all. Photo posts are therefore first-class pages here, and their image is the work itself, not a
preview of something else — it is stored as `PHOTO`, the kind issue #69 added for exactly this.

- Seeds: the profile `/{slug}/` per creator name and alias. The global video feed `/videos/`,
  the only place `.mp4` URLs exist, is `listing_tasks`: enqueued only on request (issue #71).
- Search: none. The site's only search is `POST /search_v2/`; adapters emit GET tasks, so
  `search_tasks` returns nothing (see its docstring).
- Profile listings `/{slug}/` and `/{slug}/page-N/`: post links `/{slug}/{num}/` of that same
  profile, numbered per profile and descending. Pagination follows the plain `#next_page` anchor,
  never the `/ajax/` endpoint behind `div#showmore`, and stops at that div's `data-max`: the last
  page still renders a "Next Page" link one past the end.
- Video listing `/videos/` and `/videos/page-N/`: `/video/new/{id}/` post links. The per-card
  "Open content page" link and the performer link are not enqueued (see below).
- Photo posts `/{slug}/{num}/`: no JSON-LD, no og tags, no canonical, no date, no duration. The
  title carries the type and the per-profile number ("… Photo #40 - Site11"), the performer comes
  from the post header, the image `alt` and the URL slug. The full photo (`n1.site11.example`) is the
  page's `PHOTO`: the work itself, hashed by the same image decoder a poster uses.
- Video posts `/video/new/{id}/`: the only pages with structured data. JSON-LD `VideoObject` plus a
  full og set give title, upload date, duration, poster (`site11.example`) and the mp4
  (`cdn.site11.example`, emitted as `PREVIEW`, the only kind the video decoder accepts).
- Listing cards carry no media: their `_300px.jpg` thumbnail is a scaled copy of the very image the
  post page links in full, and card media only ever becomes fetchable once that post page has been
  parsed anyway (ADR 0018), so attaching it would fetch and hash the same picture twice.
- Language subdomains (`de.`, `fr.`, …) mirror every apex path and the worker's on-site filter
  accepts subdomains, so every link is filtered down to the apex host here; the mirrors are
  duplicates and are never enqueued.
- Missing posts answer a hard HTTP 404 (the worker marks them removed); the 404 body is itself
  listing-shaped, with cards from unrelated profiles, so `parse` never trusts it: `removed` is
  never set from body text and a page without the post marker in its title yields nothing.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

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
    parse_datetime,
    parse_iso_duration,
    slugify,
    str_field,
)
from index.models import Media

DOMAIN = "site11.example"
VIDEO_SECTION = "/videos/"
# The video feed is a global firehose (`data-max="100"` in the recorded fixture) while the crawl is
# creator-targeted, so only its first pages are followed. Profiles keep their own `data-max`.
MAX_VIDEO_FEED_PAGES = 10

_SITE_HOSTS = frozenset({DOMAIN, f"www.{DOMAIN}"})
_TITLE_SUFFIX = " - Site11"
_POST_PATH = re.compile(r"^/(?P<slug>[^/]+)/(?P<number>\d+)/$")
_VIDEO_POST_PATH = re.compile(r"^/video/new/(?P<id>\d+)/")
_VIDEO_POST_LINK = re.compile(r"^/video/new/\d+/$")
_PROFILE_PATH = re.compile(r"^/(?P<slug>[^/]+)/(?:page-(?P<page>\d+)/)?$")
_VIDEOS_PATH = re.compile(r"^/videos/(?:page-(?P<page>\d+)/)?$")
_PAGE_LINK = re.compile(r"^(?P<section>/.*/)page-(?P<page>\d+)/$")
# "LenaH233 / … Leaked Photo #40": the only type and id markers on a post page.
_POST_MARKER = re.compile(r"\b(?P<type>Photo|Video)\s*#(?P<number>\d+)", re.IGNORECASE)
# The headline repeats every alias before the "Leaked …" boilerplate.
_HEADLINE_TAIL = re.compile(r"\s+Leaked\b", re.IGNORECASE)
# `/content/{a}/{b}/{slug}/{bucket}/{slug}_0040.jpg`; the `_300px` thumbnails do not match.
_FULL_IMAGE = re.compile(r"^(?P<slug>.+)_(?P<number>\d+)\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)


class Site11Adapter:
    domain = DOMAIN

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        """A profile per creator name and alias. The global video feed is `listing_tasks`.

        Profile URLs are root-level (`/{slug}/`) and directly addressable, which makes them a
        better seed than any search would be. A creator whose slug carries the site's `-N`
        disambiguation suffix (`lena-1`, `k1ra-3`) is missed; there is no GET search to fall
        back on, so those profiles need the operator's slug.
        """
        base = f"https://{site.domain}"
        slugs = dedupe(slugify(name) for creator in creators for name in creator_names(creator))
        return [
            TaskSpec(f"{base}/{slug}/", CrawlTask.Kind.LISTING, SEED_PRIORITY) for slug in slugs
        ]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """The global video feed: everyone's uploads, only on request (issue #71)."""
        return [
            TaskSpec(f"https://{site.domain}{VIDEO_SECTION}", CrawlTask.Kind.LISTING, SEED_PRIORITY)
        ]

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """None: the site's only search is `POST /search_v2/`, and adapters emit GET tasks.

        There is no GET search URL to build. Whether `/search_v2/` would also answer `?q=` is
        untested (testing it means a request, and recon is closed), so nothing is guessed here —
        a guessed seed would 404 or, worse, silently return the wrong page. `crawl_seed --query`
        therefore enqueues nothing for this site; creator seeds go through profile URLs instead.
        """
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_detail(url, tree)
        return _parse_listing(kind, url, tree)


def _parse_detail(url: str, tree: LexborHTMLParser) -> ParseResult:
    """Video posts are the only pages with JSON-LD, so the data decides, not the path."""
    video = first_json_ld(tree, "VideoObject")
    if video:
        return _parse_video_post(url, tree, video)
    return _parse_photo_post(url, tree)


def _parse_photo_post(url: str, tree: LexborHTMLParser) -> ParseResult:
    """A photo post: the title's "Photo #N" marker, the performer, and the full image.

    The marker is required. Without it the HTML is not a post page — the site's 404 body is a
    listing of unrelated profiles, and accepting it would index another performer's pictures.
    """
    match = _POST_PATH.match(urlsplit(url).path)
    title = _page_title(tree)
    marker = _POST_MARKER.search(title)
    if match is None or marker is None:
        return ParseResult()

    slug = match["slug"]
    image = _full_image(url, tree, slug, int(match["number"]))
    names = [
        *_profile_names(tree, slug),
        *_headline_names(image.alt if image else ""),
        *_headline_names(title),
        slug,
    ]
    page = PageData(
        url=url,
        title=title,
        performers=tuple(dedupe(names)),
        extra={
            "post_type": marker["type"].lower(),
            "post_number": int(marker["number"]),
            "profile_slug": slug,
        },
    )
    # The image is the work itself, not a preview of a video: PHOTO says so (issue #69). It is
    # decoded and hashed exactly like a poster — one still image, one PDQ frame.
    media = (MediaSpec(Media.Kind.PHOTO, image.url),) if image else ()
    return ParseResult(page=page, media=media)


def _parse_video_post(url: str, tree: LexborHTMLParser, video: dict[str, object]) -> ParseResult:
    title = str_field(video, "name") or meta_content(tree, "og:title")
    if not title:
        return ParseResult()

    page_url = canonical_url(tree, url) or absolute_url(url, meta_content(tree, "og:url")) or url
    poster = absolute_url(url, str_field(video, "thumbnailUrl") or meta_content(tree, "og:image"))
    content = absolute_url(url, str_field(video, "contentUrl") or meta_content(tree, "og:video"))
    # The performer is not marked up: the title is "{Display Name}, {alias}", and the media path
    # carries the profile slug (`/content/l/e/lena-hartwell/1000/…`).
    slug = _content_slug(poster or content or "")
    names = [*(clean_text(part) for part in title.split(",")), slug]
    video_id = _VIDEO_POST_PATH.match(urlsplit(page_url).path)

    page = PageData(
        url=_site_url(page_url) or url,
        title=title,
        performers=tuple(dedupe(names)),
        uploaded_at=parse_datetime(str_field(video, "uploadDate")),
        duration_s=parse_iso_duration(str_field(video, "duration")),
        extra={
            "post_type": "video",
            "video_id": video_id["id"] if video_id else "",
            "profile_slug": slug,
        },
    )
    media: list[MediaSpec] = []
    if poster:
        # A still *of* the clip, not the work itself: POSTER, never PHOTO (issue #69).
        media.append(MediaSpec(Media.Kind.POSTER, poster))
    if content:
        # PREVIEW is the only kind the media worker hashes with the video decoder. These clips are
        # short (23 s in the fixture) and are the whole upload, not a teaser of a longer file.
        media.append(MediaSpec(Media.Kind.PREVIEW, content))
    return ParseResult(page=page, media=tuple(media))


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    """Profile grids and the video feed. Other sections (`/`, `/hot/`, `/feed/…`) are not crawled.

    The crawl is creator-targeted: a profile is seeded per creator, so the site-wide feeds would
    only pull in performers nobody tracks.
    """
    path = urlsplit(url).path
    videos = _VIDEOS_PATH.match(path)
    if videos is not None:
        page = int(videos["page"] or 1)
        links = _detail_links(url, tree, page, _is_video_post)
        return _with_pagination(kind, url, tree, links, VIDEO_SECTION, page, MAX_VIDEO_FEED_PAGES)
    profile = _PROFILE_PATH.match(path)
    if profile is None:
        return ParseResult()
    slug, page = profile["slug"], int(profile["page"] or 1)
    links = _detail_links(url, tree, page, lambda p: _is_profile_post(p, slug))
    return _with_pagination(kind, url, tree, links, f"/{slug}/", page, None)


def _detail_links(
    url: str, tree: LexborHTMLParser, page: int, wanted: Callable[[str], bool]
) -> list[TaskSpec]:
    hrefs: list[str] = []
    for anchor in tree.css("a[href]"):
        href = _site_url(absolute_url(url, anchor.attributes.get("href")))
        if href is not None and wanted(urlsplit(href).path) and href not in hrefs:
            hrefs.append(href)
    return [
        TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=page < 2))
        for i, href in enumerate(hrefs)
    ]


def _with_pagination(
    kind: CrawlTask.Kind,
    url: str,
    tree: LexborHTMLParser,
    links: list[TaskSpec],
    section: str,
    page: int,
    cap: int | None,
) -> ParseResult:
    """Add the next page, if there is one. A listing without cards ends the walk."""
    if not links:
        return ParseResult()
    next_url = _next_page_url(url, tree, section, page, cap)
    if next_url is not None:
        links.append(TaskSpec(next_url, kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _next_page_url(
    url: str, tree: LexborHTMLParser, section: str, page: int, cap: int | None
) -> str | None:
    """The plain `#next_page` link, bounded by `div#showmore[data-max]`.

    The last page still renders a "Next Page" anchor one past the end (the recorded page 2 of a
    two-page profile links `page-3/`), and the 404 body links `/random/`, so both the page number
    and the section are checked against this listing's own.
    """
    showmore = tree.css_first("div#showmore[data-max]")
    data_max = (showmore.attributes.get("data-max") or "") if showmore else ""
    if not data_max.isdigit():
        return None
    last = min(int(data_max), cap) if cap is not None else int(data_max)
    anchor = tree.css_first("#next_page a[href]")
    href = _site_url(absolute_url(url, anchor.attributes.get("href")) if anchor else None)
    match = _PAGE_LINK.match(urlsplit(href).path) if href else None
    if match is None or match["section"] != section:
        return None
    number = int(match["page"])
    return href if page < number <= last else None


def _is_profile_post(path: str, slug: str) -> bool:
    match = _POST_PATH.match(path)
    return match is not None and match["slug"] == slug


def _is_video_post(path: str) -> bool:
    return _VIDEO_POST_LINK.match(path) is not None


def _site_url(url: str | None) -> str | None:
    """Keep apex URLs only: `de.site11.example` and friends mirror every path."""
    if url is None:
        return None
    return url if (urlsplit(url).hostname or "").lower() in _SITE_HOSTS else None


def _page_title(tree: LexborHTMLParser) -> str:
    node = tree.css_first("title")
    return clean_text(node.text() if node else "").removesuffix(_TITLE_SUFFIX).strip()


@dataclass(frozen=True)
class _Image:
    url: str
    alt: str


def _full_image(url: str, tree: LexborHTMLParser, slug: str, number: int) -> _Image | None:
    """The post's full-size photo: the `<img>` whose file is this profile's post `number`.

    The basename check (`{slug}_0040.jpg` for post 40) keeps out the profile avatar in the post
    header — same slug, a different number — as well as other profiles' cards and the `_300px`
    thumbnails of the surrounding grid.
    """
    for image in tree.css("img[src]"):
        src = absolute_url(url, image.attributes.get("src"))
        if src is None:
            continue
        match = _FULL_IMAGE.match(urlsplit(src).path.rsplit("/", 1)[-1])
        if match is not None and match["slug"] == slug and int(match["number"]) == number:
            return _Image(src, clean_text(image.attributes.get("alt")))
    return None


def _profile_names(tree: LexborHTMLParser, slug: str) -> list[str]:
    """The display name from the post header's link back to the profile."""
    names = []
    for anchor in tree.css("a.capitalize[href]"):
        href = anchor.attributes.get("href") or ""
        if urlsplit(href).path == f"/{slug}/":
            names.append(clean_text(anchor.text()))
    return names


def _headline_names(text: str) -> list[str]:
    """Aliases from a headline: "A / b / c Leaked Photo #40" -> [A, b, c]."""
    head = _HEADLINE_TAIL.split(text, maxsplit=1)[0]
    if head == text:  # no boilerplate: not a headline, don't guess
        return []
    return [clean_text(part) for part in head.split("/")]


def _content_slug(media_url: str) -> str:
    """Profile slug from a media path: `/content/l/e/lena-hartwell/1000/…` -> the slug."""
    parts = [part for part in urlsplit(media_url).path.split("/") if part]
    return parts[3] if len(parts) > 4 and parts[0] == "content" else ""
