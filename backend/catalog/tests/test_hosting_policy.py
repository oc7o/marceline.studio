"""The pure policies of hoster intelligence: jurisdiction, CDN detection, takedown-chance band."""

import pytest

from catalog.hosting.cdn import detect_cdn
from catalog.hosting.cooperation import (
    MIN_DECIDED_NOTICES,
    TAKEDOWN_CHANCES,
    Cooperation,
    takedown_chance,
)
from catalog.hosting.jurisdiction import EU_COUNTRIES, jurisdiction_of


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("DE", "eu"),
        ("nl", "eu"),
        (" FR ", "eu"),
        ("RE", "eu"),  # Réunion is an outermost region: EU territory
        ("AX", "eu"),
        ("US", "us"),
        ("PR", "us"),
        ("CH", "other"),  # Europe, not the Union
        ("GB", "other"),
        ("NO", "other"),  # EEA, but DSA notices are an EU instrument
        ("RU", "other"),
        ("NC", "other"),  # an overseas territory, not part of the Union
        ("EU", "unknown"),  # RIPE's pan-European code names no country
        ("ZZ", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
        ("DEU", "unknown"),
        ("D1", "unknown"),
    ],
)
def test_country_to_jurisdiction(country: str | None, expected: str) -> None:
    assert jurisdiction_of(country) == expected


def test_every_member_state_is_eu() -> None:
    members = {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    }  # fmt: skip
    assert len(members) == 27
    assert members <= EU_COUNTRIES
    assert {jurisdiction_of(code) for code in members} == {"eu"}


@pytest.mark.parametrize(
    ("asn", "address", "expected"),
    [
        (13335, "104.16.0.1", "cloudflare"),
        (13335, None, "cloudflare"),
        (54113, "151.101.1.1", "fastly"),
        (57724, "190.115.16.1", "ddos_guard"),
        # The AS lookup failed, but the address is in a range Cloudflare publishes.
        (None, "172.67.1.1", "cloudflare"),
        (None, "2606:4700::6810:1", "cloudflare"),
        (24940, "5.9.0.1", None),  # Hetzner hosts; it does not proxy
        (16509, "13.32.0.1", None),  # AWS as a whole is not a CDN
        (None, "5.9.0.1", None),
        (None, None, None),
        (None, "not-an-address", None),
    ],
)
def test_cdn_detection(asn: int | None, address: str | None, expected: str | None) -> None:
    assert detect_cdn(asn, address) == expected


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        # No notices exist today: every provider is unknown.
        (Cooperation(), "unknown"),
        # Two decided notices are an anecdote, however good; undecided ones do not count.
        (Cooperation(notices_sent=2, decided=2, removed=2), "unknown"),
        (Cooperation(notices_sent=9, decided=2, removed=2), "unknown"),
        (Cooperation(notices_sent=20, decided=20, removed=19), "exemplary"),
        (Cooperation(notices_sent=20, decided=20, removed=18), "very_good"),
        (Cooperation(notices_sent=10, decided=10, removed=8), "very_good"),
        (Cooperation(notices_sent=10, decided=10, removed=6), "good"),
        (Cooperation(notices_sent=10, decided=10, removed=3), "mixed"),
        (Cooperation(notices_sent=10, decided=10, removed=2), "bad"),
        (Cooperation(notices_sent=3, decided=3, removed=0), "bad"),
        # Mostly forwarded (a CDN passing it on): not the provider's own record.
        (Cooperation(notices_sent=4, decided=4, removed=1, forwarded=3), "mixed"),
        (Cooperation(notices_sent=4, decided=4, removed=2, forwarded=2), "mixed"),
    ],
)
def test_takedown_chance_band(history: Cooperation, expected: str) -> None:
    assert takedown_chance(history) == expected


def test_takedown_chance_needs_the_minimum_sample() -> None:
    assert MIN_DECIDED_NOTICES == 3
    below = Cooperation(notices_sent=2, decided=MIN_DECIDED_NOTICES - 1, removed=2)
    at = Cooperation(notices_sent=3, decided=MIN_DECIDED_NOTICES, removed=3)
    assert (takedown_chance(below), takedown_chance(at)) == ("unknown", "exemplary")


def test_takedown_chance_codes_are_stable_and_ordered() -> None:
    assert TAKEDOWN_CHANCES == ("unknown", "bad", "mixed", "good", "very_good", "exemplary")
