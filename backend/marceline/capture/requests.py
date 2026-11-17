"""Which requests the capture browser may send (ADR 0016, amendment 2026-09-17).

Left to itself, a browser loads whatever the page asks for (issue #78): a player iframe on
another host and the file behind it, or a player on a path of the site itself, whether or not the
host's robots.txt allows those paths. The crawler and the media worker never send such a request
(ADR 0007 and its media-host amendment), so the browser must not either: a browser cannot become a
way around the crawler's own policy.

So every request the page makes is put to `RequestPolicy.refusal` before it leaves the browser,
exactly like the media worker's `before_request` hook:

- Resource types that cannot carry the video (images, fonts, …) are refused without a question;
  `capture.mjs` receives the set, so the rule lives here and is tested here.
- A document for a **subframe** (an embedded player) must come from an allowlisted host: the
  page's own site, an enabled `catalog.Site`, or a `SiteHost` of the page's site. Anything else —
  ad frames, pop-under widgets — is refused. This is also cost and malware hygiene.
- Every other request must be allowed by its host's robots.txt for our User-Agent. An unreadable
  robots.txt (5xx, 429, network error) refuses, 4xx allows (RFC 9309), as for media.

Refusing to load something is not anti-bot evasion: nothing about the browser is disguised.

**Reconstructing a refusal (issue #78 amendment 2026-09-18).** A count alone cannot answer "which
host blocked us" — an operator deciding whether to allowlist an embed host, or to look into a
site's robots.txt, needs the host itself. `RequestPolicy` remembers, per `(host, reason)`, how many
requests were refused and the *shape* of the paths involved, for the three reasons that name a host
worth reviewing (`EMBED_NOT_ALLOWLISTED`, `ROBOTS_DISALLOWED`, `ROBOTS_UNAVAILABLE` — never
`RESOURCE_TYPE`, which every visit trips for ordinary images and fonts, and never
`UNSUPPORTED_SCHEME`, whose URL has no meaningful host). `host_refusals()` returns that as plain
data, stored in `capture_run.diagnostics.refused_hosts` — never a full URL, id or token.

A path becomes a *pattern* by `core.url_shapes.path_pattern`, which lives in `core` because the
dead-shape breaker of ADR 0041 counts a site's 404 answers against exactly the same shapes: each
`/`-separated segment that looks like an identifier is replaced with `*`, so `/embed/482910`
becomes `/embed/*` and `/get_file/9f1c2b7a91e0.mp4` becomes `/get_file/*.mp4`, while `/embed/` or
`/watch` pass through unchanged. Read that module's docstring for the rule and its limits.
"""

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from core.robots import RobotsUnavailableError
from core.url_shapes import host_of, path_pattern

# Requests that cannot carry the video and mostly carry ads, trackers and decoration. Stylesheets
# are deliberately *not* here: players lay themselves out with CSS, and without it the trial's
# only "captured" player rendered 1264x15 px and another player stayed `visibility: hidden`.
BLOCKED_RESOURCE_TYPES = frozenset(
    {"image", "font", "websocket", "eventsource", "manifest", "texttrack", "other"}
)

RESOURCE_TYPE = "resource type"
EMBED_NOT_ALLOWLISTED = "embed host not allowlisted"
ROBOTS_DISALLOWED = "robots.txt"
ROBOTS_UNAVAILABLE = "robots.txt unavailable"
UNSUPPORTED_SCHEME = "unsupported scheme"


@dataclass(frozen=True)
class BrowserRequest:
    """One request the page is about to send, as the browser reports it."""

    url: str
    resource_type: str
    subframe: bool = False  # a document navigation for a frame other than the page itself


def _within(host: str, domain: str) -> bool:
    domain = domain.lower().removeprefix("www.")
    return bool(domain) and (host == domain or host.endswith(f".{domain}"))


def embed_host_allowed(host: str, allowed_domains: Iterable[str]) -> bool:
    """Whether a subframe from `host` may load: the host or a parent domain is allowlisted."""
    return any(_within(host, domain) for domain in allowed_domains)


def page_robots_refusal(url: str) -> dict[str, object]:
    """The reconstructable detail of the page's own robots.txt refusing a whole visit.

    Stored on `BrowserCaptureCandidate.refusal_detail`, never in `capture_run`: this skip never
    opened a browser and must stay retryable — `capture_run`'s emptiness is what ADR 0016's Scan
    once rule reads to decide that (`matching.capture_triage.captured_bases`).
    """
    return {"kind": ROBOTS_DISALLOWED, "host": host_of(url), "path": path_pattern(url)}


