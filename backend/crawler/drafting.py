"""Draft a site adapter from recorded HTML fixtures (ADR 0043, issue #21). Pure: files in, text out.

Onboarding a site is mostly reading: which engine it runs, what its URL grammar is, where the
title, duration, upload date and credits actually live, how a removed video answers. All of that
is decidable from the fixtures already on disk, so this module decides it — deterministically, with
no model, no network and no database. What it prints is a **draft**: a person writes the adapter.

Two rules shape the output, both from `CLAUDE.md`:

**Structure, never content.** Selectors, attribute names, path *templates*, counts, string lengths
and which candidates agree with each other — never an extracted title, performer, tag or a page's
own URL. A reviewer has the fixture open; what they need from here is where to look. The one
exception is the removal marker, and it is printed from `REMOVAL_MARKERS` below — our vocabulary,
not the page's.

**Nothing here reaches the fetch queue.** A path template proposed here is a hypothesis about a
site's URL grammar; it is executed only once someone has written it into a registered adapter and
merged it (ADR 0043). This module therefore returns text and nothing else: it stores nothing the
crawler could ever load.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from crawler.sites.common import (
    absolute_url,
    canonical_url,
    clean_text,
    json_ld_objects,
    meta_contents,
    parse_iso_duration,
)

#: Engine fingerprints, most specific first. A marker is a lowercase substring of the raw HTML.
ENGINE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("kvs", ("kt_player", "/get_file/", "kernelteamvideosharing", "/contents/videos_screenshots/")),
    ("wordpress", ("wp-content", "wp-includes", "wp-json")),
)
#: What a page says when the video is gone. Ours, not the site's: only these are ever printed.
REMOVAL_MARKERS: tuple[str, ...] = (
    "deleted",
    "removed",
    "not found",
    "no longer available",
    "nicht gefunden",
    "nicht verfügbar",
    "404",
)
#: First path segments that name a performer index on the sites seen so far.
MODEL_SEGMENTS: frozenset[str] = frozenset(
    {
        "model",
        "models",
        "performer",
        "performers",
        "actor",
        "actors",
        "darsteller",
        "artist",
        "artists",
        "star",
        "stars",
    }
)
SEARCH_WORDS: tuple[str, ...] = ("search", "suche", "suchen", "find", "recherche", "busca")
SEARCH_INPUT_NAMES: frozenset[str] = frozenset({"q", "s", "query", "search", "keyword", "k", "kw"})
PAGE_PARAMETERS: frozenset[str] = frozenset({"page", "p", "pg", "from", "from_videos", "start"})
#: A family needs this many members before its varying positions mean anything.
MIN_FAMILY = 3
#: At most this many distinct values at one position still reads as a section, not an identifier.
SECTION_VALUES = 6
#: Distinct links of one family that make a page a listing. Recorded pages are small (a fixture of
#: a thin performer page carried 7), and a detail page is recognised by its video markers first.
MIN_LISTING_LINKS = 6
MAX_FAMILIES = 12
_MMSS = re.compile(r"\A(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\Z")
_IMAGE_ATTRIBUTES: tuple[str, ...] = ("data-original", "data-src", "data-lazy", "data-thumb", "src")

DETAIL, LISTING, MISSING, UNKNOWN = "detail", "listing", "missing", "unknown"
#: A post whose content is a set of pictures rather than a video (issue #118,
#: docs/research/creator-leak-sources.md). Independent creators' paid posts are republished
#: this way far more often than as a video page, and the media lane hashes stills already
#: (`index.Media.Kind.PHOTO`, ADR 0020) — so the draft has to be able to see one.
GALLERY = "gallery"
#: Same-shaped pictures on one page that make it a gallery rather than a page with a poster
#: and a theme. Deliberately low: a recorded fixture of a thin post is small.
MIN_GALLERY_IMAGES = 6
#: What a link to a picture ends in. A link family of these is a picture set, never a listing.
IMAGE_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


@dataclass(frozen=True)
class LinkFamily:
    """Same-site links that share one path template, e.g. `/video/*/` seen 24 times."""

    template: str
    regex: str
    count: int
    with_text: int
    numeric_only: bool
    image_attributes: tuple[str, ...] = ()
    containers: tuple[str, ...] = ()

    @property
    def first_segment(self) -> str:
        parts = [part for part in self.template.split("/") if part]
        return parts[0] if parts else ""


@dataclass(frozen=True)
class FieldCandidate:
    """Where one field could be read, and which other candidates say the same thing.

    `group` is a letter shared by candidates that found the same value, `-` for a candidate that is
    a place to look rather than a value (a repeated markup shape). `note` carries a quirk the
    adapter will have to handle. The value itself is never carried out of this module.
    """

    selector: str
    length: int
    group: str
    note: str = ""


@dataclass(frozen=True)
class SearchForm:
    action: str
    data_url: str
    method: str
    inputs: tuple[str, ...]


@dataclass(frozen=True)
class PageProfile:
    name: str
    kind: str
    reason: str
    template: str = ""
    json_ld_types: tuple[str, ...] = ()
    #: The biggest family of same-shaped pictures on this page: how many, the path template they
    #: share and the attribute that carried them. Structure only — never an image URL.
    images: int = 0
    image_template: str = ""
    image_attribute: str = ""


@dataclass(frozen=True)
class SiteDraft:
    domain: str
    engine: str
    engine_markers: tuple[str, ...] = ()
    pages: tuple[PageProfile, ...] = ()
    families: tuple[LinkFamily, ...] = ()
    detail_regex: str = ""
    #: Performer-index templates with how often they are linked, most linked first.
    model_paths: tuple[tuple[str, int], ...] = ()
    search: tuple[SearchForm, ...] = ()
    page_parameters: tuple[str, ...] = ()
    card_text: bool = False
    #: The host the site's own links use, when it is not the bare domain (an apex that redirects
    #: to `www.`): what `KvsSite.host` and a bespoke adapter's seed URLs have to use.
    host: str = ""
    fields: Mapping[str, tuple[FieldCandidate, ...]] = field(default_factory=dict)
    removal: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# --------------------------------------------------------------------------- paths and families


def _segments(path: str) -> tuple[list[str], bool]:
    return [part for part in path.split("/") if part], path.endswith("/")


def _template(parts: Sequence[str], variable: Sequence[bool], trailing: bool) -> str:
    body = "/".join("*" if flag else part for part, flag in zip(parts, variable, strict=True))
    return "/" + body + ("/" if trailing else "")


def _regex(parts: Sequence[str], variable: Sequence[bool], numeric: Sequence[bool]) -> str:
    """The template as a regex: a varying position whose values are all digits becomes `\\d+`."""
    body = "/".join(
        (r"\d+" if is_numeric else "[^/]+") if is_variable else re.escape(part)
        for part, is_variable, is_numeric in zip(parts, variable, numeric, strict=True)
    )
    return "^/" + body + "/?$"


@dataclass(frozen=True)
class _Anchor:
    path: str
    text_length: int
    image_attributes: tuple[str, ...]
    container: str


def _container(node: LexborNode) -> str:
    """The nearest ancestor that names itself: `#list_videos_common_videos_list` or `div.thumb`."""
    current = node.parent
    for _ in range(4):
        if current is None:
            break
        attributes = current.attributes
        if node_id := (attributes.get("id") or "").strip():
            return f"#{node_id}"
        if classes := (attributes.get("class") or "").split():
            return f"{current.tag}.{classes[0]}"
        current = current.parent
    return ""


def _anchors(tree: LexborHTMLParser, base: str) -> list[_Anchor]:
    host = urlsplit(base).hostname or ""
    found: list[_Anchor] = []
    for node in tree.css("a[href]"):
        url = absolute_url(base, node.attributes.get("href"))
        if url is None:
            continue
        parts = urlsplit(url)
        if (parts.hostname or "").removeprefix("www.") != host.removeprefix("www."):
            continue
        images = tuple(
            attribute
            for image in node.css("img")
            for attribute in _IMAGE_ATTRIBUTES
            if (image.attributes.get(attribute) or "").strip()
        )
        found.append(
            _Anchor(
                path=parts.path or "/",
                text_length=len(clean_text(node.text())),
                image_attributes=images,
                container=_container(node),
            )
        )
    return found


def _split(
    by_path: Mapping[str, list[_Anchor]], depth: int, position: int
) -> list[dict[str, list[_Anchor]]]:
    """Partition paths by the value at `position` while that position names a section, not a thing.

    A section segment (`videos`, `kategorien`) takes few values across many links; an identifier or
    a slug takes nearly one per link. Splitting on the first section position and recursing is what
    keeps `/de/videos/<id>/<slug>` apart from `/de/kategorien/<slug>/<page>` on a site that serves
    its whole catalogue under a language prefix — grouping by the first segment alone cannot.
    """
    if position >= depth:
        return [dict(by_path)]
    values = {_segments(path)[0][position] for path in by_path}
    if len(values) == 1 or (len(values) <= SECTION_VALUES and len(by_path) >= 2 * len(values)):
        if len(values) == 1:
            return _split(by_path, depth, position + 1)
        parts: list[dict[str, list[_Anchor]]] = []
        for value in sorted(values):
            subset = {
                path: anchors
                for path, anchors in by_path.items()
                if _segments(path)[0][position] == value
            }
            parts.extend(_split(subset, depth, position + 1))
        return parts
    return [dict(by_path)]


def _family(by_path: Mapping[str, list[_Anchor]], depth: int, trailing: bool) -> LinkFamily | None:
    paths = [_segments(path)[0] for path in by_path]
    if len(paths) < MIN_FAMILY:
        return None
    columns = [{path[index] for path in paths} for index in range(depth)]
    variable = [len(column) > 1 for column in columns]
    numeric = [bool(column) and all(value.isdigit() for value in column) for column in columns]
    varying = {value for index, column in enumerate(columns) if variable[index] for value in column}
    members = [anchor for same_path in by_path.values() for anchor in same_path]
    return LinkFamily(
        template=_template(paths[0], variable, trailing),
        regex=_regex(paths[0], variable, numeric),
        count=len(paths),
        with_text=sum(
            1
            for same_path in by_path.values()
            if any(anchor.text_length > 0 for anchor in same_path)
        ),
        numeric_only=bool(varying) and all(value.isdigit() for value in varying),
        image_attributes=tuple(
            sorted({name for member in members for name in member.image_attributes})
        ),
        containers=tuple(
            name
            for name, _ in Counter(
                member.container for member in members if member.container
            ).most_common(2)
        ),
    )


def link_families(anchors: Sequence[_Anchor]) -> list[LinkFamily]:
    """Group same-site links into path templates: a position that varies within a group is `*`.

    Links are bucketed by depth and trailing slash, then partitioned by their section segments
    (`_split`); what is left varying in a bucket is the identifier. Counts are of distinct paths,
    so a card linked twice on one page counts once.
    """
    buckets: dict[tuple[int, bool], dict[str, list[_Anchor]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for anchor in anchors:
        parts, trailing = _segments(anchor.path)
        if parts:
            buckets[(len(parts), trailing)][anchor.path].append(anchor)
    families = [
        family
        for (depth, trailing), by_path in buckets.items()
        for part in _split(by_path, depth, 0)
        if (family := _family(part, depth, trailing)) is not None
    ]
    families.sort(key=lambda family: (-family.count, family.template))
    return families[:MAX_FAMILIES]


# ----------------------------------------------------------------------------- page classification


def _json_ld_types(tree: LexborHTMLParser) -> list[str]:
    types: list[str] = []
    for obj in json_ld_objects(tree):
        value = obj.get("@type")
        if isinstance(value, str):
            types.append(value)
        elif isinstance(value, list):
            types.extend(item for item in value if isinstance(item, str))
    return types


def _title_text(tree: LexborHTMLParser) -> str:
    node = tree.css_first("title")
    return clean_text(node.text()) if node else ""


def _body_length(tree: LexborHTMLParser) -> int:
    node = tree.css_first("body")
    return len(clean_text(node.text())) if node else 0


def _is_image_path(path: str) -> bool:
    return path.lower().endswith(IMAGE_SUFFIXES)


def _looks_variable(segment: str) -> bool:
    """A path segment that reads as one item's own name rather than a fixed folder."""
    stem = segment.rpartition(".")[0] or segment
    return any(character.isdigit() for character in stem)


