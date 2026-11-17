"""The shape of a URL: its host and its path with identifier-looking segments starred.

One heuristic, two consumers, so they cannot drift apart:

- capture refusals (ADR 0016, issue #78 amendment) store the *shape* of the paths a browser visit
  refused, so "which host blocked us, on what" can be answered without keeping a URL;
- the dead-shape breaker (ADR 0041, issue #82) counts a site's 404 answers per shape, so a URL
  template a site advertises but does not serve stops being fetched one page at a time.

Each `/`-separated segment that looks like an identifier — all digits, 6+ hex characters, a 12+
character alphanumeric run mixing letters and digits (a slug or a token), or a canonical UUID — is
replaced with `*`; a segment's file extension is kept, since it describes the resource, not who is
asking for it. So `/embed/482910` becomes `/embed/*`, `/get_file/9f1c2b7a91e0.mp4` becomes
`/get_file/*.mp4`, and `/embed/` or `/watch` pass through unchanged. Query strings and fragments
are dropped outright — that is where a token or session id usually lives, and the shape does not
need them.

The UUID clause came from the media side (ADR 0041 § 1): CDNs key thumbnails by UUID, and without
it one CDN's 290 failed requests in a day spread over 261 shapes seen exactly once each — counters
that can never accumulate evidence. Hyphens keep a UUID out of the alphanumeric-run rule, so it
needs naming on its own.

This is a heuristic, not a guarantee: a short, literal-looking segment that happens to be an id is
kept verbatim, so a stored shape should be read as "shaped, not guaranteed id-free" rather than
trusted blindly.
"""

import re
from urllib.parse import urlsplit

__all__ = ["host_of", "looks_like_id", "path_pattern", "url_shape"]

_UUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def looks_like_id(segment: str) -> bool:
    """Whether this path segment (without its extension) identifies one thing rather than a kind."""
    if not segment:
        return False
    if segment.isdigit():
        return True
    if len(segment) >= 6 and all(ch in "0123456789abcdefABCDEF" for ch in segment):
        return True
    if _UUID.match(segment):
        return True
    return len(segment) >= 12 and segment.isalnum() and any(ch.isdigit() for ch in segment)


def _shape_segment(segment: str) -> str:
    if not segment:
        return segment
    base, dot, ext = segment.rpartition(".")
    has_extension = dot and 1 <= len(ext) <= 5 and ext.isalnum()
    stem = base if has_extension else segment
    suffix = f".{ext}" if has_extension else ""
    return f"*{suffix}" if looks_like_id(stem) else segment


def path_pattern(url: str) -> str:
    """Reduce a URL's path to a shape safe to store: see the module docstring for the rule."""
    path = urlsplit(url).path or "/"
    return "/".join(_shape_segment(segment) for segment in path.split("/"))


def url_shape(url: str) -> str:
    """Host plus path pattern: what the breaker of ADR 0041 counts answers against."""
    return f"{host_of(url)}{path_pattern(url)}"
