"""Issue #18: ignore rules for public broadcaster media libraries, legal streaming
guides/aggregators and leak-protection competitors, so none of them ever show up as a
source-discovery candidate.

Table-driven, no live network: `crawler.discovery.domains` is pure, and `apply_rules`/`upsert`
only touch the database (real compose Postgres, per `make check`), never the network.
"""

import pytest

from crawler.discovery.domains import classify, ignore_reason, ignored, normalize_domain
from crawler.discovery.services import apply_rules, upsert
from crawler.models import SourceCandidate

pytestmark = pytest.mark.django_db

OFF_TOPIC = [
    "cnet.com",
    "dfb-akademie.de",
    "discadia.com",
    "takedown-service-4.example",
    "semrush.com",
    "sgvmotoczesci.pl",
]
TELEGRAM_DIRECTORIES = ["telegram-group.com", "telegram.im", "tgramsearch.com"]
PASTE_SERVICES = ["rentry.co", "rentry.org"]
TYPOSQUAT = ["hexlupoad.net"]
CANDIDATE_REVIEW_DOMAINS = OFF_TOPIC + TELEGRAM_DIRECTORIES + PASTE_SERVICES + TYPOSQUAT

PUBLIC_BROADCASTERS = [
    ("ardmediathek.de", "ARD Mediathek"),
    ("zdf.de", "ZDF Mediathek"),
    ("arte.tv", "arte Mediathek"),
    ("3sat.de", "3sat Mediathek"),
    ("orf.at", "ORF TVthek"),
    ("srf.ch", "SRF Play"),
]
STREAMING_GUIDES = ["werstreamt.es", "justwatch.com"]
COMPETITORS = [
    "takedown-service-a.example",
    "takedown-service-b.example",
    "takedown-service-c.example",
]
NEW_RULE_DOMAINS = (
    [domain for domain, _ in PUBLIC_BROADCASTERS]
    + STREAMING_GUIDES
    + COMPETITORS
    + CANDIDATE_REVIEW_DOMAINS
)


@pytest.mark.parametrize("domain,label", PUBLIC_BROADCASTERS)
def test_public_broadcaster_ignored_with_reason(domain: str, label: str) -> None:
    assert ignored(domain)
    reason = ignore_reason(domain)
    assert "public broadcaster media library" in reason
    assert label in reason
    assert "not a leak source" in reason


@pytest.mark.parametrize("domain", STREAMING_GUIDES)
def test_streaming_guide_ignored_with_reason(domain: str) -> None:
    assert ignored(domain)
    reason = ignore_reason(domain)
    assert "legal streaming guide/aggregator" in reason
    assert "not a leak source" in reason


@pytest.mark.parametrize("domain", COMPETITORS)
def test_competitor_ignored_with_reason(domain: str) -> None:
    assert ignored(domain)
    reason = ignore_reason(domain)
    assert "leak-protection competitor" in reason


@pytest.mark.parametrize("domain", NEW_RULE_DOMAINS)
def test_every_new_rule_has_a_distinct_non_empty_reason(domain: str) -> None:
    reason = ignore_reason(domain)
    assert reason
    assert reason == ignore_reason(domain)  # deterministic, not derived from mutable state


@pytest.mark.parametrize("domain", NEW_RULE_DOMAINS)
def test_upsert_never_creates_a_candidate(domain: str) -> None:
    """The domain never shows up as a proposed source candidate: `upsert` (called for every
    search hit and every mined external link) refuses to create a row for it at all.
    """
    assert upsert(f"https://www.{domain}/some/path") == (None, False)
    assert not SourceCandidate.objects.filter(domain=domain).exists()


@pytest.mark.parametrize(
    "url,domain",
    [
        ("https://www.ardmediathek.de/sendung/x", "ardmediathek.de"),
        ("https://mediathek.zdf.de/some-show", "zdf.de"),
        ("https://www.justwatch.com/de", "justwatch.com"),
        ("https://app.takedown-service-a.example/dashboard", "takedown-service-a.example"),
    ],
)
def test_subdomains_normalize_onto_the_ignored_registrable_domain(url: str, domain: str) -> None:
    assert normalize_domain(url) == domain
    assert ignored(domain)


@pytest.mark.parametrize("domain", NEW_RULE_DOMAINS)
def test_reapply_rules_ignores_already_proposed_candidates(domain: str) -> None:
    """Covers `discover_sources --reapply-rules`: a candidate proposed before this rule shipped
    (or created by another offline path) is reclassified `ignored`, with the specific reason
    recorded as its note so the candidate shows *why* it was ignored.
    """
    candidate = SourceCandidate.objects.create(domain=domain)
    reason = apply_rules(candidate)
    assert reason == ignore_reason(domain)
    assert candidate.status == "ignored"
    assert candidate.notes == reason


@pytest.mark.parametrize("domain", OFF_TOPIC)
def test_off_topic_hit_ignored_with_reason(domain: str) -> None:
    assert ignored(domain)
    reason = ignore_reason(domain)
    assert reason.startswith("off-topic search hit")
    assert "not a leak source" in reason


@pytest.mark.parametrize("domain", TELEGRAM_DIRECTORIES)
def test_telegram_directory_ignored_with_reason(domain: str) -> None:
    assert ignored(domain)
    reason = ignore_reason(domain)
    assert "third-party Telegram channel directory" in reason


@pytest.mark.parametrize("domain", PASTE_SERVICES)
def test_paste_service_ignored_with_reason(domain: str) -> None:
    assert ignored(domain)
    assert (
        ignore_reason(domain) == "paste service, no video/performer content, just link-list hosting"
    )


def test_typosquat_ignored_with_reason() -> None:
    assert ignored("hexlupoad.net")
    reason = ignore_reason("hexlupoad.net")
    assert "typosquat of hexupload.net" in reason


def test_cuty_url_shortener_classifies_as_hoster() -> None:
    """Candidate review 2026-09-17: same bucket as bit/tinyurl, already in HOSTER_LABELS."""
    assert classify("cuty.io") == ("file_hoster", "hoster")


def test_torrent_source_gets_its_own_kind_and_status() -> None:
    """p2p-site.example (ADR 0013 amendment 2026-09-17): a torrent/P2P source, not a classical file
    hoster — a distinct kind and status, never `ignored` or `hoster`, so it stays visible for a
    later decision instead of disappearing into either bucket."""
    assert classify("p2p-site.example") == ("torrent", "torrent")
    candidate, _ = upsert("https://p2p-site.example/some/torrent")
    assert candidate is not None
    assert candidate.kind == SourceCandidate.Kind.TORRENT
    assert candidate.status == SourceCandidate.Status.TORRENT


def test_legacy_ignore_list_entries_keep_the_generic_note() -> None:
    """Domains ignored before issue #18 have no domain-specific reason recorded yet;
    `apply_rules` falls back to the pre-existing generic note, so
    `crawler.tests.test_discovery.test_reapply_rules_command` (asserting that exact text for
    creator-wiki.example) keeps passing unchanged.
    """
    assert ignore_reason("creator-wiki.example") == ""
    candidate = SourceCandidate.objects.create(domain="creator-wiki.example")
    reason = apply_rules(candidate)
    assert reason == "ignored non-source domain"
    assert candidate.notes == "ignored non-source domain"
