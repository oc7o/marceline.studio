"""Bounded reconnaissance exception; bounded redirects, no retries, links or media."""

from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings
from django.utils import timezone
from selectolax.lexbor import LexborHTMLParser

from catalog.models import HostRole
from catalog.request_accounting import PROBE, RequestAccounting
from core.http import FetchResponse, PoliteClient, RequestLabel, ResponseTooLargeError
from crawler.blocking import (
    AGE_GATE_MARKERS,
    CHALLENGE_MARKERS,
    classify_age_gate,
    detect_block,
)
from crawler.discovery.domains import normalize_domain
from crawler.discovery.services import (
    apply_rules,
    mark_mirror,
    mirror_domain,
    record_coverage,
)
from crawler.discovery.structure import forum_paths, gallery_image_count, profile_paths
from crawler.discovery.video_links import video_detail_paths
from crawler.models import SourceCandidate
from crawler.robots import RobotsRules, parse_robots

PROBE_DELAY_S = 5.0
MAX_REDIRECT_HOPS = 3
# Logical requests per candidate: robots.txt, homepage, and one robots.txt for the host a
# homepage redirect moves to within the same registrable domain.
MAX_PROBE_REQUESTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
UNCHECKED_HOST = "redirect requires unchecked host robots"

RobotsLoader = Callable[[str], RobotsRules | None]


def fetch(
    client: PoliteClient,
    candidate: SourceCandidate,
    url: str,
    *,
    rules: RobotsRules | None = None,
    load_robots: RobotsLoader | None = None,
) -> FetchResponse | None:
    """Follow checked redirects; with rules, `load_robots` may resolve another host's policy."""
    checked_origin = urlsplit(url)[:2]
    for hop in range(MAX_REDIRECT_HOPS + 1):
        delay = max(PROBE_DELAY_S, rules.crawl_delay_s or 0) if rules else PROBE_DELAY_S
        response = client.get(url, min_delay_s=delay)
        candidate.http_status = response.status
        if response.status not in REDIRECT_STATUSES:
            return response
        location = response.headers.get("location", "")
        try:
            target = urljoin(url, location)
        except ValueError:
            candidate.blocked_reason = "redirect invalid"
            return None
        domain = normalize_domain(target)
        candidate.evidence["redirects_to"] = target[:2000]
        if not location or not domain:
            candidate.blocked_reason = "redirect invalid"
            return None
        # Only a redirect *off* the candidate's own registrable domain can reveal a mirror. A
        # plain www<->apex hop stays inside it, and an approved candidate has a Site of its own
        # domain, which would otherwise match itself (2026-09-16).
        if domain != candidate.domain and (mirror := mirror_domain(domain, by_name=False)):
            mark_mirror(candidate, mirror)
            candidate.blocked_reason = "redirect to existing Site"
            return None
        if domain != candidate.domain:
            candidate.blocked_reason = "redirect outside registrable domain"
            return None
        if hop == MAX_REDIRECT_HOPS:
            candidate.blocked_reason = "redirect limit reached"
            return None
        if rules is not None:
            if urlsplit(target)[:2] != checked_origin:
                if load_robots is None:
                    candidate.blocked_reason = UNCHECKED_HOST
                    return None
                target_rules = load_robots(target)
                if target_rules is None:
                    return None
                candidate.evidence["redirects_to"] = target[:2000]
                rules, checked_origin = target_rules, urlsplit(target)[:2]
            if not rules.can_fetch(target):
                candidate.robots_allows = False
                candidate.blocked_reason = "redirect disallowed by robots"
                return None
        url = target
    return None


def robots_rules(
    client: PoliteClient, candidate: SourceCandidate, url: str
) -> tuple[RobotsRules, str] | None:
    """One logical robots.txt request for url's origin; returns rules and the final origin."""
    parts = urlsplit(url)
    robots = fetch(client, candidate, f"{parts.scheme}://{parts.netloc}/robots.txt")
    if robots is None:
        return None
    candidate.http_status = robots.status
    if 300 <= robots.status < 400 or robots.status == 429 or robots.status >= 500:
        candidate.blocked_reason = "robots unavailable"
        return None
    reason = block_reason(robots.status, dict(robots.headers), robots.text, robots.url)
    if reason:
        candidate.blocked_reason = reason
        return None
    final = urlsplit(robots.url)
    rules = parse_robots(robots.text if robots.status < 400 else "", settings.CRAWLER_USER_AGENT)
    return rules, f"{final.scheme}://{final.netloc}"


