"""robots.txt: parsing (RFC 9309 incl. `*` and `$` wildcards) and a per-process cache.

The stdlib `urllib.robotparser` ignores wildcards, which several monitored sites use
(e.g. `Disallow: *mode=async*`), so rules are matched here.

Lives in `core` because both the crawler (site pages) and the media worker (media hosts, ADR 0007
amendment 2026-09-17) obey it; `crawler.robots` re-exports it.

`parse_robots` answers the only question a fetch asks — may we take this URL — and to do that it
**throws away every group that does not address us**. `describe_robots` is the other reading of the
same file, for tripwire T2 of `docs/legal/crawl-position.md` (issue #105): *what does this site
reserve, and against whom*. It is pure, it is computed from the body the crawler already fetched,
and it yields tokens and booleans only — never a path and never a line of the file.
"""

import hashlib
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from core.http import FetchResponse, ResponseTooLargeError

ROBOTS_MAX_AGE_S = 24 * 3600


class RobotsUnavailableError(Exception):
    """robots.txt could not be read (5xx, 429 or network error); the site must not be crawled."""


class InvalidRobotsUrlError(ValueError):
    """The origin cannot even be turned into a request (bad host, idna, control characters).

    A property of the URL that was asked about, not of the site: never a reason to pause the site.
    """


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class RobotsRules:
    """The rules that apply to our user agent on one origin."""

    rules: tuple[RobotsRule, ...] = ()
    crawl_delay_s: float | None = None

    def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        if path == "/robots.txt":
            return True
        best: RobotsRule | None = None
        for rule in self.rules:
            if not rule.regex.match(path):
                continue
            longer = best is None or len(rule.pattern) > len(best.pattern)
            tie_allows = best is not None and len(rule.pattern) == len(best.pattern) and rule.allow
            if longer or tie_allows:
                best = rule
        return best is None or best.allow


ALLOW_ALL = RobotsRules()


def product_token(user_agent: str) -> str:
    """Our own product token, lower-case: the name a robots.txt group would address us by."""
    return user_agent.split("/", 1)[0].strip().lower()