def gallery_family(tree: LexborHTMLParser) -> tuple[str, int, str]:
    """Biggest family of same-shaped pictures on the page: (path template, count, attribute).

    One family, not the total: a theme's icons, avatars and sprites each have their own shape, and
    what says "this page is a picture set" is many pictures that look like each other. Only the
    *shape* leaves this function; the values stay in the fixture.
    """
    families: dict[tuple[str, str], set[str]] = defaultdict(set)
    for node in tree.css("img"):
        for attribute in _IMAGE_ATTRIBUTES:
            value = (node.attributes.get(attribute) or "").strip()
            if not value:
                continue
            path = urlsplit(value).path
            if not path:
                break
            parts, trailing = _segments(path)
            variable = [_looks_variable(part) for part in parts]
            families[(_template(parts, variable, trailing), attribute)].add(value)
            break
    if not families:
        return "", 0, ""
    (template, attribute), values = max(families.items(), key=lambda item: len(item[1]))
    return template, len(values), attribute


def classify(tree: LexborHTMLParser, anchors: Sequence[_Anchor]) -> tuple[str, str]:
    """What kind of page this fixture is, and the one observation that decided it."""
    title = _title_text(tree).lower()
    hits = [marker for marker in REMOVAL_MARKERS if marker in title]
    if hits:
        return MISSING, f"title contains {hits[0]!r}"
    types = _json_ld_types(tree)
    videos = sum(1 for name in types if name == "VideoObject")
    if videos == 1:
        return DETAIL, "one JSON-LD VideoObject"
    if videos == 0 and (meta_contents(tree, "og:video") or meta_contents(tree, "og:video:url")):
        return DETAIL, "og:video meta tag"
    if videos == 0 and tree.css_first('[itemprop="duration"], [itemprop="uploadDate"]') is not None:
        return DETAIL, "microdata duration/uploadDate"
    if videos == 0 and "ImageGallery" in types:
        return GALLERY, "JSON-LD ImageGallery"
    # A listing links *pages*; a picture set links the pictures themselves. A family of links to
    # image files is therefore no evidence of a listing, and leaving it in would call every
    # gallery post a listing (and, before the filter existed, every tube listing a gallery).
    biggest = link_families([anchor for anchor in anchors if not _is_image_path(anchor.path)])
    if biggest and biggest[0].count >= MIN_LISTING_LINKS:
        return LISTING, f"{biggest[0].count} links share {biggest[0].template}"
    if videos > 1:
        return LISTING, f"{videos} JSON-LD VideoObjects"
    if videos == 0:
        _, images, attribute = gallery_family(tree)
        if images >= MIN_GALLERY_IMAGES:
            return GALLERY, f"{images} same-shaped pictures in @{attribute}"
    if _body_length(tree) < 500:
        return MISSING, "almost no body text"
    return UNKNOWN, "no video markers and no repeated link family"