def probe(
    candidate: SourceCandidate,
    *,
    transport: httpx.BaseTransport | None = None,
    force: bool = False,
) -> None:
    """Bounded reconnaissance for one candidate.

    Bulk runs only touch proposed unknown/tube rows. `force` is for the operator naming a single
    domain: an approved domain still needs its probe before anyone writes an adapter for it, and
    a candidate that was approved long ago may have changed since.
    """
    if not force and (candidate.status != "proposed" or candidate.kind not in {"unknown", "tube"}):
        return
    # Offline rules classify *unreviewed* rows. A reviewed candidate keeps the operator's
    # decision: re-applying the rules here would ignore an approved domain as a "mirror" of the
    # Site its own approval created (2026-09-16).
    if candidate.status == "proposed" and apply_rules(candidate):
        candidate.save()
        return
    candidate.evidence.pop("redirects_to", None)
    candidate.evidence.pop("age_notice", None)
    candidate.probed_at = timezone.now()
    candidate.robots_allows = None
    candidate.http_status = None
    candidate.blocked_reason = ""
    candidate.title = ""
    candidate.cms_guess = "unknown"
    candidate.has_search = False
    candidate.video_links_seen = 0
    candidate.profile_links_seen = 0
    candidate.forum_links_seen = 0
    candidate.gallery_images_seen = 0
    try:
        # Redirects are returned, not followed: `fetch` checks and paces each hop explicitly.
        with PoliteClient(
            settings.CRAWLER_USER_AGENT,
            max_retries=0,
            transport=transport,
            follow_redirects=False,
            # A candidate is no site yet: its own server, counted, capped by MAX_PROBE_REQUESTS.
            observer=RequestAccounting(),
            label=RequestLabel(PROBE, role=HostRole.ORIGIN, enforce=False),
        ) as client:
            requests = 1
            first = robots_rules(client, candidate, "https://" + candidate.domain)
            if first is None:
                return
            rules, origin = first
            candidate.robots_allows = rules.can_fetch(origin + "/")
            if not candidate.robots_allows:
                return

            def target_rules(url: str) -> RobotsRules | None:
                # Rules apply to the redirect target's host (RFC 9309 initial authority).
                nonlocal requests
                if requests >= MAX_PROBE_REQUESTS:
                    candidate.blocked_reason = UNCHECKED_HOST
                    return None
                requests += 1
                result = robots_rules(client, candidate, url)
                return result[0] if result else None

            requests += 1
            response = fetch(client, candidate, origin + "/", rules=rules, load_robots=target_rules)
            if response is None:
                return
            candidate.http_status = response.status
            candidate.blocked_reason = block_reason(
                response.status, dict(response.headers), response.text, response.url
            )
            if candidate.blocked_reason or not 200 <= response.status < 300:
                return
            if any(marker in response.text.lower() for marker in AGE_GATE_MARKERS):
                candidate.evidence["age_notice"] = "client-side overlay, full page served (no gate)"
            inspect_homepage(candidate, response.text, response.url)
    except (httpx.HTTPError, ResponseTooLargeError):
        candidate.blocked_reason = "request failed"
    finally:
        # Every exit goes through here, so a probe that stopped at a wall is classified too.
        record_coverage(candidate)
        candidate.save()


def inspect_homepage(candidate: SourceCandidate, html: str, url: str) -> None:
    tree = LexborHTMLParser(html)
    title = tree.css_first("title")
    candidate.title = title.text()[:1000] if title else ""
    lower = html.lower()
    candidate.cms_guess = (
        "kvs"
        if any(marker in lower for marker in ("kt_player", "/get_file/", "kernelteamvideosharing"))
        else "wordpress"
        if "wp-content" in lower
        else "unknown"
    )
    candidate.has_search = bool(
        tree.css(
            'form input[type="search"], form input[name="s"], form input[name="q"], '
            'form[action*="search"], a[href*="?s="], a[href*="/search"]'
        )
    )
    # `video_detail_paths` (crawler/discovery/video_links.py) covers the URL grammars our own
    # site adapters actually see — not just `/video/`, `/videos/`, `/watch/` verbatim (issue #23).
    candidate.video_links_seen = len(video_detail_paths(html, url))
    # Three more counters off the same HTML — no extra request (issue #118).
    candidate.profile_links_seen = len(profile_paths(html, url))
    candidate.forum_links_seen = len(forum_paths(html, url))
    candidate.gallery_images_seen = gallery_image_count(html, url)
    if candidate.cms_guess == "kvs" or candidate.video_links_seen:
        candidate.kind = "tube"


def block_reason(status: int, headers: dict[str, str], body: str, url: str) -> str:
    reason = detect_block(status, headers, body, url)
    lower = body[:50_000].lower()
    if reason:
        return reason
    if any(marker.lower() in lower for marker in CHALLENGE_MARKERS):
        return "challenge"
    tree = LexborHTMLParser(body)
    if tree.css_first('input[type="password"]'):
        return "login"
    # `age_self_declaration` is onboardable with operator approval; providers never are.
    return classify_age_gate(body) or ""
