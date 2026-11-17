"""Pure helpers shared by site adapters: URLs, names, meta tags, JSON-LD, dates."""

import html
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from catalog.models import Creator

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SHARP_S = str.maketrans({"ß": "ss", "ẞ": "SS"})
_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
_SKIPPED_HREF_PREFIXES = ("#", "javascript:", "mailto:", "data:", "tel:")


def clean_text(text: str | None) -> str:
    return " ".join((text or "").split())


def ascii_fold(text: str) -> str:
    """ASCII only, case kept: accents dropped ("López" -> "Lopez"), other scripts removed.

    German text (issue #24): umlauts fold to their base letter ("Mädchen" -> "Madchen"), and "ß"
    becomes "ss" — NFKD has no decomposition for it, so it would otherwise vanish ("Strae").
    """
    decomposed = unicodedata.normalize("NFKD", text.translate(_SHARP_S))
    return decomposed.encode("ascii", "ignore").decode("ascii")


def normalize_name(text: str) -> str:
    """Lower-case ASCII words separated by single spaces: "Eléna_Märsh" -> "elena marsh"."""
    return _NON_ALNUM.sub(" ", ascii_fold(text).lower()).strip()


def slugify(text: str) -> str:
    return normalize_name(text).replace(" ", "-")


def dedupe(items: Iterable[str]) -> list[str]:
    """Drop empty strings and duplicates, keeping the first occurrence."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def creator_names(creator: Creator) -> list[str]:
    aliases = [alias for alias in creator.aliases if isinstance(alias, str)]
    return dedupe(clean_text(name) for name in (creator.name, *aliases))


def name_keys(names: Iterable[str]) -> list[str]:
    """The normalised forms of a person's name and aliases, for `names_person`. Pure."""
    return sorted({key for key in (normalize_name(name) for name in names) if key})


def names_person(text: str, keys: Sequence[str]) -> bool:
    """Does `text` name this person? Pure. Used by index suppression (docs/suppression.md).

    Whole words after normalisation — the rule the crawler links a creator by
    (`crawler.services.match_creators`) — plus one case that rule does not need: a value that *is*
    the name written without separators ("jane_doe", "JaneDoe"), which is how site handles and
    profile slugs spell it. Never a substring match, so "Ann" does not match "Annabel".
    """
    normalized = normalize_name(text)
    if not normalized:
        return False
    padded = f" {normalized} "
    if any(f" {key} " in padded for key in keys):
        return True
    squashed = normalized.replace(" ", "")
    return any(squashed == key.replace(" ", "") for key in keys)


def absolute_url(base: str, href: str | None) -> str | None:
    """Resolve `href` against `base`; None for empty, fragment-only or non-HTTP links."""
    href = (href or "").strip()
    if not href or href.lower().startswith(_SKIPPED_HREF_PREFIXES):
        return None
    url = urldefrag(urljoin(base, href)).url
    return url if urlsplit(url).scheme in {"http", "https"} else None


def meta_content(tree: LexborHTMLParser, key: str) -> str:
    """Content of the first `<meta property=key>` or `<meta name=key>`."""
    values = meta_contents(tree, key)
    return values[0] if values else ""


def meta_contents(tree: LexborHTMLParser, key: str) -> list[str]:
    nodes = tree.css(f'meta[property="{key}"]') or tree.css(f'meta[name="{key}"]')
    return dedupe(clean_text(node.attributes.get("content")) for node in nodes)


def canonical_url(tree: LexborHTMLParser, base: str) -> str | None:
    node = tree.css_first('link[rel="canonical"]')
    return absolute_url(base, node.attributes.get("href")) if node else None


def json_ld_objects(tree: LexborHTMLParser) -> list[dict[str, object]]:
    """All JSON-LD objects on the page, with lists and `@graph` flattened."""
    objects: list[dict[str, object]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            pending: list[object] = [json.loads(node.text(deep=True))]
        except json.JSONDecodeError:
            continue
        while pending:
            item = pending.pop(0)
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                objects.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    pending.extend(graph)
    return objects


def first_json_ld(tree: LexborHTMLParser, type_name: str) -> dict[str, object]:
    for obj in json_ld_objects(tree):
        types = obj.get("@type")
        if type_name == types or (isinstance(types, list) and type_name in types):
            return obj
    return {}


def str_field(obj: Mapping[str, object], key: str) -> str:
    value = obj.get(key)
    return clean_text(html.unescape(value)) if isinstance(value, str) else ""


def parse_iso_duration(value: str) -> int | None:
    """ISO 8601 duration ("PT0H31M38S") -> seconds."""
    match = _ISO_DURATION.match(value.strip())
    if not match or not any(match.groupdict().values()):
        return None
    parts = {name: float(number) for name, number in match.groupdict().items() if number}
    total = (
        parts.get("days", 0) * 86400
        + parts.get("hours", 0) * 3600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )
    return int(total)


def parse_datetime(value: str) -> datetime | None:
    """ISO date or datetime -> aware datetime (UTC when no offset is given)."""
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