# ------------------------------------------------------------------------------- field candidates


def _duration_seconds(value: str) -> tuple[int | None, str]:
    """Seconds and the quirk that had to be worked around, if any. Pure.

    A theme that prints `T6M15S` without the leading `P` is not hypothetical: two of the sites in
    `crawler/sites/` do it, and each cost a debugging round. Saying so in the draft is the point.
    """
    text = value.strip()
    if (seconds := parse_iso_duration(text)) is not None:
        return seconds, ""
    if (seconds := parse_iso_duration(f"P{text}")) is not None:
        return seconds, "ISO 8601 without the leading P"
    if (match := _MMSS.match(text)) is not None:
        hours, minutes, secs = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(secs), "clock format, not ISO 8601"
    return None, ""


def _grouped(pairs: Sequence[tuple[str, str, str]]) -> tuple[FieldCandidate, ...]:
    """Candidates labelled A, B, … by the value they found. The values never leave this function."""
    labels: dict[str, str] = {}
    out: list[FieldCandidate] = []
    for selector, value, note in pairs:
        key = " ".join(value.split()).casefold()
        if not key:
            continue
        if key not in labels:
            labels[key] = chr(ord("A") + len(labels)) if len(labels) < 26 else "?"
        out.append(FieldCandidate(selector=selector, length=len(key), group=labels[key], note=note))
    return tuple(out)


