"""What a robots.txt reserves, and against whom (tripwire T2, issue #105).

The bodies below are **synthetic**, but two of them have the shape of what was measured on the
enabled sites on 2026-09-18 (§ 3.1 of docs/legal/crawl-position.md): a standard KVS-style template
with a `*` group of ordinary path disallows, followed by `User-agent: <token>` / `Disallow: /`
groups naming other companies' AI crawlers. No site is named here; the shapes are what matters.
"""

from core.http import FetchResponse
from core.robots import (
    AI_CRAWLER_TOKENS,
    RobotsCache,
    describe_robots,
    parse_robots,
    reservation_headers,
)

UA = "MarcelineBot/0.1"

#: The measured shape: an ordinary `*` group, then named AI crawlers shut out of the whole site.
#: Nothing addresses us, and nothing shuts out `*`.
NAMED_AI_ONLY = """User-agent: *
Disallow: /search/
Disallow: /login/
Disallow: *mode=async*
Sitemap: https://s.test/sitemap.xml

User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: ClaudeBot
Disallow: /

User-agent: anthropic-ai
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: PerplexityBot
Disallow: /

User-agent: Bytespider
Disallow: /

User-agent: Amazonbot
Disallow: /

User-agent: cohere-ai
Disallow: /
"""

#: The day that changes: the same file, with a group naming us.
NAMES_US = (
    NAMED_AI_ONLY
    + """
User-agent: MarcelineBot
Disallow: /
"""
)

#: The other way it could change: the `*` group itself shuts the site.
NAMES_WILDCARD = """User-agent: *
Disallow: /
"""


def test_a_reservation_addressed_to_other_crawlers_is_seen_but_does_not_name_us() -> None:
    facts = describe_robots(NAMED_AI_ONLY, UA)

    assert facts.third_party_reservation
    assert not facts.names_us
    assert not facts.names_wildcard
    assert not facts.reserved_against_us
    assert set(facts.ai_tokens) == {
        "amazonbot",
        "anthropic-ai",
        "bytespider",
        "ccbot",
        "claudebot",
        "cohere-ai",
        "google-extended",
        "gptbot",
        "perplexitybot",
    }
    assert facts.ai_tokens == tuple(sorted(facts.ai_tokens))
    assert set(facts.ai_tokens) <= AI_CRAWLER_TOKENS
    # …and the rules that actually bind us are the `*` group's, unchanged.
    rules = parse_robots(NAMED_AI_ONLY, UA)
    assert rules.can_fetch("https://s.test/video/1/")
    assert not rules.can_fetch("https://s.test/search/x")


def test_a_group_naming_us_is_unambiguous() -> None:
    facts = describe_robots(NAMES_US, UA)

    assert facts.names_us
    assert facts.reserved_against_us
    assert "marcelinebot" in facts.full_disallow_agents
    # Our own token never appears as a "third-party AI token".
    assert "marcelinebot" not in facts.ai_tokens
    # And the ordinary parser already refuses every URL, because the group that names us wins.
    assert not parse_robots(NAMES_US, UA).can_fetch("https://s.test/video/1/")


def test_a_wildcard_reservation_is_recorded_and_already_obeyed() -> None:
    facts = describe_robots(NAMES_WILDCARD, UA)

    assert facts.names_wildcard
    assert not facts.names_us
    assert not facts.reserved_against_us
    # "Already obeyed" is the whole reason it needs no new enforcement:
    assert not parse_robots(NAMES_WILDCARD, UA).can_fetch("https://s.test/video/1/")
    assert not parse_robots(NAMES_WILDCARD, UA).can_fetch("https://s.test/")


def test_a_carve_out_is_still_a_reservation_but_an_open_site_is_not() -> None:
    """`Disallow: /` with one path allowed back still reserves the site; `Allow: /` does not.

    The probes are ordinary paths, so a file that shuts everything except `/public/` is recorded as
    a reservation — which is what it is — while the pattern some sites use to *undo* a template
    (`Disallow: /` followed by `Allow: /`, where the allow wins the tie) is not.
    """
    carve_out = "User-agent: GPTBot\nDisallow: /\nAllow: /public/\n"
    reopened = "User-agent: GPTBot\nDisallow: /\nAllow: /\n"

    assert describe_robots(carve_out, UA).ai_tokens == ("gptbot",)
    assert describe_robots(reopened, UA).ai_tokens == ()


def test_a_file_with_no_reservation_says_so() -> None:
    facts = describe_robots("User-agent: *\nDisallow: /search/\n", UA)

    assert not facts.third_party_reservation
    assert not facts.names_us
    assert not facts.names_wildcard
    assert facts.agents == ("*",)
    assert facts.full_disallow_agents == ()


def test_the_content_hash_changes_only_with_the_content() -> None:
    first = describe_robots(NAMED_AI_ONLY, UA).content_hash

    assert describe_robots(NAMED_AI_ONLY, UA).content_hash == first
    assert describe_robots(NAMES_US, UA).content_hash != first
    assert len(first) == 64


def test_reservation_headers_are_read_from_the_robots_response() -> None:
    assert reservation_headers({"tdm-reservation": "1"}) == ("tdm-reservation",)
    assert reservation_headers({"tdm-reservation": "0"}) == ()
    assert reservation_headers({"x-robots-tag": "noai, noimageai"}) == ("x-robots-tag",)
    assert reservation_headers({"x-robots-tag": "noindex"}) == ()
    assert reservation_headers({}) == ()


def test_a_header_reserves_the_site_against_us_even_with_a_harmless_file() -> None:
    facts = describe_robots("User-agent: *\nDisallow:\n", UA, {"tdm-reservation": "1"})

    assert facts.reserved_against_us
    assert facts.reservation_headers == ("tdm-reservation",)
    assert not facts.names_us


def test_the_cache_hands_the_facts_over_once_per_fetch_and_never_on_a_hit() -> None:
    seen: list[tuple[str, str]] = []

    def fetch(url: str, delay: float) -> FetchResponse:
        return FetchResponse(
            url=url, status=200, headers={"tdm-reservation": "1"}, body=NAMED_AI_ONLY.encode()
        )

    cache = RobotsCache(fetch, UA)
    cache.rules_for(
        "https://s.test/a",
        observer=lambda origin, facts: seen.append((origin, facts.content_hash)),
    )
    cache.rules_for(
        "https://s.test/b",
        observer=lambda origin, facts: seen.append((origin, facts.content_hash)),
    )

    assert [origin for origin, _ in seen] == ["https://s.test"]


def test_the_cache_does_not_describe_a_robots_txt_it_could_not_read() -> None:
    seen: list[str] = []

    cache = RobotsCache(lambda url, delay: FetchResponse(url, 404, {}, b""), UA)
    cache.rules_for("https://s.test/a", observer=lambda origin, facts: seen.append(origin))

    assert seen == []
