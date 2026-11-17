import httpx
import pytest

from core.http import FetchResponse
from core.robots import (
    InvalidRobotsUrlError,
    RobotsCache,
    RobotsUnavailableError,
    effective_delay_s,
    parse_robots,
)

UA = "MarcelineBot/0.1"

SITE01_ROBOTS = """User-agent: *
Disallow: /player/*
Disallow: /embed/*
Disallow: /login/
Disallow: *?action=*
Disallow: *mode=async*
Sitemap: https://site01.example/sitemap.xml
"""


def test_wildcard_rules() -> None:
    rules = parse_robots(SITE01_ROBOTS, UA)

    assert not rules.can_fetch("https://site01.example/embed/2099")
    assert not rules.can_fetch("https://site01.example/latest-updates/?mode=async&function=get_block")
    assert not rules.can_fetch("https://site01.example/video/1/x/?action=flag")
    assert rules.can_fetch("https://site01.example/video/2099/lena-hartwell/")
    assert rules.can_fetch("https://site01.example/robots.txt")


def test_longest_match_wins_and_allow_wins_ties() -> None:
    rules = parse_robots(
        "User-agent: *\nDisallow: /private\nAllow: /private/public\nDisallow: /tie\nAllow: /tie\n",
        UA,
    )

    assert not rules.can_fetch("https://s.test/private/x")
    assert rules.can_fetch("https://s.test/private/public/x")
    assert rules.can_fetch("https://s.test/tie")


def test_dollar_anchors_the_end() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /*.php$\n", UA)

    assert not rules.can_fetch("https://s.test/a.php")
    assert rules.can_fetch("https://s.test/a.php?x=1")


def test_specific_group_replaces_star_group() -> None:
    rules = parse_robots(
        "User-agent: *\nDisallow: /\n\nUser-agent: MarcelineBot\nDisallow: /nope\nCrawl-delay: 3\n",
        UA,
    )

    assert rules.can_fetch("https://s.test/ok")
    assert not rules.can_fetch("https://s.test/nope")
    assert rules.crawl_delay_s == 3.0


def test_crawl_delay_raises_effective_delay() -> None:
    rules = parse_robots("User-agent: *\nCrawl-delay: 10\n", UA)

    assert effective_delay_s(5.0, rules) == 10.0
    assert effective_delay_s(12.0, rules) == 12.0


def _response(status: int, body: str = "") -> FetchResponse:
    return FetchResponse(
        url="https://s.test/robots.txt", status=status, headers={}, body=body.encode()
    )


def test_cache_fetches_once_per_origin_and_allows_all_on_404() -> None:
    fetched: list[tuple[str, float]] = []

    def fetch(url: str, delay: float) -> FetchResponse:
        fetched.append((url, delay))
        return _response(404)

    cache = RobotsCache(fetch, UA)

    assert cache.rules_for("https://s.test/a", min_delay_s=5).can_fetch("https://s.test/a")
    assert cache.can_fetch("https://s.test/b")
    assert fetched == [("https://s.test/robots.txt", 5)]


def test_cache_expires() -> None:
    now = [0.0]
    fetched: list[str] = []

    def fetch(url: str, delay: float) -> FetchResponse:
        fetched.append(url)
        return _response(200, "User-agent: *\nDisallow: /x\n")

    cache = RobotsCache(fetch, UA, max_age_s=60, clock=lambda: now[0])
    assert cache.cached("https://s.test/x") is None
    cache.rules_for("https://s.test/")
    rules = cache.cached("https://S.test/x")
    assert rules is not None and not rules.can_fetch("https://s.test/x")
    assert cache.cached("https://other.test/x") is None
    now[0] = 61.0
    assert cache.cached("https://s.test/x") is None
    cache.rules_for("https://s.test/")

    assert len(fetched) == 2


def test_cache_raises_when_robots_is_unavailable() -> None:
    cache = RobotsCache(lambda url, delay: _response(503), UA)

    with pytest.raises(RobotsUnavailableError):
        cache.rules_for("https://s.test/")


def test_cache_reports_an_unrequestable_origin_as_invalid_not_unavailable() -> None:
    def fetch(url: str, delay: float) -> FetchResponse:
        raise httpx.InvalidURL("Invalid port")

    with pytest.raises(InvalidRobotsUrlError):
        RobotsCache(fetch, UA).rules_for("https://s.test/")
