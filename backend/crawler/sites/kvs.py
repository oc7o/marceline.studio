"""Template adapter for video sites built on the KVS content management system.

KVS sites share one CMS, so they share URL and markup conventions; only the theme differs. One
`KvsAdapter` class serves every KVS site, configured by a small declarative `KvsSite`:

- Seeds: `/search/{slug}/` per creator name and alias (the search form's `data-url` is
  `/search/%QUERY%/`), plus model pages (`KvsSite.model_path`, e.g. `/models/{slug}/`) where the
  site has them. Free-text search seeds (`search_tasks`) use the same search page.
- Listing, model and search pages: video cards live in list blocks `#list_videos_{block}`. Each
  card links the video page (`KvsSite.video_path`) with an `<img>` whose `data-original` (or `src`)
  is a `/contents/videos_screenshots/…` image: `preview.jpg` is the poster, numbered images
  (`…/320x180/3.jpg`) are timeline frames, and `data-cnt="5"` announces rotator frames `1..5` of
  the same numbering (capped at `MAX_CARD_FRAMES`). Tokenized `data-preview` clips are ignored.
- Pagination (`#list_videos_{block}_pagination`) is either plain links (`/latest-updates/2/`,
  `/models/{slug}/2/`) or AJAX links (`href="#search"`,
  `data-parameters="q:…;from_videos+from_albums:2"`), whose plain-page equivalent is
  `?from_videos=2` on the page's own (final, post-redirect) URL. Pages without video cards yield
  no links, so pagination stops on empty pages.
- Video pages: title, upload date, duration and poster from JSON-LD `VideoObject` and
  og/video meta tags; tags, categories and performers from per-site selectors. Where a theme lists
  the video's numbered timeline frames as plain images (a screenshots tab,
  `KvsSite.screenshot_selector`), they follow the poster, capped at `MAX_SCREENSHOT_FRAMES`; their
  tokenized `/get_file/` download links are never used. Video pages yield no links, which keeps
  the crawl focused on the seeds. The metadata poster is a template guess — where the site does
  not actually serve it (`KvsSite.poster_from_screenshot`) the video's first numbered screenshot
  is taken instead.
- Missing videos answer HTTP 404 on all KVS sites seen so far (the worker marks them removed);
  there is no reliable removal marker on 200 pages, so `removed` is never set.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
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
    slugify,
    str_field,
)
from index.models import Media

MAX_CARD_FRAMES = 10
MAX_SCREENSHOT_FRAMES = 10
_CARD_ANCHORS = '[id^="list_videos_"] a[href]'
_SCREENSHOT_PATH = "/contents/videos_screenshots/"
_PAGINATION_ANCHORS = '[id^="list_videos_"][id$="_pagination"] a'
_IMAGE_ATTRIBUTES = ("data-original", "data-src", "src")
_NUMBERED_FRAME = re.compile(r"/(\d+)\.jpg$")
# A metadata poster (`…/videos_screenshots/{bucket}/{video}/preview.jpg`) and the size directory
# of a numbered screenshot (`…/{video}/566x302/1.jpg`), for `poster_from_screenshot`.
_METADATA_POSTER = re.compile(rf"^(?P<dir>.*{_SCREENSHOT_PATH}\d+/\d+)/preview[^/]*\.jpg$")
_SCREENSHOT_SIZE = re.compile(rf"{_SCREENSHOT_PATH}\d+/\d+/(?P<size>\d+x\d+)/\d+\.jpg$")
_PAGE_PARAMETER = re.compile(r"(?:^|;)(?P<keys>from[\w+]*):(?P<page>\d+)(?:;|$)")
_PAGE_QUERY_KEYS = ("from_videos", "from")
_PAGE_NUMBER_PATH = re.compile(r"/(\d+)/?$")
# Disallowed by KVS robots.txt files or useless for the index; the worker enforces robots.txt too.
_EXCLUDED_PATH_PREFIXES = (
    "/embed/",
    "/player/",
    "/login",
    "/logout",
    "/signup",
    "/captcha/",
    "/link/",
    "/related_videos_html/",
)


@dataclass(frozen=True)
class KvsSite:
    """Per-site configuration of the KVS template.

    `host` is the host seed URLs use (e.g. `www.site25.example` when the apex redirects to www); it
    defaults to the site's domain. Selectors are CSS selectors for video-page links whose text
    is the tag, category or performer name; an empty selector extracts nothing.

    `search_path` is the search page, as the search form's `data-url` gives it with the query in
    place of `%QUERY%`. It is `/search/{slug}/` on a stock install, but a KVS site served under a
    language prefix puts that prefix in front of every path of its own (site16.example:
    `/de/search/{slug}/` beside `/de/videos/…`), so it is configurable like `video_path` and
    `model_path`.

    Theme options, all off by default:
    - `card_selector`: the anchors that count as video cards (default: any link in a list block).
    - `screenshots_only`: card images outside `/contents/videos_screenshots/` yield no media.
    - `screenshot_selector`: `<img>` elements listing the video's numbered timeline frames on the
      video page (a screenshots tab); only images under `/contents/videos_screenshots/` count.
    - `performers_extra_key`: also store the performer names in `PageData.extra` under this key.
    - `poster_from_screenshot`: this theme's metadata poster is a template artefact — use the
      video's first numbered screenshot instead (see `_screenshot_poster`).
    """

    domain: str
    host: str = ""
    video_path: str = r"^/video/\d+/[^/]+/?$"
    model_path: str = ""  # e.g. "/models/{slug}/"; empty when the site has no model pages
    search_path: str = "/search/{slug}/"
    tag_selector: str = ""
    category_selector: str = ""
    performer_selector: str = ""
    card_selector: str = _CARD_ANCHORS
    screenshots_only: bool = False
    screenshot_selector: str = ""
    performers_extra_key: str = ""
    poster_from_screenshot: bool = False


class KvsAdapter:
    def __init__(self, site: KvsSite) -> None:
        self.config = site
        self.domain = site.domain
        self._video_path = re.compile(site.video_path)

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        names = dedupe(
            normalize_name(name) for creator in creators for name in creator_names(creator)
        )
        tasks: list[TaskSpec] = []
        for name in names:
            tasks.extend(self.search_tasks(site, name))
            if self.config.model_path and slugify(name):
                path = self.config.model_path.format(slug=slugify(name))
                tasks.append(TaskSpec(self._url(site, path), CrawlTask.Kind.LISTING, SEED_PRIORITY))
        return list({task.url: task for task in tasks}.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        """`search_path` for a name or free text; empty when the text has no ASCII words."""
        slug = slugify(query)
        if not slug:
            return []
        url = self._url(site, self.config.search_path.format(slug=slug))
        return [TaskSpec(url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)]

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """None: every seed of this site is a creator or search page (issue #71)."""
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return self._parse_video(url, tree)
        return self._parse_listing(kind, url, tree)

    def _url(self, site: Site, path: str) -> str:
        return f"https://{self.config.host or site.domain}{path}"

    def _parse_listing(self, kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
        first_page = _page_number(url) < 2
        media_by_url: dict[str, tuple[MediaSpec, ...]] = {}
        for anchor in tree.css(self.config.card_selector):
            href = absolute_url(url, anchor.attributes.get("href"))
            if href is None or not self._video_path.match(urlsplit(href).path):
                continue
            # Cards link the video twice (image and title); keep the first image seen.
            if not media_by_url.get(href):
                media_by_url[href] = _card_media(
                    url, anchor, screenshots_only=self.config.screenshots_only
                )
        if not media_by_url:
            return ParseResult()
        links = [
            TaskSpec(href, CrawlTask.Kind.DETAIL, detail_priority(i, first_page=first_page), media)
            for i, (href, media) in enumerate(media_by_url.items())
        ]
        anchors = tree.css(_PAGINATION_ANCHORS)
        pages = dedupe(_pagination_url(url, anchor) or "" for anchor in anchors)
        links.extend(TaskSpec(page, kind, PAGINATION_PRIORITY) for page in pages if page != url)
        return ParseResult(links=tuple(links))

    def _parse_video(self, url: str, tree: LexborHTMLParser) -> ParseResult:
        video = first_json_ld(tree, "VideoObject")
        title = str_field(video, "name") or meta_content(tree, "og:title")
        if not title:
            return ParseResult()

        tags = _link_texts(tree, self.config.tag_selector)
        categories = _link_texts(tree, self.config.category_selector)
        duration_meta = meta_content(tree, "video:duration")
        duration_s = (
            int(duration_meta)
            if duration_meta.isdigit()
            else parse_iso_duration(str_field(video, "duration"))
        )
        poster = absolute_url(
            url, str_field(video, "thumbnailUrl") or meta_content(tree, "og:image")
        )
        if poster and self.config.poster_from_screenshot:
            poster = _screenshot_poster(tree, poster) or poster
        performers = _link_texts(tree, self.config.performer_selector)
        extra: dict[str, object] = {"tags": tags, "categories": categories}
        if self.config.performers_extra_key:
            extra[self.config.performers_extra_key] = performers
        page = PageData(
            url=canonical_url(tree, url) or absolute_url(url, meta_content(tree, "og:url")) or url,
            title=title,
            tags=tuple(dedupe([*tags, *categories])),
            performers=tuple(performers),
            uploaded_at=parse_datetime(meta_content(tree, "video:release_date"))
            or parse_datetime(str_field(video, "uploadDate")),
            duration_s=duration_s,
            extra=extra,
        )
        media = [MediaSpec(Media.Kind.POSTER, poster)] if poster else []
        if self.config.screenshot_selector:
            frames = dedupe(_screenshot_frames(url, tree, self.config.screenshot_selector))
            media.extend(
                MediaSpec(Media.Kind.FRAME, frame)
                for frame in frames[:MAX_SCREENSHOT_FRAMES]
                if frame != poster
            )
        return ParseResult(page=page, media=tuple(media))


def _image_src(base: str, image: LexborNode) -> str | None:
    candidates = (absolute_url(base, image.attributes.get(key)) for key in _IMAGE_ATTRIBUTES)
    return next((candidate for candidate in candidates if candidate), None)


def _screenshot_poster(tree: LexborHTMLParser, poster: str) -> str | None:
    """The video's own first numbered screenshot, in place of a metadata poster that is a guess.

    A KVS theme fills og:image and the JSON-LD `thumbnailUrl` from a template, whether or not the
    file was ever generated: on site17.example `…/{video}/preview.jpg` answers 404 for everything
    but the newest part of the catalogue (2026-09-18: about three quarters of the fetched posters, ~40 % of the
    site's daily media budget), while the numbered screenshots the cards use exist for the whole
    range. The size directory is read off the page's other cards rather than assumed, so a theme
    that serves another size stays right. Returns None when the poster is not a screenshot-path
    poster or the page shows no numbered screenshot — the caller then keeps the metadata poster.
    """
    match = _METADATA_POSTER.match(poster)
    if match is None:
        return None
    for image in tree.css("img"):
        for key in _IMAGE_ATTRIBUTES:
            size = _SCREENSHOT_SIZE.search(image.attributes.get(key) or "")
            if size is not None:
                return f"{match['dir']}/{size['size']}/1.jpg"
    return None


def _screenshot_frames(base: str, tree: LexborHTMLParser, selector: str) -> list[str]:
    """Plain timeline frame images; their `<a>` wrappers are tokenized downloads, never used."""
    sources = (_image_src(base, image) for image in tree.css(selector))
    return [src for src in sources if src and _SCREENSHOT_PATH in urlsplit(src).path]


def _card_media(
    base: str, anchor: LexborNode, *, screenshots_only: bool = False
) -> tuple[MediaSpec, ...]:
    """The card image: poster (`preview.jpg`), or its timeline frame(s) including rotator frames."""
    image = anchor.css_first("img")
    if image is None:
        return ()
    src = _image_src(base, image)
    if src is None or (screenshots_only and _SCREENSHOT_PATH not in urlsplit(src).path):
        return ()
    if urlsplit(src).path.endswith("/preview.jpg"):
        return (MediaSpec(Media.Kind.POSTER, src),)
    count = image.attributes.get("data-cnt") or ""
    numbered = _NUMBERED_FRAME.search(src)
    if numbered is None or not count.isdigit() or int(count) < 2:
        return (MediaSpec(Media.Kind.FRAME, src),)
    prefix = src[: numbered.start(1)]
    frames = range(1, min(int(count), MAX_CARD_FRAMES) + 1)
    return tuple(MediaSpec(Media.Kind.FRAME, f"{prefix}{n}.jpg") for n in frames)


def _pagination_url(base: str, anchor: LexborNode) -> str | None:
    """A pagination link as a plain page URL: its href, or `?from_videos=N` for AJAX links."""
    href = absolute_url(base, anchor.attributes.get("href"))
    if href is not None:
        return None if urlsplit(href).path.startswith(_EXCLUDED_PATH_PREFIXES) else href
    match = _PAGE_PARAMETER.search(anchor.attributes.get("data-parameters") or "")
    if match is None:
        return None
    parts = urlsplit(base)
    key = match.group("keys").split("+")[0]
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != key]
    if int(match.group("page")) > 1:
        query.append((key, match.group("page")))
    return parts._replace(query=urlencode(query), fragment="").geturl()


def _page_number(url: str) -> int:
    """Page number from `?from_videos=2` or KVS paths such as `/latest-updates/2/`; 1 if absent."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    for key in _PAGE_QUERY_KEYS:
        if params.get(key, "").isdigit():
            return int(params[key])
    match = _PAGE_NUMBER_PATH.search(parts.path)
    return int(match.group(1)) if match else 1


def _link_texts(tree: LexborHTMLParser, selector: str) -> list[str]:
    return dedupe(clean_text(node.text()) for node in tree.css(selector)) if selector else []


# Sites on the template.
SITE19 = KvsSite(
    domain="site19.example",
    model_path="/models/{slug}/",
    tag_selector='ul.vx-list-tags a[href*="/tags/"]',
    category_selector='ul.vx-list-tags a[href*="/categories/"]',
    performer_selector='ul.vx-list-thumb a[href*="/models/"]',
)
SITE18 = KvsSite(
    domain="site18.example",
    host="www.site18.example",
    model_path="/models/{slug}/",
    tag_selector='div.top-options a[href*="/tags/"]',
    category_selector='div.top-options a[href*="/categories/"]',
    performer_selector='div.models-list a[href*="/models/"]',
)
SITE22 = KvsSite(
    domain="site22.example",
    host="www.site22.example",
    video_path=r"^/videos/\d+/[^/]+/?$",
    tag_selector='div.tags-list a[href*="/t/"]',
    category_selector='div.tags-list a[href*="/ct/"]',
)
SITE20 = KvsSite(
    domain="site20.example",
    model_path="/models/{slug}/",
    tag_selector='div.media-info__lists-row a[href*="/tags/"]',
    category_selector='div.media-info__lists-row a[href*="/categories/"]',
    performer_selector='div.media-info__lists-row a[href*="/models/"]',
)
SITE17 = KvsSite(
    domain="site17.example",
    host="site17.example",
    model_path="/models/{slug}/",
    tag_selector='div.top-options a[href*="/tags/"]',
    category_selector='div.top-options a[href*="/categories/"]',
    performer_selector='div.top-options a[href*="/models/"]',
    # Its video pages advertise `…/preview.jpg`, which the site only serves for the newest part of
    # its catalogue; the numbered screenshots exist throughout (`_screenshot_poster`).
    poster_from_screenshot=True,
)
# site21.example paginates by path (`/search/{slug}/2/`); `?from_videos=N` silently serves page 1.
# Its only `/models/` link on a video page reads "View Model", so there is no performer selector.
SITE21 = KvsSite(
    domain="site21.example",
    video_path=r"^/videos/\d+/[^/]+/?$",
    model_path="/models/{slug}/",
    tag_selector='div.box-list a[href*="/tags/"]',
    category_selector='div.box-list a[href*="/categories/"]',
)
# site08.example lists each video's timeline frames in a screenshots tab. Its model slugs carry an
# unpredictable suffix (`/models/lena-x7/` for "Lena"), so there are no model seeds.
SITE08 = KvsSite(
    domain="site08.example",
    video_path=r"^/videos/\d+/[^/]+/?$",
    tag_selector='div.info a[href*="/tags/"]',
    category_selector='div.info a[href*="/categories/"]',
    performer_selector='div.info a[href*="/models/"]',
    card_selector='[id^="list_videos_"] div.item a[href]',
    screenshots_only=True,
    screenshot_selector="div.block-screenshots img",
    performers_extra_key="models",
)
# site16.example serves one KVS install under a language prefix (`/de/`, `/en/`, `/fr/`, …;
# the apex serves the original language). Every path of the site carries it, so seeds, video pages
# and model pages are all `/de/…` and a crawl stays inside the German skin: the cards of a `/de/`
# page link `/de/` pages, and the other languages are reached only through `<link rel=alternate>`
# elements, which no adapter follows. Its models are `/de/actor/{slug}/`; the video page's info
# block carries categories and models (its "Tags:" line is plain text without links, and the
# category names cover the same ground). The metadata poster is `…/{video}/preview.jpg`, which
# nothing on the site itself ever requests — every card and the screenshots tab use the numbered
# screenshots — so the poster is taken from those instead of spending a media request on a file
# that may not exist (as on site17.example).
SITE16 = KvsSite(
    domain="site16.example",
    host="www.site16.example",
    video_path=r"^/de/videos/\d+/[^/]+/?$",
    model_path="/de/actor/{slug}/",
    search_path="/de/search/{slug}/",
    category_selector='div.item a[href*="/kategorien/"]',
    performer_selector='div.item a[href*="/actor/"]',
    screenshot_selector="div.block-screenshots img",
    poster_from_screenshot=True,
)
KVS_SITES = (SITE19, SITE18, SITE22, SITE20, SITE17, SITE21, SITE16)
