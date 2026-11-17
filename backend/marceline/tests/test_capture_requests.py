"""The capture browser's request policy (issue #78): pure, no network, no database."""

import pytest

from core.robots import RobotsUnavailableError
from marceline.capture.requests import (
    BLOCKED_RESOURCE_TYPES,
    EMBED_NOT_ALLOWLISTED,
    RESOURCE_TYPE,
    ROBOTS_DISALLOWED,
    ROBOTS_UNAVAILABLE,
    UNSUPPORTED_SCHEME,
    BrowserRequest,
    RequestPolicy,
    embed_host_allowed,
    page_robots_refusal,
)


def policy(
    disallowed: tuple[str, ...] = (),
    unavailable: tuple[str, ...] = (),
    asked: list[str] | None = None,
) -> RequestPolicy:
    def robots_allows(url: str) -> bool:
        if asked is not None:
            asked.append(url)
        if any(host in url for host in unavailable):
            raise RobotsUnavailableError(url)
        return not any(part in url for part in disallowed)

    return RequestPolicy(
        allowed_embed_domains=frozenset({"example.test", "www.embed.test"}),
        robots_allows=robots_allows,
    )


def test_players_keep_their_stylesheets() -> None:
    """Without CSS a player rendered 1264x15 px and another player stayed hidden (issue #78)."""
    assert "stylesheet" not in BLOCKED_RESOURCE_TYPES
    assert {"image", "font"} <= BLOCKED_RESOURCE_TYPES


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("example.test", True),
        ("www.example.test", True),
        ("player.example.test", True),
        ("embed.test", True),  # "www." on a site domain is not part of its identity
        ("cdn.embed.test", True),
        ("notexample.test", False),
        ("example.test.evil.org", False),
        ("ads.example.org", False),
    ],
)
def test_embed_hosts_are_the_allowlisted_domains_and_their_subdomains(
    host: str, allowed: bool
) -> None:
    assert embed_host_allowed(host, {"example.test", "www.embed.test"}) is allowed


def test_refusals_are_counted_by_reason_and_kind_never_by_url() -> None:
    rules = policy(disallowed=("/get/", "/embed/"), unavailable=("down.example.net",))

    decisions = [
        rules.refusal(BrowserRequest("https://example.test/poster.jpg", "image")),
        rules.refusal(BrowserRequest("https://example.test/get/player", "xhr")),
        rules.refusal(BrowserRequest("https://down.example.net/a.ts", "media")),
        rules.refusal(BrowserRequest("https://ads.example.org/x", "document", subframe=True)),
        rules.refusal(BrowserRequest("https://embed.test/embed/1", "document", subframe=True)),
        rules.refusal(BrowserRequest("https://ads.example.org/x.js", "script")),
        rules.refusal(BrowserRequest("data:text/plain,x", "fetch")),
    ]

    assert decisions == [
        RESOURCE_TYPE,
        ROBOTS_DISALLOWED,
        ROBOTS_UNAVAILABLE,
        EMBED_NOT_ALLOWLISTED,
        ROBOTS_DISALLOWED,
        None,  # a third-party script its robots.txt allows is not the allowlist's business
        UNSUPPORTED_SCHEME,
    ]
    assert rules.embed_refusal() == ROBOTS_DISALLOWED
    assert rules.robots_refused(("xhr",)) is True
    assert rules.robots_refused(("media",)) is False  # unavailable is not "disallowed"
    assert "http" not in " ".join(rules.summary())
    assert rules.refused_embed_hosts == {"ads.example.org", "embed.test"}
    # Reconstructable per host (issue #78 amendment): reason, count and path shapes, never a URL.
    assert rules.host_refusals() == [
        {"host": "ads.example.org", "reason": EMBED_NOT_ALLOWLISTED, "count": 1, "paths": ["/x"]},
        {"host": "down.example.net", "reason": ROBOTS_UNAVAILABLE, "count": 1, "paths": ["/a.ts"]},
        {
            "host": "embed.test",
            "reason": ROBOTS_DISALLOWED,
            "count": 1,
            "paths": ["/embed/*"],
        },
        {
            "host": "example.test",
            "reason": ROBOTS_DISALLOWED,
            "count": 1,
            "paths": ["/get/player"],
        },
    ]
    # RESOURCE_TYPE (the poster image) and UNSUPPORTED_SCHEME (the data: URL) never name a host.
    hosts_and_reasons = {(row["host"], row["reason"]) for row in rules.host_refusals()}
    assert not any(reason in (RESOURCE_TYPE, UNSUPPORTED_SCHEME) for _, reason in hosts_and_reasons)


def test_host_refusals_tally_repeated_requests_and_keep_every_path_shape() -> None:
    rules = policy(disallowed=("/get/",))

    rules.refusal(BrowserRequest("https://example.test/get/1", "xhr"))
    rules.refusal(BrowserRequest("https://example.test/get/2", "xhr"))
    rules.refusal(BrowserRequest("https://example.test/other/abc", "xhr"))  # allowed, not tallied

    assert rules.host_refusals() == [
        {
            "host": "example.test",
            "reason": ROBOTS_DISALLOWED,
            "count": 2,
            "paths": ["/get/*"],
        }
    ]


# The shaping itself is `core.url_shapes` and is tested in core/tests/test_url_shapes.py.
# What belongs here is that a refusal is stored as a shape and never as a URL.
def test_page_robots_refusal_names_host_and_pattern_never_the_full_url() -> None:
    detail = page_robots_refusal("https://example.test/videos/482910?ref=xyz")

    assert detail == {"kind": ROBOTS_DISALLOWED, "host": "example.test", "path": "/videos/*"}
    assert "http" not in str(detail)
    assert "ref=xyz" not in str(detail)


def test_an_ad_frame_is_not_the_embedded_player() -> None:
    """Every page of the first re-run had one, and it hid the real cause (issue #78)."""
    rules = policy()

    assert (
        rules.refusal(BrowserRequest("https://ads.example.org/x", "document", subframe=True))
        == EMBED_NOT_ALLOWLISTED
    )

    assert rules.embed_refusal() is None  # an unknown host is an ad, not the player
    assert rules.summary() == {"embed host not allowlisted / subframe": 1}


def test_an_unreadable_robots_txt_is_asked_for_once_per_visit() -> None:
    """A page pulls dozens of scripts from ad hosts; each failed robots.txt costs retries."""
    asked: list[str] = []
    rules = policy(unavailable=("down.example.net",), asked=asked)

    for index in range(5):
        assert (
            rules.refusal(BrowserRequest(f"https://down.example.net/{index}.js", "script"))
            == ROBOTS_UNAVAILABLE
        )

    assert len(asked) == 1
    assert rules.refused[(ROBOTS_UNAVAILABLE, "script")] == 5