def _node_selector(node: LexborNode) -> str:
    tag = node.tag or "*"
    classes = (node.attributes.get("class") or "").split()
    return f"{tag}.{classes[0]}" if classes else tag


def _json_ld_video(tree: LexborHTMLParser) -> Mapping[str, object]:
    for obj in json_ld_objects(tree):
        types = obj.get("@type")
        if types == "VideoObject" or (isinstance(types, list) and "VideoObject" in types):
            return obj
    return {}


def _string(obj: Mapping[str, object], key: str) -> str:
    value = obj.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(item for item in value if isinstance(item, str))
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else ""
    return ""


def _meta_pairs(tree: LexborHTMLParser, keys: Sequence[str]) -> list[tuple[str, str, str]]:
    return [
        (f"meta[{key}]", values[0], "")
        for key in keys
        if (values := meta_contents(tree, key)) and values[0]
    ]


def _text_pairs(tree: LexborHTMLParser, selectors: Sequence[str]) -> list[tuple[str, str, str]]:
    """The first node matching each selector, reported under the selector a reviewer would write."""
    pairs: list[tuple[str, str, str]] = []
    for selector in selectors:
        node = tree.css_first(selector)
        if node is None:
            continue
        value = clean_text(node.attributes.get("content") or "") or clean_text(node.text())
        if value:
            derived = _node_selector(node)
            label = selector if derived == selector else f"{selector} ({derived})"
            pairs.append((label, value, ""))
    return pairs


