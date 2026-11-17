"""What the customer sees of their own originals — and what they must never see.

Two invariants are asserted here as hard as the tenancy rule itself:
- a safety-flagged page is not listed and **not counted** (ADR 0011/0018): a count would say a
  hidden page exists;
- no media, of any page, anywhere in a customer response (issue #53).
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from accounts.models import Organisation
from catalog.models import Site
from index.models import Media, Page
from index.services import gate_min_score, page_media_allowed
from marceline.app.safety import (
    fields_visible,
    hidden_page_filter,
    page_visible,
    visible_page_filter,
)
from marceline.tests.app_helpers import (
    APP,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.copy_extent import CLASSIFIER_VERSION
from matching.models import Reference, ReferenceLeak, ReferenceMatch

FINDING_FIELDS = {
    "page_id",
    "site",
    "url",
    "availability",
    "last_checked",
    "evidence",
    "extent",
}


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def owner(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def customer(owner: User) -> Client:
    return client_for(owner)


@pytest.fixture
def original(owner: User) -> Reference:
    return make_reference(owner, title="Alpha's own original")


def match(reference: Reference, page: Page, **fields: Any) -> ReferenceMatch:
    values: dict[str, Any] = {
        "best_distance": 8,
        "matched_frames": 4,
        "page_frames": 6,
        "best_reference_timestamp_s": 12.0,
        "evidence_frames": 3,
        "strength": "strong",
        "band": "high",
        **fields,
    }
    return ReferenceMatch.objects.create(reference=reference, page=page, **values)


def test_an_original_nobody_found_anything_for(customer: Client, original: Reference) -> None:
    [row] = customer.get(f"{APP}/originals").json()

    assert row["id"] == public_id_of(original)
    assert row["title"] == "Alpha's own original"
    assert row["state"] == "nothing_found"
    assert row["counts"] == {"confirmed": 0, "removed": 0}


def test_found_online_and_found_removed(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    online = make_page("still-up")
    gone = make_page("taken-down")
    gone.availability = Page.Availability.REMOVED
    gone.save(update_fields=["availability"])
    ReferenceLeak.objects.create(reference=original, page=online, status="confirmed")
    ReferenceLeak.objects.create(reference=original, page=gone, status="confirmed")

    [row] = customer.get(f"{APP}/originals").json()
    assert row["state"] == "found_online"
    assert row["counts"] == {"confirmed": 2, "removed": 1}

    online.availability = Page.Availability.REMOVED
    online.save(update_fields=["availability"])

    [row] = customer.get(f"{APP}/originals").json()
    assert row["state"] == "found_removed"
    assert row["counts"]["removed"] == 2


def test_an_unconfirmed_match_is_invisible_not_even_counted(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """Operator decision 2026-09-17: what we are still looking at is internal."""
    candidate = make_page("candidate")
    match(original, candidate)
    ReferenceLeak.objects.create(reference=original, page=candidate, status="proposed")

    [row] = customer.get(f"{APP}/originals").json()
    detail = customer.get(f"{APP}/originals/{public_id_of(original)}")

    assert row["state"] == "nothing_found"
    assert row["counts"] == {"confirmed": 0, "removed": 0}
    assert detail.json()["findings"] == []
    assert candidate.url not in detail.content.decode()


def test_low_band_noise_is_not_shown_until_a_human_looks_at_it(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    noise = make_page("two-frames-apart")
    match(original, noise, band="low", evidence_frames=1, strength="weak")

    assert customer.get(f"{APP}/originals").json()[0]["counts"]["confirmed"] == 0

    ReferenceLeak.objects.create(reference=original, page=noise, status="confirmed")

    assert customer.get(f"{APP}/originals").json()[0]["counts"]["confirmed"] == 1


@pytest.mark.parametrize("decided_by", ["human", "auto"])
def test_a_confirmed_match_stays_a_finding_when_matching_rebands_it_low(
    customer: Client, original: Reference, make_page: Callable[[str], Page], decided_by: str
) -> None:
    """The low band filters only what nobody decided. Matching re-bands decided rows on every
    run, so a confirmed match with no leak row must not vanish when its band drops to `low`."""
    page = make_page("confirmed-then-rebanded")
    match(original, page, status="confirmed", decided_by=decided_by)
    assert customer.get(f"{APP}/originals").json()[0]["counts"]["confirmed"] == 1

    ReferenceMatch.objects.filter(reference=original, page=page).update(
        band="low", evidence_frames=1, strength="weak"
    )

    [row] = customer.get(f"{APP}/originals").json()
    detail = customer.get(f"{APP}/originals/{public_id_of(original)}").json()
    assert (row["state"], row["counts"]["confirmed"]) == ("found_online", 1)
    assert [finding["page_id"] for finding in detail["findings"]] == [page.public_id]
    assert [finding["page_id"] for finding in customer.get(f"{APP}/findings").json()] == [
        page.public_id
    ]


def test_rejected_findings_are_gone(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("not-it")
    match(original, page, status="rejected")
    ReferenceLeak.objects.create(reference=original, page=page, status="rejected")

    assert customer.get(f"{APP}/originals").json()[0]["counts"]["confirmed"] == 0


def test_detail_lists_the_case_as_text(
    customer: Client, original: Reference, site: Site, make_page: Callable[[str], Page]
) -> None:
    confirmed = make_page("confirmed-leak")
    reviewing = make_page("under-review")
    ReferenceLeak.objects.create(reference=original, page=confirmed, status="confirmed")
    match(original, confirmed, status="confirmed")
    match(original, reviewing, evidence_frames=2)

    response = customer.get(f"{APP}/originals/{public_id_of(original)}")

    assert response.status_code == 200
    body = response.json()
    assert body["original"]["state"] == "found_online"
    # The copy under review is not there at all.
    assert [row["page_id"] for row in body["findings"]] == [confirmed.public_id]
    assert reviewing.url not in response.content.decode()
    first = body["findings"][0]
    assert set(first) == FINDING_FIELDS
    assert (first["page_id"], first["site"]) == (confirmed.public_id, site.domain)
    assert first["url"] == confirmed.url
    assert (first["availability"], first["evidence"]) == ("unknown", "fingerprint_match")


@pytest.mark.parametrize(
    ("status", "matches"),
    [
        (Page.SafetyStatus.BLOCKED, []),
        (Page.SafetyStatus.REVIEW, []),
        (Page.SafetyStatus.CLEAR, [{"term": "x", "category": "y", "action": "review"}]),
    ],
)
def test_a_safety_flagged_page_is_never_listed_or_counted(
    customer: Client,
    original: Reference,
    make_page: Callable[[str], Page],
    status: str,
    matches: list[dict[str, str]],
) -> None:
    """ADR 0018 and docs/customer-ui-plan.md §4: never, in any form — not even as a count."""
    flagged = make_page("flagged")
    flagged.safety_status = status
    flagged.safety_matches = matches
    flagged.save(update_fields=["safety_status", "safety_matches"])
    ReferenceLeak.objects.create(reference=original, page=flagged, status="confirmed")
    match(original, flagged, status="confirmed")

    listed = customer.get(f"{APP}/originals").json()[0]
    detail = customer.get(f"{APP}/originals/{public_id_of(original)}").json()

    assert listed["state"] == "nothing_found"
    assert listed["counts"] == {"confirmed": 0, "removed": 0}
    assert detail["findings"] == []
    assert (
        flagged.url
        not in customer.get(f"{APP}/originals/{public_id_of(original)}").content.decode()
    )


@pytest.mark.parametrize("score", [0.2, 0.79, None], ids=["low", "just-below", "never-scored"])
def test_a_page_the_media_gate_does_not_allow_is_never_listed_or_counted(
    customer: Client,
    original: Reference,
    make_page: Callable[[str], Page],
    score: float | None,
) -> None:
    """ADR 0018: clear is not enough. A page scored below the gate, or never scored, is hidden."""
    gated = make_page("gated")
    gated.safety_score = score
    gated.save(update_fields=["safety_score"])
    ReferenceLeak.objects.create(reference=original, page=gated, status="confirmed")
    match(original, gated, status="confirmed")

    listed = customer.get(f"{APP}/originals").json()[0]
    detail = customer.get(f"{APP}/originals/{public_id_of(original)}")

    assert listed["state"] == "nothing_found"
    assert listed["counts"] == {"confirmed": 0, "removed": 0}
    assert detail.json()["findings"] == []
    assert gated.url not in detail.content.decode()


def test_a_page_exactly_at_the_gate_is_visible(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("at-the-gate")
    page.safety_score = gate_min_score()
    page.save(update_fields=["safety_score"])
    ReferenceLeak.objects.create(reference=original, page=page, status="confirmed")

    assert customer.get(f"{APP}/originals").json()[0]["counts"]["confirmed"] == 1


@pytest.mark.parametrize("status", list(Page.SafetyStatus))
@pytest.mark.parametrize("score", [None, 0.0, 0.79, 0.8, 1.0])
@pytest.mark.parametrize("matches", [[], [{"term": "x", "category": "y", "action": "review"}]])
def test_the_query_and_the_in_memory_rule_are_one_rule(
    make_page: Callable[[str], Page],
    status: str,
    score: float | None,
    matches: list[dict[str, str]],
) -> None:
    """`visible_page_filter` spells `page_visible` (and so `page_media_allowed`) as a query."""
    page = make_page("any")
    page.safety_status = status
    page.safety_score = score
    page.safety_matches = matches
    page.save(update_fields=["safety_status", "safety_score", "safety_matches"])

    queried = Page.objects.filter(visible_page_filter(), pk=page.pk).exists()
    # `hidden_page_filter` is what tells the production cache to forget a page
    # (`marceline/cache_purge.py`), so it has to be the exact complement of this one — including
    # for a never-scored page, where a plain `~Q(...)` would evaluate to SQL NULL and match
    # nothing.
    hidden = Page.objects.filter(hidden_page_filter(), pk=page.pk).exists()

    assert queried == page_visible(page)
    assert hidden is not queried
    assert page_visible(page) == (
        page_media_allowed(page.safety_status, page.safety_score) and not matches
    )
    assert fields_visible(page.safety_status, page.safety_score, page.safety_matches) == queried


def test_no_customer_response_carries_media(
    customer: Client,
    original: Reference,
    make_page: Callable[[str], Page],
    make_media: Callable[..., Media],
) -> None:
    """Phase 1 embeds no pirate imagery at all (#53), not even for a perfectly safe page.

    The guarantee is absence: no schema in `marceline/app` has a media field, so a customer
    response carries nothing a viewer could turn into an image request.
    """
    page = make_page("confirmed-leak")
    media = make_media(page)
    ReferenceLeak.objects.create(reference=original, page=page, status="confirmed")

    detail = customer.get(f"{APP}/originals/{public_id_of(original)}")
    listing = customer.get(f"{APP}/originals")

    assert set(detail.json()["findings"][0]) == FINDING_FIELDS
    for body in (detail.content.decode(), listing.content.decode()):
        assert "media" not in body.lower()
        assert media.url not in body


def test_each_confirmed_copy_says_how_much_of_the_original_it_carries(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """Issue #62: a fact about the copy — and an excerpt is a finding like any other."""
    current = {"copy_extent_version": CLASSIFIER_VERSION}
    expected = {}
    for slug, fields, extent in (
        ("full", {"copy_extent": "full_copy", **current}, "full_copy"),
        ("clip", {"copy_extent": "excerpt", **current}, "excerpt"),
        ("compilation", {"copy_extent": "compilation_candidate", **current}, "not_determined"),
        ("poster", {"copy_extent": "unknown", **current}, "not_determined"),
        ("stale", {"copy_extent": "full_copy"}, "not_determined"),
    ):
        page = make_page(slug)
        match(original, page, status="confirmed", decided_by="human", **fields)
        expected[page.public_id] = extent
    leak_only = make_page("leak-only")
    ReferenceLeak.objects.create(reference=original, page=leak_only, status="confirmed")
    expected[leak_only.public_id] = "not_determined"

    detail = customer.get(f"{APP}/originals/{public_id_of(original)}").json()
    case = customer.get(f"{APP}/cases/{public_id_of(original)}").json()
    findings = customer.get(f"{APP}/findings").json()

    assert detail["original"]["counts"]["confirmed"] == 6
    for rows in (detail["findings"], case["findings"], findings):
        assert {row["page_id"]: row["extent"] for row in rows} == expected
