"""What kind of place a candidate is, and whether our own rules let us cover it.

Pure and offline: it reads only what the bounded probe already recorded on the candidate
(`crawler/discovery/probe.py`, at most three requests) plus the offline domain rules. It makes no
request, opens no page and decides nothing for the operator — it sorts, so that a candidate our
own rules forbid never reaches the onboarding queue in the first place.

The kinds are the technically distinguishable subset of the typology in
`docs/research/creator-leak-sources.md`; the verdicts are that document's four policy outcomes.
Nothing here is a legal opinion: `coverable` means "nothing we recorded forbids it", never
"approved" — a site is still onboarded by hand, with an ADR 0007 review and a disabled `Site`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from crawler.blocking import AGE_SELF_DECLARATION, AGE_VERIFICATION_PROVIDER


class Coverage(StrEnum):
    """What our own rules allow for a candidate of this kind and this probe result."""

    #: Nothing recorded forbids an adapter. Still needs the ordinary human onboarding.
    COVERABLE = "coverable"
    #: We may record that an indexed page points here; we never fetch what it serves.
    LINK_INTELLIGENCE = "link_intelligence"
    #: A login, a CAPTCHA/challenge, a paywall, an age *verification* gate or a robots.txt
    #: disallow stands in front of it. Our hard rules forbid working around any of them.
    NOT_COVERABLE = "not_coverable"
    #: Held back for a legal reason, not a technical one (messaging-app previews, issue #60).
    PARKED = "parked"
    #: Not probed yet, or the probe never got far enough to say.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    kind: str
    coverage: Coverage
    reason: str


#: Probe `blocked_reason` fragments that mean a wall we may never work around (ADR 0007,
#: `crawler/blocking.py`). Matched case-insensitively as substrings, because the probe stores
#: both its own short markers ("login", "challenge") and `detect_block`'s longer sentences.
WALL_REASONS: tuple[tuple[str, str], ...] = (
    ("login", "login wall"),
    ("challenge", "CAPTCHA or challenge page"),
    ("captcha", "CAPTCHA or challenge page"),
    ("cloudflare block", "blocked by the site"),
    ("paywall", "paywall"),
    ("subscription", "paywall"),
)
#: Kinds decided by the offline domain rules (`crawler.discovery.domains.classify`) before any
#: probe: their coverage never depends on what a homepage looks like.
DOMAIN_RULE_COVERAGE: Mapping[str, tuple[Coverage, str]] = {
    "file_hoster": (
        Coverage.LINK_INTELLIGENCE,
        "file host: we record that a page points here, never fetch what it serves",
    ),
    "torrent": (
        Coverage.LINK_INTELLIGENCE,
        "torrent/P2P source: no page to crawl and no file URL to fetch (ADR 0013 amendment)",
    ),
    "social": (
        Coverage.PARKED,
        "messaging/social platform: parked pending review (issue #60)",
    ),
}
#: A post page carrying at least this many same-template full-size images reads as a gallery
#: rather than a video page. Deliberately low: recorded homepages are thin.
MIN_GALLERY_IMAGES = 6
#: Same-site links into a person's own page (`/model/*`, `/creator/*`, …) that make a site read as
#: organised around names rather than around uploads.
MIN_PROFILE_LINKS = 6
#: Thread/board links that make a site read as a forum.
MIN_FORUM_LINKS = 6


@dataclass(frozen=True)
class ProbeFacts:
    """Exactly the probe fields the classifier is allowed to read. Nothing here is page text."""

    domain_kind: str = "unknown"
    robots_allows: bool | None = None
    blocked_reason: str = ""
    cms_guess: str = "unknown"
    has_search: bool = False
    video_links_seen: int = 0
    profile_links_seen: int = 0
    forum_links_seen: int = 0
    gallery_images_seen: int = 0
    probed: bool = False


def _wall(reason: str) -> str:
    lower = reason.lower()
    for fragment, label in WALL_REASONS:
        if fragment in lower:
            return label
    return ""


def classify_probe(facts: ProbeFacts) -> Verdict:
    """The kind and the coverage verdict, from recorded probe facts alone.

    Order matters and is the order of our own rules: a domain rule that already decided the kind
    wins, then a wall we may never work around, then robots.txt, then what the homepage's
    *structure* said. An age gate is the one case that is neither: self-declaration is onboardable
    with the operator's per-site approval (`Site.age_confirmation`), a verification provider never
    is (ADR 0007 amendment 2026-09-15).
    """
    if decided := DOMAIN_RULE_COVERAGE.get(facts.domain_kind):
        coverage, reason = decided
        return Verdict(facts.domain_kind, coverage, reason)
    if not facts.probed:
        return Verdict(facts.domain_kind, Coverage.UNKNOWN, "not probed yet")
    if facts.blocked_reason.startswith(AGE_VERIFICATION_PROVIDER):
        return Verdict(
            facts.domain_kind,
            Coverage.NOT_COVERABLE,
            "age verification provider: never automated, with or without approval",
        )
    if label := _wall(facts.blocked_reason):
        return Verdict(facts.domain_kind, Coverage.NOT_COVERABLE, label)
    if facts.robots_allows is False:
        return Verdict(facts.domain_kind, Coverage.NOT_COVERABLE, "robots.txt disallows the site")
    if facts.robots_allows is None:
        return Verdict(facts.domain_kind, Coverage.UNKNOWN, "robots.txt could not be read")
    kind = _structure_kind(facts)
    if facts.blocked_reason.startswith(AGE_SELF_DECLARATION):
        return Verdict(
            kind,
            Coverage.COVERABLE,
            "self-declaration age gate: needs the operator's per-site approval "
            "(Site.age_confirmation, empty everywhere today)",
        )
    if facts.blocked_reason:
        return Verdict(kind, Coverage.UNKNOWN, f"probe stopped: {facts.blocked_reason}")
    if kind == "forum":
        return Verdict(
            kind,
            Coverage.NOT_COVERABLE,
            "forum: threads are behind registration, and we hold no account anywhere",
        )
    if kind == "unknown":
        return Verdict(kind, Coverage.UNKNOWN, "no structure a probe can judge; needs a human")
    return Verdict(kind, Coverage.COVERABLE, f"{kind} structure, no wall recorded")


def _structure_kind(facts: ProbeFacts) -> str:
    """The kind the homepage's structure suggests. Counts only — never a title or a URL."""
    if facts.forum_links_seen >= MIN_FORUM_LINKS:
        return "forum"
    if facts.cms_guess == "kvs" or facts.video_links_seen:
        # A tube whose links are organised around creator pages is still a tube to the crawler;
        # `name_index` is for a site that offers names and no video grammar at all.
        return "tube"
    if facts.gallery_images_seen >= MIN_GALLERY_IMAGES:
        return "gallery"
    if facts.profile_links_seen >= MIN_PROFILE_LINKS:
        return "name_index"
    return "unknown"


def blocks_onboarding(coverage: str) -> bool:
    """Is this verdict one the onboarding queue must never see approved?"""
    return coverage in {Coverage.NOT_COVERABLE, Coverage.PARKED}