def _groups(text: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Every group of a robots.txt as (agent tokens, directive lines), in file order."""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    lines: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if lines:
                groups.append((agents, lines))
                agents, lines = [], []
            agents.append(value.lower())
        elif key in {"allow", "disallow", "crawl-delay"} and agents:
            lines.append((key, value))
    if agents:
        groups.append((agents, lines))
    return groups


def _rules_from(directives: list[tuple[str, str]]) -> RobotsRules:
    rules: list[RobotsRule] = []
    crawl_delay: float | None = None
    for key, value in directives:
        if key == "crawl-delay":
            try:
                crawl_delay = max(crawl_delay or 0.0, float(value))
            except ValueError:
                continue
        elif value:
            rules.append(RobotsRule(allow=key == "allow", pattern=value, regex=_compile(value)))
    return RobotsRules(rules=tuple(rules), crawl_delay_s=crawl_delay)


def parse_robots(text: str, user_agent: str) -> RobotsRules:
    """Rules for `user_agent` (its product token, e.g. `MarcelineBot`), falling back to `*`."""
    token = product_token(user_agent)
    groups = _groups(text)
    specific = [group_lines for names, group_lines in groups if token in names]
    fallback = [group_lines for names, group_lines in groups if "*" in names]
    selected = [item for group_lines in (specific or fallback) for item in group_lines]
    return _rules_from(selected)


def effective_delay_s(site_delay_s: float, rules: RobotsRules) -> float:
    return max(site_delay_s, rules.crawl_delay_s or 0.0)


# --- What does this file reserve, and against whom? (tripwire T2, issue #105) -------------------

#: Product tokens of the AI / TDM crawlers sites name when they reserve their content, lower-case.
#: **Data, not logic** — add a token when a site names one we do not know yet; nothing else changes.
#: Sources: the crawlers' own published documentation (OpenAI `GPTBot`, `OAI-SearchBot`,
#: `ChatGPT-User`; Anthropic `ClaudeBot`, `anthropic-ai`, `Claude-Web`; Google `Google-Extended`;
#: Common Crawl `CCBot`; Perplexity `PerplexityBot`; ByteDance `Bytespider`; Amazon `Amazonbot`;
#: Cohere `cohere-ai`; Meta `Meta-ExternalAgent`, `FacebookBot`; Apple `Applebot-Extended`;
#: Diffbot, Omgili, Timpi, YouBot, AI2Bot, PetalBot, `img2dataset`), plus the Internet Archive's
#: `ia_archiver`, which the measured sites list in the same breath. The two enabled sites carrying
#: such groups today (§ 3.1 of docs/legal/crawl-position.md, 2026-09-18) name nine and seven tokens,
#: all of them from this set.
AI_CRAWLER_TOKENS: frozenset[str] = frozenset(
    {
        "ai2bot",
        "amazonbot",
        "anthropic-ai",
        "applebot-extended",
        "bytespider",
        "ccbot",
        "chatgpt-user",
        "claude-searchbot",
        "claude-web",
        "claudebot",
        "cohere-ai",
        "diffbot",
        "facebookbot",
        "google-extended",
        "gptbot",
        "ia_archiver",
        "img2dataset",
        "meta-externalagent",
        "meta-externalfetcher",
        "oai-searchbot",
        "omgili",
        "omgilibot",
        "perplexitybot",
        "petalbot",
        "timpibot",
        "youbot",
    }
)

#: Response headers that carry a usage reservation of their own. `TDM-Reservation: 1` is the header
#: form of the TDM Reservation Protocol (W3C TDM Reservation Protocol 1.0); `X-Robots-Tag: noai` /
#: `noimageai` is the widely deployed convention. Only the **robots.txt** response is read for these
#: — the request every site expects a crawler to make. A page's headers are never scraped for them.
TDM_RESERVATION_HEADER = "tdm-reservation"
ROBOTS_TAG_HEADER = "x-robots-tag"
NO_AI_TOKENS = ("noai", "noimageai", "noml", "notrain", "noaitraining")
#: Two paths a group is probed with to decide whether it shuts the *whole* site for that agent: a
#: file that disallows `/` but allows one path back is not a full-site reservation.
_FULL_DISALLOW_PROBES = ("/", "/a/b.html")


@dataclass(frozen=True)
class RobotsFacts:
    """A small structured summary of one robots.txt response: whom it addresses, whom it shuts out
    of the whole site, and whether it reserves the site against text and data mining.

    Tokens, counters and booleans only — never a path and never a line of the file. The body is
    kept nowhere; `content_hash` is what tells a change from a re-read.
    """

    content_hash: str
    #: Every user-agent token the file addresses, lower-case, sorted.
    agents: tuple[str, ...] = ()
    #: Of those, the ones the file shuts out of the whole site.
    full_disallow_agents: tuple[str, ...] = ()
    #: A full-site disallow in a group that names **our own** product token.
    names_us: bool = False
    #: A full-site disallow in the `*` group — already obeyed by `parse_robots`, nothing new.
    names_wildcard: bool = False
    #: A full-site disallow in a group naming a known AI/TDM crawler that is not us.
    ai_tokens: tuple[str, ...] = ()
    #: Names of response headers that carried a reservation.
    reservation_headers: tuple[str, ...] = ()

    @property
    def reserved_against_us(self) -> bool:
        """Unambiguous: this site reserves itself against *us*, by name or by header."""
        return self.names_us or bool(self.reservation_headers)

    @property
    def third_party_reservation(self) -> bool:
        """A machine-readable reservation addressed to other companies' crawlers, not to us.

        It is recorded and reported and never acted on by the code; the operator decides.
        """
        return bool(self.ai_tokens)


def reservation_headers(headers: Mapping[str, str]) -> tuple[str, ...]:
    """Which of `headers` reserve the response against text and data mining (lower-case names)."""
    found: list[str] = []
    value = (headers.get(TDM_RESERVATION_HEADER) or "").strip()
    if value and value != "0":
        found.append(TDM_RESERVATION_HEADER)
    tag = (headers.get(ROBOTS_TAG_HEADER) or "").lower()
    if any(token in tag for token in NO_AI_TOKENS):
        found.append(ROBOTS_TAG_HEADER)
    return tuple(found)


def describe_robots(
    text: str, user_agent: str, headers: Mapping[str, str] | None = None
) -> RobotsFacts:
    """Summarise one robots.txt response for tripwire T2. Pure; makes no request of its own."""
    token = product_token(user_agent)
    agents: set[str] = set()
    shut_out: set[str] = set()
    for names, directives in _groups(text):
        agents.update(names)
        rules = _rules_from(directives)
        if all(not rules.can_fetch(path) for path in _FULL_DISALLOW_PROBES):
            shut_out.update(names)
    return RobotsFacts(
        content_hash=hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        agents=tuple(sorted(agents)),
        full_disallow_agents=tuple(sorted(shut_out)),
        names_us=token in shut_out,
        names_wildcard="*" in shut_out,
        ai_tokens=tuple(sorted((shut_out & AI_CRAWLER_TOKENS) - {token})),
        reservation_headers=reservation_headers(headers or {}),
    )


class RobotsCache:
    """Fetches robots.txt once per origin and keeps it for `max_age_s` seconds.

    Thread-safe: threads sharing one cache (the crawl runtime, ADR 0030) wait for each other per
    origin, so a robots.txt is fetched once however many threads need it at the same moment.
    """

    def __init__(
        self,
        fetch: Callable[[str, float], FetchResponse],
        user_agent: str,
        *,
        max_age_s: float = ROBOTS_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._user_agent = user_agent
        self._max_age_s = max_age_s
        self._clock = clock
        self._cache: dict[str, tuple[float, RobotsRules]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def rules_for(
        self,
        url: str,
        *,
        min_delay_s: float = 0.0,
        fetch: Callable[[str, float], FetchResponse] | None = None,
        observer: Callable[[str, RobotsFacts], None] | None = None,
    ) -> RobotsRules:
        """Cached rules for `url`'s origin, fetched first when missing or stale. `fetch` replaces
        the cache's own fetch for this call (a caller whose client differs per request).

        `observer` is handed `(origin, RobotsFacts)` when this call actually **fetched** the file —
        never on a cache hit, so it fires at most once per origin per `max_age_s` and costs no extra
        request (tripwire T2, issue #105). It is passed per call rather than held by the cache
        because the crawl runtime shares one cache between lanes and builds it elsewhere.
        """
        origin = _origin(url)
        fresh = self.cached(url)
        if fresh is not None:
            return fresh
        with self._lock_for(origin):
            fresh = self.cached(url)
            if fresh is not None:
                return fresh
            return self._fetch_rules(origin, min_delay_s, fetch, observer)

    def _lock_for(self, origin: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(origin, threading.Lock())

    def _fetch_rules(
        self,
        origin: str,
        min_delay_s: float,
        fetch: Callable[[str, float], FetchResponse] | None,
        observer: Callable[[str, RobotsFacts], None] | None = None,
    ) -> RobotsRules:
        try:
            response = (fetch or self._fetch)(f"{origin}/robots.txt", min_delay_s)
        except (httpx.HTTPError, ResponseTooLargeError) as exc:
            raise RobotsUnavailableError(f"robots.txt for {origin}: {exc}") from exc
        except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
            raise InvalidRobotsUrlError(f"robots.txt for {origin}: {exc}") from exc
        if response.status >= 500 or response.status == 429:
            raise RobotsUnavailableError(f"robots.txt for {origin}: HTTP {response.status}")
        readable = response.status < 400
        rules = parse_robots(response.text, self._user_agent) if readable else ALLOW_ALL
        self._cache[origin] = (self._clock(), rules)
        if observer is not None and readable:
            # The caller's own callback decides what to do with the facts and handles its own
            # errors: recording a reservation must never be the reason a fetch fails.
            observer(origin, describe_robots(response.text, self._user_agent, response.headers))
        return rules

    def clear(self) -> None:
        self._cache.clear()

    def cached(self, url: str) -> RobotsRules | None:
        """The fresh cached rules for `url`'s origin, or None; never fetches."""
        cached = self._cache.get(_origin(url))
        if cached is not None and self._clock() - cached[0] < self._max_age_s:
            return cached[1]
        return None

    def can_fetch(self, url: str) -> bool:
        return self.rules_for(url).can_fetch(url)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))