# Reasons that name a host worth an operator's review: allowlist it, or look at its robots.txt.
# Never RESOURCE_TYPE (every visit trips it for ordinary images/fonts) or UNSUPPORTED_SCHEME
# (its "host" is meaningless — data:, blob:, …).
HOST_REASONS = frozenset({EMBED_NOT_ALLOWLISTED, ROBOTS_DISALLOWED, ROBOTS_UNAVAILABLE})


@dataclass
class _HostTally:
    """One host's tally for one refusal reason within a visit."""

    count: int = 0
    paths: set[str] = field(default_factory=set)


@dataclass
class RequestPolicy:
    """Decides every request of one visit; remembers what it refused, for the skip reason.

    `robots_allows(url)` answers from the host's robots.txt (fetching it once per origin) and
    raises `RobotsUnavailableError` when it cannot be read.
    """

    allowed_embed_domains: frozenset[str]
    robots_allows: Callable[[str], bool]
    refused: Counter[tuple[str, str]] = field(default_factory=Counter)
    refused_embed_hosts: set[str] = field(default_factory=set)
    # Hosts whose robots.txt could not be read: asked once per visit, not once per request. A
    # page pulls dozens of scripts from ad hosts, and each unreadable robots.txt costs the HTTP
    # client's full retry budget — that alone made a refused visit take 94 s (issue #78).
    unreadable_hosts: set[str] = field(default_factory=set)
    # Per (host, reason), for HOST_REASONS only: count and the path shapes seen (issue #78
    # amendment 2026-09-18) — what makes a refusal reconstructable after the fact.
    _host_refusals: dict[tuple[str, str], _HostTally] = field(default_factory=dict)

    def refusal(self, request: BrowserRequest) -> str | None:
        """None when the request may be sent, else the reason it is refused."""
        reason = self._reason(request)
        if reason is not None and reason != RESOURCE_TYPE:
            self.refused[(reason, "subframe" if request.subframe else request.resource_type)] += 1
            if request.subframe:
                self.refused_embed_hosts.add(host_of(request.url))
            if reason in HOST_REASONS:
                host = host_of(request.url)
                if host:
                    tally = self._host_refusals.setdefault((host, reason), _HostTally())
                    tally.count += 1
                    tally.paths.add(path_pattern(request.url))
        return reason

    def _reason(self, request: BrowserRequest) -> str | None:
        if request.resource_type in BLOCKED_RESOURCE_TYPES:
            return RESOURCE_TYPE
        if urlsplit(request.url).scheme not in ("http", "https"):
            return UNSUPPORTED_SCHEME
        host = host_of(request.url)
        if request.subframe and not embed_host_allowed(host, self.allowed_embed_domains):
            return EMBED_NOT_ALLOWLISTED
        if host in self.unreadable_hosts:
            return ROBOTS_UNAVAILABLE
        try:
            allowed = self.robots_allows(request.url)
        except RobotsUnavailableError:
            self.unreadable_hosts.add(host)
            return ROBOTS_UNAVAILABLE
        return None if allowed else ROBOTS_DISALLOWED

    def embed_refusal(self) -> str | None:
        """Why an **embedded player** was refused, if one was.

        Only a subframe from an allowlisted host — the page's own site, an enabled site, one of
        its hosts — can be the player, and then robots.txt is the reason it was refused. A
        subframe from an unknown host is an ad or a pop-under widget: every page of the first
        re-run had one, and counting those as the player hid the real cause (issue #78).
        """
        for reason in (ROBOTS_DISALLOWED, ROBOTS_UNAVAILABLE):
            if self.refused[(reason, "subframe")]:
                return reason
        return None

    def robots_refused(self, resource_types: Iterable[str]) -> bool:
        """Whether any request of these types was refused by a robots.txt."""
        types = set(resource_types)
        return any(
            count and reason == ROBOTS_DISALLOWED and kind in types
            for (reason, kind), count in self.refused.items()
        )

    def summary(self) -> dict[str, int]:
        """Refusal counts as `reason / kind` (stored in `capture_run`, never a URL)."""
        refused = sorted(self.refused.items())
        return {f"{reason} / {kind}": count for (reason, kind), count in refused}

    def host_refusals(self) -> list[dict[str, object]]:
        """Per refused host: the reason, how many requests, and the path shapes — never a URL.

        Covers `HOST_REASONS` only (embed-not-allowlisted, robots-disallowed, robots-unavailable):
        the reasons an operator can act on by reviewing a host. Stored in
        `capture_run.diagnostics.refused_hosts` (issue #78 amendment 2026-09-18) so a blocked
        embed host, or one whose robots.txt disallowed us, can be found again after the fact.
        """
        return [
            {"host": host, "reason": reason, "count": tally.count, "paths": sorted(tally.paths)}
            for (host, reason), tally in sorted(self._host_refusals.items())
        ]
