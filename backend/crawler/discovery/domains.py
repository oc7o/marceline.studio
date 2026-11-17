"""Offline, deliberately small domain rules; no public-suffix downloads."""

import ipaddress
import re
from urllib.parse import urlsplit

IGNORE_DOMAINS = frozenset(
    {
        "fanhub.example",
        "fanbase.example",
        "reddit.com",
        "x.com",
        "twitter.com",
        "instagram.com",
        "youtube.com",
        "youtu.be",
        "wikipedia.org",
        "imdb.com",
        # Professional networking and creator reference databases are not sources.
        "linkedin.com",
        "creator-wiki.example",
        "creator-db.example",
        "facebook.com",
        "tiktok.com",
    }
)

# Issue #18: each entry below carries its own reason, so a candidate shows *why* it was
# ignored (`services.apply_rules`/`reapply_rules`) instead of the generic ignore-list note.
_COMPETITOR = "leak-protection competitor, not a leak source"
IGNORE_REASONS: dict[str, str] = {
    # Public broadcaster media libraries (DACH): legal, licensed catch-up TV, never a leak
    # source.
    "ardmediathek.de": "public broadcaster media library (ARD Mediathek), not a leak source",
    "zdf.de": "public broadcaster media library (ZDF Mediathek), not a leak source",
    "arte.tv": "public broadcaster media library (arte Mediathek), not a leak source",
    "3sat.de": "public broadcaster media library (3sat Mediathek), not a leak source",
    "orf.at": "public broadcaster media library (ORF TVthek), not a leak source",
    "srf.ch": "public broadcaster media library (SRF Play), not a leak source",
    # Legal streaming guides/aggregators: point to licensed offers, never a leak source.
    "werstreamt.es": "legal streaming guide/aggregator, not a leak source",
    "justwatch.com": "legal streaming guide/aggregator, not a leak source",
    # Leak-protection competitors (placeholder domains in this export): not a leak source, and
    # proposing one as a source would be a confusing mixup.
    "takedown-service-a.example": _COMPETITOR,
    "takedown-service-b.example": _COMPETITOR,
    "takedown-service-c.example": _COMPETITOR,
    # Candidate review 2026-09-17 (issues #23, #25):
    # off-topic search hits the German query templates pulled in, none hosting or linking
    # any video.
    "cnet.com": "off-topic search hit (mainstream tech news), not a leak source",
    "dfb-akademie.de": "off-topic search hit (German football association academy site), not a "
    "leak source",
    "discadia.com": "off-topic search hit (Discord server directory), not a leak source",
    "takedown-service-4.example": "off-topic search hit (takedown-service blog post), not a leak source",
    "semrush.com": "off-topic search hit (traffic-analytics page about a domain, not the domain "
    "itself), not a leak source",
    "sgvmotoczesci.pl": "off-topic search hit (Polish motorcycle-parts shop), not a leak source",
    # Third-party Telegram channel directories, not t.me itself; Telegram crawling is its own
    # "not yet" track.
    "telegram-group.com": "third-party Telegram channel directory, not t.me itself",
    "telegram.im": "third-party Telegram channel directory, not t.me itself",
    "tgramsearch.com": "third-party Telegram channel directory, not t.me itself",
    # Infrastructure, not content: paste services carrying link lists, no video/performer content.
    "rentry.co": "paste service, no video/performer content, just link-list hosting",
    "rentry.org": "paste service, no video/performer content, just link-list hosting",
    # Transposed-letter typosquat of hexupload.net (file hoster, HOSTER_LABELS): a single 403
    # hit, not worth tracking separately from the real brand.
    "hexlupoad.net": "typosquat of hexupload.net (file hoster); not tracked separately",
}
IGNORE_DOMAINS = IGNORE_DOMAINS | frozenset(IGNORE_REASONS)
MULTIPART_SUFFIXES = frozenset({"co.uk", "com.au", "co.nz", "co.jp", "com.br", "co.in"})
HOSTER_LABELS = frozenset(
    {
        "vidara",
        "lulustream",
        "luluvdo",
        "doodstream",
        "dood",
        "streamtape",
        "rapidgator",
        "savefiles",
        "bigwarp",
        "strmup",
        "ddownload",
        "krakenfiles",
        "uploadmall",
        "send",
        "bit",
        "tinyurl",
        # Candidate review 2026-09-17: a URL shortener, same bucket as bit/tinyurl above.
        "cuty",
    }
)
SOCIAL_DOMAINS = frozenset({"t.me", "telegram.org", "discord.com", "discord.gg"})
# Torrent/P2P sources (ADR 0013 amendment 2026-09-17): different technology and legal footing
# than a file hoster, never given a page adapter, but not the same as `ignored` either — kept
# visible under its own kind/status for a later decision.
TORRENT_LABELS = frozenset({"p2p-site"})


def normalize_domain(value: str) -> str:
    try:
        parts = urlsplit(value if "://" in value else "//" + value)
        if parts.scheme and parts.scheme not in {"http", "https"}:
            return ""
        host = (parts.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
        try:
            ipaddress.ip_address(host)
            return ""
        except ValueError:
            pass
        labels = host.split(".")
        if len(labels) < 2 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
        ):
            return ""
        count = 3 if ".".join(labels[-2:]) in MULTIPART_SUFFIXES else 2
        return ".".join(labels[-count:])
    except (ValueError, UnicodeError):
        return ""


def ignored(domain: str) -> bool:
    return domain in IGNORE_DOMAINS or domain.split(".")[0] == "google"


def ignore_reason(domain: str) -> str:
    """Specific reason `domain` is ignored, or "" for the older, generic part of the list.

    `services.apply_rules` falls back to its generic note in that case.
    """
    return IGNORE_REASONS.get(domain, "")


def classify(domain: str) -> tuple[str, str]:
    if domain in SOCIAL_DOMAINS:
        return "social", "ignored"
    if domain.split(".")[0] in HOSTER_LABELS:
        return "file_hoster", "hoster"
    if domain.split(".")[0] in TORRENT_LABELS:
        return "torrent", "torrent"
    return "unknown", "proposed"
