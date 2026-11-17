"""Recognise likely per-video detail links on a probed homepage (issue #23).

`probe_candidates` spends at most three requests per candidate (robots.txt, a redirect's robots.txt,
the homepage) and counts `video_links_seen` from whatever the homepage fetch already brought back —
no further request. The original heuristic matched only paths containing `/video/`, `/videos/`,
`/watch/` or `/watch?` verbatim, so it missed URL grammars several of our own site adapters answer
with today (`crawler/sites/*.py`, `grep -n 'video_path\\|_VIDEO_PATH'`): `.html` detail pages
carrying an id (`site02.example`, `/hdvideo/482-slug.html`), an id glued onto the end of an otherwise
wordy slug with no separating keyword (`site07.example`, `/some-title-v30343`), a bare hex id under a
one-word path (`site03.example`, `/post/<hex>`). Two of the sixteen candidates the operator reviewed
for issue #23 reproduce the same gap (id 38 recommended for onboarding anyway on other evidence,
id 117 left `unclear` for exactly this reason).

None of this is a second guess-list: every piece reuses something already decided elsewhere.
`core.url_shapes.looks_like_id` is the id test ADR 0041's dead-shape breaker already trusts;
`crawler.drafting.MIN_FAMILY`/`MODEL_SEGMENTS` are the same thresholds the offline adapter-drafting
tool uses to tell a link family from a performer index. Two signals, unioned over the same anchor
list:

1. **Anchored: a keyword, a `.html` file, or a short type-code, together with an id.** A path
   segment named after a video (`_VIDEO_WORDS` — the nouns our own adapters' detail-path regexes
   use, plus the same web-wide synonyms for "one video's own page") or a `.html`/`.htm` page,
   together with an id-looking segment (whole, or a token of a hyphen/underscore-compounded
   segment — `/hdvideo/482-this_part.html` reads `482` out of the compound) anywhere in the path; a
   language prefix (`/de/videos/…`) changes nothing, it is just one more literal segment ahead of
   both. A short letter code glued straight onto a number (`v30343`) is distinctive enough that it
   counts on its own, with no keyword needed. One such link is already a signal — a thin homepage
   with only a couple of teaser links still needs that.
2. **Weak: an id-looking segment on its own, corroborated by a same-shaped family.** A *whole*
   segment that reads as an id by itself, with nothing anchoring it, is corroborated only by
   `crawler.drafting.MIN_FAMILY` (3) other links reduced to the same shape
   (`core.url_shapes.path_pattern`) — a bare, short digit run (`/hdvideo/2`, `/hdvideo/3`, …) reads
   exactly like site02's own pagination, never its video shape, so it needs a longer run
   (`_BARE_DIGIT_MIN`) to count even as weak evidence, and a compounded token skips straight to
   signal 1 instead (the compounding itself already rules out a page counter).

Both signals skip a path that carries a non-video word at any position — pagination, category,
tag, search, the performer index (`MODEL_SEGMENTS`), or a handful of common non-video content
sections (blog, forum, gallery, …: a real adapter fixture's own footer links landed one of these
in a family of over a hundred during development, which is exactly the "conservative against a
blog" this module has to hold). Pure: HTML and the page's own URL in, a set of distinct on-site
paths out (the probe stores `len(...)`); no request, and no per-adapter regex duplicated here — a
site that gets an actual adapter keeps its own exact pattern (`crawler/sites/<site>.py`); this is
only the probe's coarse recognizer for a domain that has none yet.
"""

import re
from collections import defaultdict
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from core.url_shapes import looks_like_id, path_pattern
from crawler.drafting import MIN_FAMILY, MODEL_SEGMENTS
from crawler.sites.common import absolute_url

