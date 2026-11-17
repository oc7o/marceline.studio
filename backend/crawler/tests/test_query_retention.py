"""Retention for the discovery search trail (ADR 0048, issue #103).

Invented names throughout: a query's text is exactly what this feature exists to remove.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from crawler.models import SearchCall, SourceCandidate
from crawler.retention import (
    apply_query_retention,
    scrub_search_calls,
    scrubbed_evidence,
    scrubbed_sample,
)

pytestmark = pytest.mark.django_db


def make_call(*, query: str, age_days: int, cost: str = "0.002") -> SearchCall:
    call = SearchCall.objects.create(
        query=query, mode="standard", status="ok", cost_usd=Decimal(cost), result_count=10
    )
    # `created_at` is auto_now_add, so the age is written afterwards.
    SearchCall.objects.filter(pk=call.pk).update(
        created_at=timezone.now() - timedelta(days=age_days)
    )
    call.refresh_from_db()
    return call


def make_candidate(
    *, domain: str, age_days: int, queries: list[dict[str, object]]
) -> SourceCandidate:
    candidate = SourceCandidate.objects.create(domain=domain, evidence={"queries": queries})
    SourceCandidate.objects.filter(pk=candidate.pk).update(
        last_seen_at=timezone.now() - timedelta(days=age_days)
    )
    candidate.refresh_from_db()
    return candidate


# --- the pure part ----------------------------------------------------------------------------


def test_a_sample_loses_its_query_and_nothing_else() -> None:
    sample = {
        "query": "Nora Vale leaked",
        "rank": 3,
        "url": "https://pirate.example/a",
        "title": "x",
    }
    cleaned, changed = scrubbed_sample(sample)
    assert changed
    assert cleaned == {"rank": 3, "url": "https://pirate.example/a", "title": "x"}


def test_a_sample_without_a_query_is_untouched() -> None:
    assert scrubbed_sample({"rank": 1}) == ({"rank": 1}, False)
    assert scrubbed_sample("not a mapping") == ("not a mapping", False)


def test_only_the_queries_section_of_the_evidence_changes() -> None:
    evidence = {
        "queries": [{"query": "Nora Vale leaked", "rank": 1}, {"rank": 2}],
        "linking_pages": ["https://pirate.example/b"],
        "mirror_of": "other.example",
    }
    cleaned, changed = scrubbed_evidence(evidence)
    assert changed == 1
    assert cleaned["queries"] == [{"rank": 1}, {"rank": 2}]
    assert cleaned["linking_pages"] == ["https://pirate.example/b"]
    assert cleaned["mirror_of"] == "other.example"


def test_evidence_with_no_query_text_is_returned_unchanged() -> None:
    assert scrubbed_evidence({"linking_pages": []}) == ({"linking_pages": []}, 0)
    assert scrubbed_evidence(None) == (None, 0)


# --- the pass ---------------------------------------------------------------------------------


def test_a_dry_run_writes_nothing() -> None:
    call = make_call(query="Nora Vale leaked", age_days=40)
    report = apply_query_retention(dry_run=True, retain_days=30)
    assert report.due == 1 and report.scrubbed == 0
    call.refresh_from_db()
    assert call.query == "Nora Vale leaked" and call.query_scrubbed_at is None


def test_the_text_goes_and_the_accounting_stays() -> None:
    old = make_call(query="Nora Vale leaked", age_days=40, cost="0.002")
    recent = make_call(query="Dana Quill leaked", age_days=2)
    report = apply_query_retention(dry_run=False, retain_days=30)

    assert report.due == 1 and report.scrubbed == 1
    old.refresh_from_db()
    recent.refresh_from_db()
    assert old.query == "" and old.query_scrubbed_at is not None
    assert recent.query == "Dana Quill leaked" and recent.query_scrubbed_at is None
    # The row, the cost, the outcome and the timestamp are the spend ledger of ADR 0013.
    assert SearchCall.objects.count() == 2
    assert old.cost_usd == Decimal("0.002")
    assert old.status == "ok" and old.result_count == 10 and old.mode == "standard"


def test_it_is_idempotent_and_keeps_the_first_stamp() -> None:
    call = make_call(query="Nora Vale leaked", age_days=40)
    apply_query_retention(dry_run=False, retain_days=30)
    call.refresh_from_db()
    first = call.query_scrubbed_at

    second = apply_query_retention(dry_run=False, retain_days=30)

    assert second.due == 0 and second.scrubbed == 0
    call.refresh_from_db()
    assert call.query_scrubbed_at == first


def test_candidate_evidence_is_scrubbed_on_the_same_clock() -> None:
    old = make_candidate(
        domain="old.example",
        age_days=40,
        queries=[{"query": "Nora Vale leaked", "rank": 1, "url": "https://old.example/a"}],
    )
    touched = make_candidate(
        domain="touched.example", age_days=1, queries=[{"query": "Dana Quill leaked"}]
    )

    report = apply_query_retention(dry_run=False, retain_days=30)

    assert report.candidates_with_text == 2
    assert report.candidates_due == 1 and report.candidates_scrubbed == 1
    assert report.samples_scrubbed == 1
    old.refresh_from_db()
    touched.refresh_from_db()
    # The rank and the URL stay: they are the evidence that this domain carries copies.
    assert old.evidence["queries"] == [{"rank": 1, "url": "https://old.example/a"}]
    assert touched.evidence["queries"] == [{"query": "Dana Quill leaked"}]


def test_scrub_search_calls_takes_ids_and_skips_what_is_already_blank() -> None:
    first = make_call(query="Nora Vale leaked", age_days=1)
    second = make_call(query="", age_days=1)
    assert scrub_search_calls([first.pk, second.pk]) == 1
    assert scrub_search_calls([first.pk, second.pk]) == 0
    assert scrub_search_calls([]) == 0


def test_the_report_names_no_query_and_no_domain() -> None:
    make_call(query="Nora Vale leaked", age_days=40)
    make_candidate(domain="pirate.example", age_days=40, queries=[{"query": "Nora Vale leaked"}])
    report = apply_query_retention(dry_run=True, retain_days=30)
    text = repr(report)
    assert "Nora" not in text and "pirate.example" not in text
