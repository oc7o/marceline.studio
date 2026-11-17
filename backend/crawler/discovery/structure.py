"""Three more counters read off the homepage the probe already fetched (issue #118).

`probe_candidates` spends at most three requests per candidate and reads everything it knows about
a domain out of that one homepage (`crawler/discovery/probe.py`). `video_links_seen` answers "is
this a tube?"; independent creators' content is republished in places that are not tubes
(`docs/research/creator-leak-sources.md`), and telling those apart needs three more questions the
same HTML already answers:

- **profile links** — same-site links into a *person's own page* (`/model/<name>`, `/creator/…`).
  A site organised around names rather than around uploads is the shape that matters most for an
  independent creator, and it is also the shape whose seed unit is a creator page.
- **forum links** — same-site links into a thread or a board. A forum is not coverable under our
  own rules (its threads sit behind registration), so recognising one early keeps it out of the
  onboarding queue instead of costing a human a review round.
- **gallery images** — the largest family of same-template images on the page. A post that is an
  image set rather than a video is what most creator leaks look like, and the media lane hashes
  stills already (`index.Media.Kind.PHOTO`, ADR 0020).

Pure, like `video_links.py`: HTML and the page's own URL in, counts out. **No extra request**, and
nothing here is stored but the three integers — never a URL, a name or a title. The vocabularies
are ours (`crawler.drafting.MODEL_SEGMENTS` for the performer index, the forum words below), never
read off the page, and they are coarse on purpose: this is a probe's first guess about a domain
that has no adapter, not an adapter.
"""

from collections import defaultdict
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser

from core.url_shapes import path_pattern
from crawler.drafting import MODEL_SEGMENTS
from crawler.sites.common import absolute_url

#: Path segments that name a thread, a board or a forum section, in the grammars the common forum
#: engines use (`/threads/…`, `/topic/…`, `/viewtopic.php`, `/showthread.php`, `/board/…`).
FORUM_SEGMENTS: frozenset[str] = frozenset(
    {
        "forum",
        "forums",
        "foren",
        "board",
        "boards",
        "thread",
        "threads",
        "topic",
        "topics",
        "viewtopic.php",
        "showthread.php",
        "viewforum.php",
        "forumdisplay.php",
    }
)
#: Query parameters the same engines use when the thread id is not in the path.
FORUM_PARAMETERS: frozenset[str] = frozenset({"t", "topic", "tid", "threadid", "f", "forumid"})
#: Attributes a lazy-loading gallery hides its real image URL in, in the order we prefer them.
IMAGE_ATTRIBUTES: tuple[str, ...] = ("data-original", "data-src", "data-lazy", "data-thumb", "src")
IMAGE_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _on_site_urls(
    tree: LexborHTMLParser, base_url: str, selector: str, attributes: tuple[str, ...]
) -> list[str]:
    """Absolute URLs from `selector`'s `attributes` that stay on the page's own host."""
    host = (urlsplit(base_url).hostname or "").removeprefix("www.")
    found: list[str] = []
    for node in tree.css(selector):
        for attribute in attributes:
            value = node.attributes.get(attribute)
            if not value:
                continue
            url = absolute_url(base_url, value)
            if url is None:
                break
            if (urlsplit(url).hostname or "").removeprefix("www.") == host:
                found.append(url)
            break
    return found


def profile_paths(html: str, base_url: str) -> frozenset[str]:
    """Distinct on-site paths that read as one person's own page.

    A performer-index word followed by something that is not a page number: `/model/<name>` counts,
    `/models` and `/models/2` do not — the index itself is not a profile.
    """
    tree = LexborHTMLParser(html)
    paths: set[str] = set()
    for url in _on_site_urls(tree, base_url, "a[href]", ("href",)):
        segments = _segments(urlsplit(url).path)
        for index, segment in enumerate(segments[:-1]):
            tail = segments[index + 1]
            if segment.lower() in MODEL_SEGMENTS and not tail.isdigit():
                paths.add(urlsplit(url).path)
                break
    return frozenset(paths)


def forum_paths(html: str, base_url: str) -> frozenset[str]:
    """Distinct on-site paths that read as a thread or a board."""
    tree = LexborHTMLParser(html)
    paths: set[str] = set()
    for url in _on_site_urls(tree, base_url, "a[href]", ("href",)):
        parts = urlsplit(url)
        segments = _segments(parts.path)
        names = {segment.lower() for segment in segments}
        query = {key.lower() for key in parse_qs(parts.query)}
        if names & FORUM_SEGMENTS or (segments and query & FORUM_PARAMETERS):
            paths.add(parts.path + (f"?{parts.query}" if parts.query else ""))
    return frozenset(paths)


def gallery_image_count(html: str, base_url: str) -> int:
    """Size of the largest family of same-shaped on-site images, `<img>` and image links alike.

    One family, not the total: a theme's sprites, avatars and icons are each their own shape, and
    what says "this page is an image set" is many pictures that look like each other.
    """
    tree = LexborHTMLParser(html)
    urls = set(_on_site_urls(tree, base_url, "img", IMAGE_ATTRIBUTES))
    urls |= {
        url
        for url in _on_site_urls(tree, base_url, "a[href]", ("href",))
        if urlsplit(url).path.lower().endswith(IMAGE_SUFFIXES)
    }
    families: dict[str, set[str]] = defaultdict(set)
    for url in urls:
        families[path_pattern(url)].add(url)
    return max((len(paths) for paths in families.values()), default=0)