def _attribute_candidates(tree: LexborHTMLParser, attribute: str) -> tuple[FieldCandidate, ...]:
    """Elements carrying `data-duration="6:15"` and the like: a shape to read, not a value."""
    shapes: Counter[str] = Counter()
    for node in tree.css(f"[{attribute}]"):
        if clean_text(node.attributes.get(attribute) or ""):
            shapes[f"{_node_selector(node)}[{attribute}]"] += 1
    return tuple(
        FieldCandidate(selector=f"{selector} ({count}x)", length=0, group="-")
        for selector, count in shapes.most_common(2)
    )


def _is_model_link(path: str) -> bool:
    """`/de/actor/lena-hartwell/` yes, `/de/actor/` no: a link into the performer index."""
    parts, _ = _segments(path)
    return any(part.lower() in MODEL_SEGMENTS for part in parts[:-1])


def field_candidates(tree: LexborHTMLParser) -> dict[str, tuple[FieldCandidate, ...]]:
    """Where title, duration, upload date and credits could be read off a detail page."""
    video = _json_ld_video(tree)
    titles: list[tuple[str, str, str]] = []
    if title := _title_text(tree):
        titles.append(("<title>", title, ""))
    titles += _meta_pairs(tree, ("og:title", "twitter:title", "title"))
    if name := _string(video, "name"):
        titles.append(("json-ld VideoObject.name", name, ""))
    titles += _text_pairs(tree, ('[itemprop="name"]', "h1"))

    raw_durations: list[tuple[str, str, str]] = _meta_pairs(
        tree, ("og:video:duration", "video:duration", "duration")
    ) + _text_pairs(tree, ('[itemprop="duration"]',))
    if stated := _string(video, "duration"):
        raw_durations.insert(0, ("json-ld VideoObject.duration", stated, ""))
    durations: list[tuple[str, str, str]] = []
    for selector, raw, _ in raw_durations:
        seconds, note = _duration_seconds(raw)
        if seconds is not None:
            durations.append((selector, str(seconds), note))
        else:
            durations.append((selector, f"unparsed:{selector}", "does not parse as a duration"))
    # A clock string or a `data-duration` attribute is a place to look, not a value to compare:
    # reported without a group letter.
    clocks: Counter[str] = Counter()
    for node in tree.css("span, div, em, strong, time, li, p"):
        if _MMSS.match(clean_text(node.text())):
            clocks[_node_selector(node)] += 1
    clock_candidates = tuple(
        FieldCandidate(selector=f"{selector} (text mm:ss, {count}x)", length=0, group="-")
        for selector, count in clocks.most_common(2)
    ) + _attribute_candidates(tree, "data-duration")

    uploaded: list[tuple[str, str, str]] = []
    if value := _string(video, "uploadDate"):
        uploaded.append(("json-ld VideoObject.uploadDate", value, ""))
    uploaded += _meta_pairs(
        tree, ("video:release_date", "og:video:release_date", "article:published_time")
    )
    uploaded += _text_pairs(tree, ('[itemprop="uploadDate"]', "time[datetime]"))

    performers: list[tuple[str, str, str]] = []
    if actor := _string(video, "actor"):
        performers.append(("json-ld VideoObject.actor", actor, ""))
    performers += _meta_pairs(tree, ("video:actor",))
    performers += _text_pairs(tree, ('[itemprop="actor"]',))
    containers: Counter[str] = Counter()
    for node in tree.css("a[href]"):
        if _is_model_link(urlsplit(node.attributes.get("href") or "").path):
            containers[_container(node)] += 1
    model_candidates = tuple(
        FieldCandidate(
            selector=f"{container or '(no container)'} a (performer-index links, {count}x)",
            length=0,
            group="-",
        )
        for container, count in containers.most_common(3)
    )

    return {
        "title": _grouped(titles),
        "duration": _grouped(durations) + clock_candidates,
        "uploaded_at": _grouped(uploaded),
        "performers": _grouped(performers) + model_candidates,
    }


