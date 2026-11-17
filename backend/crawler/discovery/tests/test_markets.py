"""German discovery market, query safety and desk-research candidates (issue #24, ADR 0013)."""

import json
from io import StringIO
from unittest.mock import patch

import httpx
import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from catalog.models import Creator, Site
from crawler.discovery.dataforseo import DataForSEO
from crawler.discovery.services import (
    GERMAN_GENERIC_QUERIES,
    GERMAN_MARKET,
    GERMAN_QUERY_TEMPLATES,
    Query,
    plan_queries,
    record_research,
    unsafe_query_reason,
)
from crawler.models import CandidateCreator, SearchCall, SourceCandidate
from crawler.services import load_safety_terms

pytestmark = pytest.mark.django_db
CREDS = override_settings(DATAFORSEO_LOGIN="login", DATAFORSEO_PASSWORD="password")
NOTE = "desk research: traffic ranking"


def _organic(url: str) -> dict[str, object]:
    item = {"type": "organic", "rank_absolute": 1, "url": url, "title": "t"}
    return {"status_code": 20000, "cost": "0.002", "tasks": [{"result": [{"items": [item]}]}]}


def test_german_templates_and_generic_queries_are_safe() -> None:
    terms = load_safety_terms()

    for template in GERMAN_QUERY_TEMPLATES:
        assert unsafe_query_reason(template.format(name="Example Name"), terms) == "", template
    for query in GERMAN_GENERIC_QUERIES:
        assert unsafe_query_reason(query, terms) == "", query


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Example heimlich gefilmt", "forbidden word: heimlich"),
        ("Example Ex-Freundin", "forbidden word: ex freundin"),
        ("Example versteckte Kamera", "forbidden word: versteckte kamera"),
        ("Example junge Frau", "safety signal: minor_context:young"),
        ("Example Schülerin", "safety signal: minor_context:school"),
        ("Example flagged term", "safety signal: block_term"),  # customer_release_queue blocks
        ("Example hidden cam", "safety signal: review_term"),
    ],
)
def test_unsafe_queries_are_named(text: str, reason: str) -> None:
    assert unsafe_query_reason(text, load_safety_terms()) == reason


def test_plan_german_market_templates_and_generic_queries() -> None:
    creator = Creator.objects.create(name="Example Name", aliases=["Alias"])

    plan = plan_queries(["Example Name"], [], market=GERMAN_MARKET)
    generic = plan_queries([], [], market=GERMAN_MARKET, generic=True)

    assert plan[0] == Query("Example Name geleakt", creator.pk)
    assert Query("Alias download", creator.pk) in plan
    assert len(plan) == 2 * len(GERMAN_QUERY_TEMPLATES)
    assert [query.text for query in generic] == list(GERMAN_GENERIC_QUERIES)
    assert all(query.creator_id is None for query in generic)
    with pytest.raises(ValueError, match="no generic queries"):
        plan_queries([], [], generic=True)


@override_settings(DATAFORSEO_LOGIN="", DATAFORSEO_PASSWORD="")
def test_german_dry_run_names_market_and_cost_without_calls() -> None:
    out = StringIO()

    with patch("httpx.Client.request", side_effect=AssertionError("network")):
        call_command("discover_sources", market="de", generic=True, dry_run=True, stdout=out)

    text = out.getvalue()
    assert "market=de location=2276 language=de" in text
    assert "deutsche fanhub leaks" in text
    assert f"queries={len(GERMAN_GENERIC_QUERIES)} estimated cost=$0.010000" in text
    assert not SearchCall.objects.exists()


@override_settings(DATAFORSEO_LOGIN="", DATAFORSEO_PASSWORD="")
def test_dry_run_reports_queries_left_out_by_the_budget() -> None:
    Creator.objects.create(name="Example Name")
    out = StringIO()

    with override_settings(DISCOVERY_MAX_CALLS_PER_RUN=3):
        call_command("discover_sources", market="de", dry_run=True, stdout=out)

    assert "queries=3 estimated cost=$0.006000" in out.getvalue()
    left_out = len(GERMAN_QUERY_TEMPLATES) - 3
    assert f"{left_out} more planned queries left out" in out.getvalue()