#: Nouns our own adapters' detail-path regexes use (video, videos, watch, vid, post) plus the same
#: web-wide synonyms for "one video's own page" (v, view, embed, clip, movie, scene), so a site
#: with none of our adapters is not missed for using a different one of them.
_VIDEO_WORDS = frozenset(
    {
        "video",
        "videos",
        "v",
        "view",
        "views",
        "watch",
        "vid",
        "vids",
        "embed",
        "embeds",
        "clip",
        "clips",
        "movie",
        "movies",
        "scene",
        "scenes",
        "post",
        "posts",
    }
)
#: Segments that name something other than one video: pagination, category/tag, search, the
#: performer index (`crawler.drafting.MODEL_SEGMENTS` — a profile page is not itself a video link),
#: and the handful of other content sections a site with a "detail page" shape just as often has.
_NON_VIDEO_WORDS = (
    frozenset(
        {
            "page",
            "seite",
            "pg",
            "category",
            "categories",
            "kategorie",
            "kategorien",
            "tag",
            "tags",
            "tagged",
            "search",
            "suche",
            "blog",
            "blogs",
            "article",
            "articles",
            "news",
            "forum",
            "forums",
            "thread",
            "threads",
            "comment",
            "comments",
            "photo",
            "photos",
            "image",
            "images",
            "gallery",
            "galleries",
            "story",
            "stories",
            "download",
            "downloads",
            "file",
            "files",
            "attachment",
            "attachments",
        }
    )
    | MODEL_SEGMENTS
)
#: A page counter is a short run of digits; a real id, bare or not, is essentially never this
#: short. Guards the un-anchored, whole-segment id test only (signal 2) — a digit run compounded
#: with anything else (signal 1) is not ambiguous with pagination at any length.
_BARE_DIGIT_MIN = 4
#: A short letter code glued straight onto a number of ordinary id length (site07: `-v30343`).
_SHORT_CODE_ID = re.compile(r"\A[a-z]{1,3}\d{4,}\Z", re.IGNORECASE)
_DELIMITERS = re.compile(r"[._-]")


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _stem(segment: str) -> str:
    """The segment without its file extension, if it has a short, alphanumeric one."""
    base, dot, ext = segment.rpartition(".")
    return base if dot and 1 <= len(ext) <= 5 and ext.isalnum() else segment


def _tokens(segment: str) -> list[str]:
    return [token for token in _DELIMITERS.split(_stem(segment)) if token]


def _whole_segment_id(segment: str) -> bool:
    """Exactly what `core.url_shapes.path_pattern` stars, plus the pagination guard above."""
    stem = _stem(segment)
    if stem.isdigit():
        return len(stem) >= _BARE_DIGIT_MIN
    return looks_like_id(stem)


def _has_shortcode_token(segments: list[str]) -> bool:
    return any(_SHORT_CODE_ID.match(token) for segment in segments for token in _tokens(segment))


def _has_compound_id_token(segments: list[str]) -> bool:
    """A plain id token (digits, hex, UUID, alnum) inside a segment compounded with something
    else — the compounding is what rules out a page counter, so any length counts; still needs a
    keyword or `.html` anchor alongside it (unlike a short-code token) since compounding alone
    does not rule out, say, a numbered blog post under a word we do not recognise.
    """
    return any(
        len(tokens) > 1 and any(looks_like_id(token) for token in tokens)
        for tokens in (_tokens(segment) for segment in segments)
    )


def _is_html_detail(segments: list[str]) -> bool:
    return bool(segments) and segments[-1].lower().endswith((".html", ".htm"))


def _has_keyword(segments: list[str]) -> bool:
    return any(segment.lower() in _VIDEO_WORDS for segment in segments)


def video_detail_paths(html: str, base_url: str) -> frozenset[str]:
    """Distinct on-site paths on this page that look like one video's own page.

    `base_url` is the URL the HTML was fetched from (for resolving relative links and matching the
    site's own host); it is never fetched again here.
    """
    tree = LexborHTMLParser(html)
    host = (urlsplit(base_url).hostname or "").removeprefix("www.")
    anchored: set[str] = set()
    weak: set[str] = set()
    for node in tree.css("a[href]"):
        url = absolute_url(base_url, node.attributes.get("href"))
        if url is None:
            continue
        parts = urlsplit(url)
        if (parts.hostname or "").removeprefix("www.") != host:
            continue
        path = parts.path or "/"
        segments = _segments(path)
        if not segments or any(segment.lower() in _NON_VIDEO_WORDS for segment in segments):
            continue
        has_whole_id = any(_whole_segment_id(segment) for segment in segments)
        is_anchored = _has_shortcode_token(segments) or (
            (has_whole_id or _has_compound_id_token(segments))
            and (_is_html_detail(segments) or _has_keyword(segments))
        )
        if is_anchored:
            anchored.add(path)
        elif has_whole_id:
            weak.add(path)

    by_shape: dict[str, set[str]] = defaultdict(set)
    for path in weak:
        by_shape[path_pattern(path)].add(path)
    family_hits = {
        path for paths in by_shape.values() if len(paths) >= MIN_FAMILY for path in paths
    }
    return frozenset(anchored | family_hits)