# --------------------------------------------------------------------------------- search and page


def search_forms(tree: LexborHTMLParser) -> list[SearchForm]:
    forms: list[SearchForm] = []
    for node in tree.css("form"):
        action = clean_text(node.attributes.get("action") or "")
        data_url = clean_text(node.attributes.get("data-url") or "")
        names = tuple(
            name
            for input_node in node.css("input, select")
            if (name := clean_text(input_node.attributes.get("name") or ""))
        )
        haystack = f"{action} {data_url}".lower()
        looks_like_search = any(word in haystack for word in SEARCH_WORDS) or any(
            name.lower() in SEARCH_INPUT_NAMES for name in names
        )
        if looks_like_search:
            forms.append(
                SearchForm(
                    action=action,
                    data_url=data_url,
                    method=(node.attributes.get("method") or "get").lower(),
                    inputs=names,
                )
            )
    return forms


def _page_parameters(tree: LexborHTMLParser, base: str) -> list[str]:
    found: Counter[str] = Counter()
    for node in tree.css("a[href]"):
        url = absolute_url(base, node.attributes.get("href"))
        if url is None:
            continue
        for key, value in parse_qsl(urlsplit(url).query):
            if key.lower() in PAGE_PARAMETERS and value.isdigit():
                found[key] += 1
    return [key for key, _ in found.most_common(4)]


# ------------------------------------------------------------------------------------- the draft


def analyse(domain: str, fixtures: Mapping[str, str]) -> SiteDraft:
    """Recorded HTML of one site to a reviewable draft. Pure: no network, no database, no clock."""
    base = f"https://{domain}/"
    trees = {name: LexborHTMLParser(html) for name, html in fixtures.items() if html.strip()}
    anchors = {name: _anchors(tree, base) for name, tree in trees.items()}
    pages: list[PageProfile] = []
    for name in sorted(trees):
        kind, reason = classify(trees[name], anchors[name])
        template, images, attribute = gallery_family(trees[name])
        pages.append(
            PageProfile(
                name=name,
                kind=kind,
                reason=reason,
                json_ld_types=tuple(sorted(set(_json_ld_types(trees[name])))),
                images=images,
                image_template=template,
                image_attribute=attribute,
            )
        )

    listings = [page.name for page in pages if page.kind == LISTING]
    pooled = [anchor for name in (listings or sorted(trees)) for anchor in anchors.get(name, ())]
    families = link_families(pooled)

    details = [page.name for page in pages if page.kind == DETAIL]
    detail_regex = ""
    for name in details:
        canonical = canonical_url(trees[name], base)
        if canonical is None:
            continue
        path = urlsplit(canonical).path or "/"
        for family in families:
            if re.match(family.regex, path):
                detail_regex = family.regex
                break
        if detail_regex:
            break

    # The performer index is rarely linked from a listing page and a video credits one or two
    # performers, so it is found by its own path segment over every fixture rather than by a
    # family: three links are needed for a family, and one is enough to see `/de/actor/<slug>/`.
    model_paths = model_index([anchor for found in anchors.values() for anchor in found])
    forms = [form for name in sorted(trees) for form in search_forms(trees[name])]
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    unique_forms: list[SearchForm] = []
    for form in forms:
        key = (form.action, form.data_url, form.method, form.inputs)
        if key not in seen:
            seen.add(key)
            unique_forms.append(form)

    fields: dict[str, tuple[FieldCandidate, ...]] = {}
    if details:
        fields = field_candidates(trees[details[0]])

    detail_family = next((f for f in families if f.regex == detail_regex), None)
    card_text = detail_family is not None and detail_family.with_text >= detail_family.count

    engine, markers = _engine(fixtures)
    return SiteDraft(
        domain=domain,
        engine=engine,
        engine_markers=markers,
        pages=tuple(pages),
        families=tuple(families),
        detail_regex=detail_regex,
        model_paths=tuple(model_paths),
        search=tuple(unique_forms),
        page_parameters=tuple(
            dict.fromkeys(key for name in trees for key in _page_parameters(trees[name], base))
        ),
        host=_host(trees, base),
        card_text=card_text,
        fields=fields,
        removal=tuple(page.reason for page in pages if page.kind == MISSING),
        notes=tuple(_notes(pages, details, detail_regex, model_paths)),
    )


