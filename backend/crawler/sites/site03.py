"""site03.example adapter (custom PHP).

- Seeds: the search page `/{Name-With-Dashes}.html` per creator name and alias; free-text search
  seeds (`search_tasks`, e.g. video titles) use the same `/{Words-With-Dashes}.html` page.
- Listing/search pages: post links (each with its card thumbnail and hover preview clip) and the
  `rel=next` page (`/{Name}?page=30`, offsets in steps of 30).
- Post pages: schema.org microdata (`itemprop` name, uploadDate, duration, thumbnailUrl), the post
  headline for performers (`a.ps_link`), hashtags and studio. Outbound hoster/torrent links are
  kept in `extra["external_links"]` for later discovery but never enqueued. Post pages yield no
  further links in v0; the related-post cards on them are ignored.
- Credits are noisy (issue #3): the site links every title word it knows as a name, so a post
  without real names credits "Kit" and "Echo", and "Dana Forde" becomes "Dana". `classify_credits`
  drops noise words, one-word fragments of another credit and one-word fragments of a longer name
  in the title (`extra["dropped_performers"]`, with the reason), and flags compilation and
  multi-scene posts (`extra["compilation"]`), which then link no creator
  (`crawler.services.creator_links`). Thresholds: `MANY_PERFORMERS`, `COMPILATION_MIN_PERFORMERS`,
  `LONG_POST_S` / `LONG_POST_MIN_PERFORMERS`. Pages stay indexed either way.

Media URLs carry a per-asset token and a 10-digit hour timestamp segment
(`/tkn/{x}/{y}/vid/{token}/{ts}/{uploader}/{post}/full.jpg`; listing card posters
`/tkn/{x}/{y}/img/{token}/{ts}/{a}/{b}/0.webp`), so they must be fetched soon after
the page. The site asks for `Crawl-delay: 10` in robots.txt, which the worker enforces.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote_plus, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from catalog.models import Creator, Site
from crawler.models import CrawlTask
from crawler.sites.base import (
    COMPILATION_KEY,
    DROPPED_PERFORMERS_KEY,
    PAGINATION_PRIORITY,
    SEED_PRIORITY,
    MediaSpec,
    PageData,
    ParseResult,
    TaskSpec,
    detail_priority,
    dropped_performer_names,
)
from crawler.sites.common import (
    absolute_url,
    ascii_fold,
    clean_text,
    creator_names,
    dedupe,
    meta_content,
    normalize_name,
    parse_datetime,
    parse_iso_duration,
)
from index.models import Media

_POST_PATH = re.compile(r"^/post/[0-9a-f]+$")
_HASHTAG = re.compile(r"(?<!\S)#\S+")
_URL_IN_TEXT = re.compile(r"https?://[^\s{}<>\"']+")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_TITLE_SUFFIX = " on Site03"

# Words the site credits as performers that are no names: promotional title text and ordinary
# words its name matcher picked up (dev DB, 2026-09-17: one-word credits of 2,777 posts). Normalized
# (`normalize_name`). A credit made only of these is dropped as `noise_word`.
NOISE_WORDS = frozenset(
    {
        # promotional title text ("FULL HD HERE:", "Watch/Download:", "NEW", "SCENE 1 ->")
        "full", "hd", "here", "new", "watch", "download", "scene", "premium", "uncut", "exclusive",
        "compilation", "cast", "starring", "video", "videos", "leak",
        "leaked", "free", "hot", "telegram", "link", "links", "support", "like",
        "more", "deleted", "content", "hq", "1080p", "4k",
        # ordinary words credited as names
        "mean", "butter", "echo", "apple", "blush", "bronze", "bubbles", "cheer", "era", "fancy",
        "para", "ver", "moi", "amiga",
    }
)  # fmt: skip
# Title-case words that may stand next to a one-word credit without making it part of a longer
# name ("Kira Rowan And Nina", "Kira x Nina").
CONNECTOR_WORDS = frozenset(
    {"a", "amp", "an", "and", "at", "by", "feat", "for", "from", "ft", "gets", "her", "his", "in",
     "is", "my", "of", "on", "the", "to", "vs", "with", "x"}
)  # fmt: skip
# A post crediting this many performers (after dropping noise) is a compilation or a multi-scene
# movie whatever its title says. Real scenes credit 1-4; 42 of 2,777 posts credited 8 or more.
MANY_PERFORMERS = 6
# Fewer performers with a compilation word in the title ("Compilation", "Best of"). A
# one-performer compilation stays linked: it is about that performer.
COMPILATION_MIN_PERFORMERS = 2
_COMPILATION_WORDS = re.compile(
    r"(?<![a-z0-9])(?:compilations?|best of|supercut)(?![a-z0-9])", re.IGNORECASE
)
# A long post with several performers is a full movie of several scenes.
LONG_POST_S = 90 * 60
LONG_POST_MIN_PERFORMERS = 3
_WORD_BEFORE = re.compile(r"(?<![\w'\u2019])(\w[\w'\u2019]*)[ \t]+$")
_WORD_AFTER = re.compile(r"^[ \t]+(\w[\w'\u2019]*)")


class Site03Adapter:
    domain = "site03.example"

    def seed_tasks(self, site: Site, creators: Sequence[Creator]) -> list[TaskSpec]:
        tasks: dict[str, TaskSpec] = {}
        for creator in creators:
            for name in creator_names(creator):
                task = _search_task(site, name)
                if task:
                    tasks.setdefault(task.url.lower(), task)
        return list(tasks.values())

    def search_tasks(self, site: Site, query: str) -> list[TaskSpec]:
        task = _search_task(site, query)
        return [task] if task else []

    def listing_tasks(self, site: Site) -> list[TaskSpec]:
        """None: every seed of this site is a creator or search page (issue #71)."""
        return []

    def parse(self, kind: CrawlTask.Kind, url: str, html: str) -> ParseResult:
        tree = LexborHTMLParser(html)
        if kind == CrawlTask.Kind.DETAIL:
            return _parse_post(url, tree)
        return _parse_listing(kind, url, tree)


def _search_slug(name: str) -> str:
    """Search slug keeping case, e.g. `Eléna Märsh` -> `Elena-Marsh` (the site's own format)."""
    return "-".join(part for part in _NON_ALNUM.split(ascii_fold(name)) if part)


def _search_task(site: Site, text: str) -> TaskSpec | None:
    """`/{Words-With-Dashes}.html` for a creator name or free text; None without ASCII words."""
    slug = _search_slug(text)
    if not slug:
        return None
    return TaskSpec(f"https://{site.domain}/{slug}.html", CrawlTask.Kind.SEARCH, SEED_PRIORITY)


def _post_url(url: str | None) -> str | None:
    """Post URL without tracking query (`?sk=...&so=0`); None for non-post URLs."""
    if url is None:
        return None
    parts = urlsplit(url)
    if not _POST_PATH.match(parts.path):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _parse_listing(kind: CrawlTask.Kind, url: str, tree: LexborHTMLParser) -> ParseResult:
    first_page = _page_offset(url) == 0
    links: list[TaskSpec] = []
    for card in tree.css("div.post_el_small"):
        anchor = card.css_first('a.post_time[href^="/post/"]')
        href = _post_url(absolute_url(url, anchor.attributes.get("href"))) if anchor else None
        if href is None:
            continue
        priority = detail_priority(len(links), first_page=first_page)
        links.append(TaskSpec(href, CrawlTask.Kind.DETAIL, priority, _card_media(url, card)))
    next_page = _next_page(url, tree)
    if next_page:
        links.append(TaskSpec(next_page, kind, PAGINATION_PRIORITY))
    return ParseResult(links=tuple(links))


def _page_offset(url: str) -> int:
    """Result offset from `?page=30` (steps of 30); 0 on the first page."""
    value = parse_qs(urlsplit(url).query).get("page", [""])[0]
    return int(value) if value.isdigit() else 0


def _card_media(base: str, card: LexborNode) -> tuple[MediaSpec, ...]:
    media: list[MediaSpec] = []
    image = card.css_first("img.mini_post_vid_thumb")
    thumb = absolute_url(base, image.attributes.get("data-src")) if image else None
    if thumb:
        media.append(MediaSpec(Media.Kind.POSTER, thumb))
    video = card.css_first("video.hvp_player")
    preview = absolute_url(base, video.attributes.get("src")) if video else None
    if preview:
        media.append(MediaSpec(Media.Kind.PREVIEW, preview))
    return tuple(media)


def _next_page(url: str, tree: LexborHTMLParser) -> str | None:
    node = tree.css_first('link[rel="next"]')
    href = absolute_url(url, node.attributes.get("href")) if node else None
    return href if href and href != url else None


def _parse_post(url: str, tree: LexborHTMLParser) -> ParseResult:
    message = tree.css_first("span.page_message")
    title = tree.css_first("title")
    if (message and clean_text(message.text()) == "Post Not Found") or (
        title and clean_text(title.text()).startswith("Post Not Found")
    ):
        return ParseResult(removed=True)
    container = tree.css_first("#vid_container_id")
    raw_title = _itemprop(container, "name") or meta_content(tree, "og:title").removesuffix(
        _TITLE_SUFFIX
    )
    if not raw_title:
        return ParseResult()

    wrap = tree.css_first("div.post_el_wrap")
    headline = (wrap.css_first("h1") if wrap else None) or tree.css_first("h1")
    duration_s = parse_iso_duration(_itemprop(container, "duration"))
    credits = classify_credits(raw_title, _subkeys(headline, "a.ps_link[data-subkey]"), duration_s)
    studios = _subkeys(headline, "b.sub_cat_s[data-subkey]")
    hashtags = (
        dedupe(clean_text(a.attributes.get("label")) for a in headline.css("a.hash_link[label]"))
        if headline
        else []
    )
    keywords = [clean_text(word) for word in meta_content(tree, "keywords").split(",")]
    poster = absolute_url(url, _itemprop(container, "thumbnailUrl")) or absolute_url(
        url, meta_content(tree, "og:image")
    )

    page = PageData(
        # Fetched URL first: og:url sometimes points at www.site03.example, which would split one post
        # into two Page rows.
        url=_post_url(url) or _post_url(absolute_url(url, meta_content(tree, "og:url"))) or url,
        title=clean_text(_HASHTAG.sub(" ", raw_title)) or raw_title,
        tags=tuple(dedupe([*hashtags, *studios])),
        performers=credits.performers,
        uploaded_at=parse_datetime(_itemprop(container, "uploadDate")),
        duration_s=duration_s,
        extra={
            "raw_title": raw_title,
            "studios": studios,
            "hashtags": hashtags,
            "keywords": dedupe(keywords),
            "external_links": _external_links(url, wrap),
            **credits.extra(),
        },
    )
    media = (MediaSpec(Media.Kind.POSTER, poster),) if poster else ()
    return ParseResult(page=page, media=media)


def _itemprop(container: LexborNode | None, name: str) -> str:
    node = container.css_first(f'meta[itemprop="{name}"]') if container else None
    return clean_text(node.attributes.get("content")) if node else ""


def _subkeys(node: LexborNode | None, selector: str) -> list[str]:
    if node is None:
        return []
    return dedupe(
        clean_text(unquote_plus(n.attributes.get("data-subkey") or "")) for n in node.css(selector)
    )


def _external_links(base: str, wrap: LexborNode | None) -> list[str]:
    """Off-site hoster, shortener and torrent links of the post itself (not related posts)."""
    if wrap is None:
        return []
    site_host = (urlsplit(base).hostname or "").lower().removeprefix("www.")
    candidates: list[str] = []
    for anchor in wrap.css("a.extlink[href], a.mpc_btn[href]"):
        href = absolute_url(base, anchor.attributes.get("href"))
        if href:
            candidates.append(href)
    for textarea in wrap.css("textarea.PostEditTA"):
        candidates.extend(_URL_IN_TEXT.findall(textarea.text(deep=True)))
    return dedupe(
        link
        for link in candidates
        if (urlsplit(link).hostname or "").lower().removeprefix("www.") != site_host
    )


@dataclass(frozen=True)
class Credits:
    """A post's performer credits after noise filtering, and whether it is a compilation."""

    performers: tuple[str, ...]
    dropped: tuple[tuple[str, str], ...] = ()  # (credit, reason)
    compilation: tuple[str, ...] = ()  # reasons; empty when the post is no compilation

    def extra(self) -> dict[str, object]:
        """The credit flags for `PageData.extra` (`crawler.sites.base`); always both keys."""
        return {
            DROPPED_PERFORMERS_KEY: [{"name": name, "reason": why} for name, why in self.dropped],
            COMPILATION_KEY: (
                {"reasons": list(self.compilation), "performer_count": len(self.performers)}
                if self.compilation
                else None
            ),
        }


def classify_credits(raw_title: str, credited: Sequence[str], duration_s: int | None) -> Credits:
    """Split the site's credits into performers and noise, and flag compilations. Pure.

    Also applied to stored posts (`relink_flagged_pages`), whose `raw_extraction` keeps the raw
    title, the duration and, before issue #3, the unfiltered credits.
    """
    title = _HASHTAG.sub(" ", raw_title)
    credits = dedupe(clean_text(name) for name in credited)
    words_of_longer = {
        word for name in credits if len(words := normalize_name(name).split()) > 1 for word in words
    }
    performers: list[str] = []
    dropped: list[tuple[str, str]] = []
    for name in credits:
        words = normalize_name(name).split()
        if not words or all(word in NOISE_WORDS for word in words):
            dropped.append((name, "noise_word"))
        elif len(words) == 1 and words[0] in words_of_longer:
            dropped.append((name, "name_fragment"))
        elif len(words) == 1 and _only_in_longer_names(name, title):
            dropped.append((name, "title_fragment"))
        else:
            performers.append(name)
    return Credits(
        performers=tuple(performers),
        dropped=tuple(dropped),
        compilation=compilation_reasons(raw_title, len(performers), duration_s),
    )


def stored_credits(raw_extraction: Mapping[str, object]) -> Credits | None:
    """`classify_credits` for a stored post (`relink_flagged_pages`); None without a raw title
    (a blocked page keeps no extraction). The credits are the stored performers plus any names an
    earlier classification dropped, so running it again changes nothing."""
    raw_title = raw_extraction.get("raw_title")
    if not isinstance(raw_title, str) or not raw_title:
        return None
    performers = raw_extraction.get("performers")
    credited = (
        [name for name in performers if isinstance(name, str)]
        if isinstance(performers, list)
        else []
    )
    credited.extend(dropped_performer_names(raw_extraction))
    duration = raw_extraction.get("duration_s")
    return classify_credits(raw_title, credited, duration if isinstance(duration, int) else None)


def compilation_reasons(raw_title: str, performers: int, duration_s: int | None) -> tuple[str, ...]:
    """Why a post with `performers` credited performers is a compilation; empty when it is none."""
    reasons: list[str] = []
    if performers >= MANY_PERFORMERS:
        reasons.append("many_performers")
    if performers >= COMPILATION_MIN_PERFORMERS and _COMPILATION_WORDS.search(raw_title):
        reasons.append("compilation_word")
    if (
        duration_s is not None
        and duration_s >= LONG_POST_S
        and performers >= LONG_POST_MIN_PERFORMERS
    ):
        reasons.append("long_multi_performer")
    return tuple(reasons)


def _only_in_longer_names(name: str, title: str) -> bool:
    """True when every whole-word occurrence of the one-word credit `name` in `title` stands next
    to another capitalized word: the site credited part of a longer name ("Dana Forde" -> "Dana",
    "Kit Rowan" -> "Kit"). A credit the title never shows is kept."""
    pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
    occurrences = list(pattern.finditer(title))
    if not occurrences:
        return False
    for match in occurrences:
        before = _WORD_BEFORE.search(title[: match.start()])
        after = _WORD_AFTER.match(title[match.end() :])
        if not (
            (before and _is_name_word(before.group(1))) or (after and _is_name_word(after.group(1)))
        ):
            return False
    return True


def _is_name_word(word: str) -> bool:
    """A capitalized word that could be part of a name: not all caps, no noise, no connector."""
    normalized = normalize_name(word)
    return (
        word[:1].isupper()
        and not (len(word) > 1 and word.isupper())
        and normalized not in NOISE_WORDS
        and normalized not in CONNECTOR_WORDS
    )