@override_settings(DATAFORSEO_LOGIN="", DATAFORSEO_PASSWORD="")
def test_unsafe_query_is_refused_before_any_call_even_in_dry_run() -> None:
    with pytest.raises(CommandError, match="heimlich"):
        call_command("discover_sources", query=["deutsche heimlich leaks"], dry_run=True)
    assert not SearchCall.objects.exists()


@CREDS
def test_german_market_searches_germany_in_german() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.extend(json.loads(request.content))
        return httpx.Response(200, json=_organic("https://www.newsite.test/v"))

    client = DataForSEO(
        transport=httpx.MockTransport(handler),
        location_code=GERMAN_MARKET.location_code,
        language=GERMAN_MARKET.language,
    )
    client.search("deutsche fanhub leaks")

    assert sent[0]["location_code"] == 2276
    assert sent[0]["language_code"] == "de"
    call = SearchCall.objects.get()
    assert (call.location, call.language) == (2276, "de")


@CREDS
def test_default_market_keeps_the_configured_location() -> None:
    with override_settings(DISCOVERY_LOCATION_CODE=2840, DISCOVERY_LANGUAGE="en"):
        client = DataForSEO(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_organic("x")))
        )
    assert (client.location_code, client.language) == (2840, "en")


@CREDS
def test_german_command_run_links_creator_and_logs_locale() -> None:
    creator = Creator.objects.create(name="Example Name")
    out = StringIO()

    with patch(
        "httpx.Client.request",
        return_value=httpx.Response(
            200,
            json=_organic("https://www.newsite.test/v"),
            request=httpx.Request("POST", "https://api.dataforseo.com"),
        ),
    ):
        call_command(
            "discover_sources", creator=["Example Name"], market="de", max_calls=1, stdout=out
        )

    assert "calls=1" in out.getvalue()
    assert SearchCall.objects.get().language == "de"
    candidate = SourceCandidate.objects.get(domain="newsite.test")
    assert candidate.evidence["queries"][0]["query"] == "Example Name geleakt"
    assert CandidateCreator.objects.filter(candidate=candidate, creator=creator).exists()


def test_record_research_creates_a_proposed_candidate_without_search_hits() -> None:
    result = record_research("https://www.example-tube.test/", NOTE)
    again = record_research("example-tube.test", NOTE)

    candidate = SourceCandidate.objects.get(domain="example-tube.test")
    assert (result.created, again.created) == (True, False)
    assert candidate.status == "proposed"
    assert candidate.search_hits == 0
    assert candidate.evidence["research"] == [NOTE]


def test_record_research_skips_sites_ignored_domains_and_keeps_reviews() -> None:
    Site.objects.create(domain="known.test", name="known")
    SourceCandidate.objects.create(domain="reviewed.test", status="rejected")

    site = record_research("known.test", NOTE)
    ignored = record_research("linkedin.com", NOTE)
    invalid = record_research("not a domain", NOTE)
    reviewed = record_research("reviewed.test", NOTE)

    assert (site.candidate, site.skipped) == (None, "already a Site")
    assert ignored.candidate is None and ignored.skipped
    assert invalid.candidate is None
    assert reviewed.candidate is not None and reviewed.candidate.status == "rejected"
    assert SourceCandidate.objects.get(domain="reviewed.test").evidence["research"] == [NOTE]
    assert not SourceCandidate.objects.filter(domain__in=["known.test", "linkedin.com"]).exists()


def test_add_domain_command_needs_a_note_and_makes_no_request() -> None:
    out = StringIO()

    with pytest.raises(CommandError, match="--note"):
        call_command("discover_sources", add_domain=["example-tube.test"])
    with patch("httpx.Client.request", side_effect=AssertionError("network")):
        call_command(
            "discover_sources",
            add_domain=["example-tube.test"],
            note=NOTE,
            dry_run=True,
            stdout=out,
        )
        assert not SourceCandidate.objects.exists()
        call_command("discover_sources", add_domain=["example-tube.test"], note=NOTE, stdout=out)

    assert "calls=0 cost=$0 new=1 updated=0 skipped=0" in out.getvalue()
    assert SourceCandidate.objects.get().evidence["research"] == [NOTE]