def _host(trees: Mapping[str, LexborHTMLParser], base: str) -> str:
    """The host the site links itself by, when that is not the bare domain."""
    domain = urlsplit(base).hostname or ""
    hosts: Counter[str] = Counter()
    for tree in trees.values():
        for node in tree.css("a[href], link[rel=canonical]"):
            url = absolute_url(base, node.attributes.get("href"))
            host = (urlsplit(url).hostname or "") if url else ""
            if host and host.removeprefix("www.") == domain.removeprefix("www."):
                hosts[host] += 1
    top = hosts.most_common(1)
    return top[0][0] if top and top[0][0] != domain else ""


def model_index(anchors: Sequence[_Anchor]) -> list[tuple[str, int]]:
    """Performer-index templates like `/de/actor/{slug}/`, by how often they are linked.

    Found by the segment that names it rather than as a link family: a video credits one or two
    performers, and a family needs three links. Several candidates that differ only in a leading
    segment mean the site serves one catalogue under several language skins — which one to crawl is
    a decision, so all of them are reported.
    """
    prefixes: Counter[str] = Counter()
    for anchor in anchors:
        parts, trailing = _segments(anchor.path)
        for index, part in enumerate(parts[:-1]):
            if part.lower() in MODEL_SEGMENTS:
                head = "/" + "/".join(parts[: index + 1])
                prefixes[f"{head}/{{slug}}" + ("/" if trailing else "")] += 1
                break
    return prefixes.most_common(4)


def _engine(fixtures: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    lowered = "\n".join(fixtures.values()).lower()
    for engine, markers in ENGINE_MARKERS:
        hits = tuple(marker for marker in markers if marker in lowered)
        if hits:
            return engine, hits
    return "bespoke", ()


def _notes(
    pages: Sequence[PageProfile],
    details: Sequence[str],
    detail_regex: str,
    model_paths: Sequence[tuple[str, int]],
) -> list[str]:
    notes: list[str] = []
    if len(model_paths) > 1:
        notes.append(
            "several performer-index templates: the site serves one catalogue under several "
            "prefixes (language skins) — which one the adapter crawls is a decision"
        )
    if not details:
        notes.append("no fixture looks like a video page — record one before writing the adapter")
    if details and not detail_regex:
        notes.append(
            "the video page has no <link rel=canonical>: pick the detail family by hand, and "
            "decide what the adapter canonicalises to"
        )
    if not any(page.kind == MISSING for page in pages):
        notes.append("no fixture of a removed video — record one, it decides `removed`")
    if not any(page.kind == LISTING for page in pages):
        notes.append("no listing fixture — link families were pooled over every fixture instead")
    if any(page.kind == GALLERY for page in pages):
        notes.append(
            "a fixture is a picture set, not a video page: its media are "
            "`MediaSpec(kind=Media.Kind.PHOTO, url=...)`, one per picture, and the post is one "
            "page — the media lane hashes stills already (ADR 0020). Decide what the adapter does "
            "with the thumbnail *and* the full-size link when the post carries both"
        )
    return notes


def kvs_draft(draft: SiteDraft) -> list[str]:
    """A `KvsSite(...)` entry for a site the fingerprint calls KVS: for those, the draft is it."""
    if draft.engine != "kvs":
        return []
    search_path = "/search/{slug}/"
    for form in draft.search:
        if "%QUERY%" in form.data_url:
            # The path only: `KvsSite` builds the URL from the site's own host.
            search_path = (urlsplit(form.data_url).path or "").replace("%QUERY%", "{slug}")
            break
    lines = [
        "KvsSite(",
        f'    domain="{draft.domain}",',
        *([f'    host="{draft.host}",'] if draft.host else []),
        f'    video_path=r"{draft.detail_regex or "^/video/[^/]+/?$"}",',
        f'    model_path="{draft.model_paths[0][0] if draft.model_paths else ""}",',
        f'    search_path="{search_path}",',
        "    # selectors: fill from the field candidates above, then assert them in the test",
        '    tag_selector="",',
        '    category_selector="",',
        '    performer_selector="",',
        ")",
    ]
    return lines


# ----------------------------------------------------------------------------------- rendering


def _field_lines(name: str, candidates: Sequence[FieldCandidate]) -> list[str]:
    if not candidates:
        return [f"  {name:<12} none found"]
    lines = []
    for index, candidate in enumerate(candidates):
        label = name if index == 0 else ""
        note = f"  — {candidate.note}" if candidate.note else ""
        lines.append(
            f"  {label:<12} [{candidate.group}] len {candidate.length:>4}  "
            f"{candidate.selector}{note}"
        )
    return lines


def render(draft: SiteDraft) -> list[str]:
    """The draft as lines of plain text. Structure only — no title, name, tag or URL is printed."""
    lines = [
        f"{draft.domain}: {draft.engine} "
        + (f"({', '.join(draft.engine_markers)})" if draft.engine_markers else "(no known engine)"),
        "",
        "fixtures",
    ]
    for page in draft.pages:
        types = f"  json-ld: {', '.join(page.json_ld_types)}" if page.json_ld_types else ""
        lines.append(f"  {page.name:<26} {page.kind:<8} {page.reason}{types}")
        if page.images:
            lines.append(
                f"  {'':<26} pictures {page.images:>4}x @{page.image_attribute} "
                f"{page.image_template}"
            )

    lines += ["", "link families (same site, pooled over the listing fixtures)"]
    for family in draft.families:
        flags = []
        if family.regex == draft.detail_regex:
            flags.append("DETAIL")
        if family.first_segment in MODEL_SEGMENTS:
            flags.append("model index")
        if family.numeric_only:
            flags.append("pagination?")
        lines.append(
            f"  {family.template:<34} {family.count:>4}x  text {family.with_text}/{family.count}"
            f"  img {','.join(family.image_attributes) or '-'}"
            f"  in {','.join(family.containers) or '-'}"
            + (f"  [{' '.join(flags)}]" if flags else "")
        )
    if draft.detail_regex:
        lines.append(f'  video_path=r"{draft.detail_regex}"')
    for template, count in draft.model_paths:
        lines.append(f'  model_path="{template}" ({count} links)')
    if draft.host:
        lines.append(f'  host="{draft.host}" (the site links itself by this host, not the domain)')
    lines.append(
        f"  card_text = {draft.card_text} "
        "(every detail card carries visible text -> the title gate of ADR 0029 can walk listings)"
    )
    if draft.page_parameters:
        lines.append(f"  pagination parameters: {', '.join(draft.page_parameters)}")

    lines += ["", "search"]
    if draft.search:
        for form in draft.search:
            lines.append(
                f"  action={form.action or '-'} data-url={form.data_url or '-'} "
                f"method={form.method} inputs={','.join(form.inputs) or '-'}"
            )
    else:
        lines.append("  no search form found — seeds must come from the model index")

    lines += ["", "fields on the video page (group letter = candidates that agree)"]
    for name in ("title", "duration", "uploaded_at", "performers"):
        lines += _field_lines(name, draft.fields.get(name, ()))

    lines += ["", "removal"]
    lines += [f"  {reason}" for reason in draft.removal] or [
        "  unknown — no fixture of a removed video"
    ]

    if kvs := kvs_draft(draft):
        lines += ["", "KVS template entry (crawler/sites/kvs.py KVS_SITES)"]
        lines += [f"  {line}" for line in kvs]

    lines += ["", "next (CLAUDE.md, Adding a site adapter)"]
    lines += [
        f"  1. fixtures are in place ({len(draft.pages)} recorded)",
        "  2. "
        + (
            "add the KvsSite entry above to KVS_SITES"
            if draft.engine == "kvs"
            else f"write crawler/sites/{draft.domain.split('.')[0]}.py against the families above"
        ),
        "  3. register it in crawler/sites/registry.py",
        f"  4. write crawler/tests/test_{draft.domain.split('.')[0]}.py — assert the real values "
        "from the fixtures (this draft deliberately prints none)",
        "  5. SitePolicy in catalog/sources.py, recon note in docs/sources/, Site stays disabled",
    ]
    if draft.notes:
        lines += ["", "watch out"] + [f"  - {note}" for note in draft.notes]
    return lines


def load_fixtures(directory: Path) -> dict[str, str]:
    """Every `*.html` in a recorded fixture directory, by file name. The only I/O in this module."""
    return {
        file.name: file.read_text(errors="replace") for file in sorted(directory.glob("*.html"))
    }


def draft_lines(directory: Path) -> list[str]:
    """The whole tool: a fixture directory named after the site, to the lines of a draft."""
    return render(analyse(directory.name, load_fixtures(directory)))
