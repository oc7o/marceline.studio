"""The case file, from the negative side first.

Three guarantees are asserted here as hard as tenancy itself (which `test_app_tenancy.py` owns):

- a safety-flagged page is never a finding, never a removal and **never a count** — and a page
  that becomes flagged later leaves no trace behind, not even an empty case row;
- no media, of any page, anywhere in a customer response (#53);
- **no prose.** Every value a bilingual UI branches on is a machine-readable code. A German or
  English sentence in one of these payloads is a bug, so the test walks the payload and insists.

The rest fixes what each timeline step is derived from, and that a removed finding can reopen:
"handled" means removed *and* not re-uploaded anywhere, so the removals log is living state.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from accounts.models import Organisation
from catalog.models import Site
from index.models import Media, Page
from marceline.tests.app_helpers import (
    APP,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.models import CaseFinding, LeakCase, Reference, ReferenceLeak, ReferenceMatch

CASE_FINDING_FIELDS = {
    "original_id",
    "page_id",
    "site",
    "url",
    "case_state",
    "availability",
    "online",
    "last_checked",
    "confirmed_on",
    "removed_at",
    "reappeared_at",
    "evidence",
    "extent",
    "hosting",
}
CASE_FIELDS = {
    "original_id",
    "title",
    "state",
    "opened_on",
    "removed_at",
    "reopened_on",
    "reopen_count",
    "counts",
}
DETAIL_FIELDS = {"original", "case", "findings", "timeline", "not_available"}
STEP_FIELDS = {"step", "status", "at", "on", "reason", "blocked_by_issue"}
UNKNOWN_FIELDS = {"topic", "reason", "blocked_by_issue"}
REMOVAL_FIELDS = {
    "original_id",
    "title",
    "page_id",
    "site",
    "url",
    "removed_at",
    "confirmed_on",
    "reappeared_at",
    "last_checked",
    "state",
    "case_state",
    "handled",
}
# The customer steps of docs/customer-ui-plan.md §3, in the operator's order. Found and verified
# are one step, `confirmed`, dated to the day (operator decision, 2026-09-17).
STEPS = [
    "confirmed",
    "notice_prepared",
    "notice_sent",
    "notice_acknowledged",
    "removed",
    "rechecked",
]
#: A machine-readable code: lower case, no spaces, no punctuation, no language.
CODE = re.compile(r"^[a-z][a-z0-9_]*$")
#: A day, with nothing finer: our own step never says what time we confirmed something.
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Stamps that, next to a confirmation date, would measure how fast we review.
LATENCY_FIELDS = {"discovered_at", "verified_at", "first_seen", "opened_at", "detected_at"}

FIRST_CHECK = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
LATER_CHECK = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
LAST_CHECK = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)


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


def confirm(reference: Reference, page: Page) -> ReferenceLeak:
    return ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")


def take_down(page: Page, at: datetime) -> None:
    """What `crawl_recheck` does when it finds a page gone."""
    page.availability = Page.Availability.REMOVED
    page.availability_checked_at = at
    page.save(update_fields=["availability", "availability_checked_at"])


def put_back(page: Page, at: datetime) -> None:
    """The same URL live again — the norm on these sites."""
    page.availability = Page.Availability.PLAYABLE
    page.availability_checked_at = at
    page.save(update_fields=["availability", "availability_checked_at"])


def steps(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["step"]: step for step in body["timeline"]}


@pytest.mark.parametrize(
    ("status", "matches"),
    [
        (Page.SafetyStatus.BLOCKED, []),
        (Page.SafetyStatus.REVIEW, []),
        (Page.SafetyStatus.CLEAR, [{"term": "x", "category": "y", "action": "review"}]),
    ],
)
def test_a_safety_flagged_page_is_never_a_finding_a_case_or_a_removal(
    customer: Client,
    original: Reference,
    make_page: Callable[[str], Page],
    status: str,
    matches: list[dict[str, str]],
) -> None:
    """ADR 0018 and plan §4: never, in any form — not even as a count or a removal receipt."""
    flagged = make_page("flagged")
    flagged.safety_status = status
    flagged.safety_matches = matches
    flagged.save(update_fields=["safety_status", "safety_matches"])
    confirm(original, flagged)
    match(original, flagged, status="confirmed")
    take_down(flagged, FIRST_CHECK)

    detail = customer.get(f"{APP}/cases/{public_id_of(original)}")
    body = detail.json()

    assert customer.get(f"{APP}/findings").json() == []
    assert customer.get(f"{APP}/cases").json() == []
    assert customer.get(f"{APP}/removals").json() == []
    assert (body["case"], body["findings"]) == (None, [])
    assert body["original"]["counts"] == {"confirmed": 0, "removed": 0}
    assert steps(body)["confirmed"]["status"] == "pending"
    assert flagged.url not in detail.content.decode()
    # Not even a row that would let a customer infer a hidden page exists.
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (0, 0)


def test_a_page_that_becomes_flagged_leaves_no_trace_of_its_case(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """A case is written on read, so the removal of one has to be written on read too."""
    page = make_page("clear-for-now")
    confirm(original, page)
    take_down(page, FIRST_CHECK)
    assert len(customer.get(f"{APP}/removals").json()) == 1
    assert LeakCase.objects.count() == 1

    page.safety_status = Page.SafetyStatus.BLOCKED
    page.save(update_fields=["safety_status"])

    assert customer.get(f"{APP}/removals").json() == []
    assert customer.get(f"{APP}/findings").json() == []
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (0, 0)


@pytest.mark.parametrize("score", [0.2, None], ids=["below-the-gate", "never-scored"])
def test_a_page_the_media_gate_does_not_allow_is_no_case_and_leaves_no_trace(
    customer: Client,
    original: Reference,
    make_page: Callable[[str], Page],
    score: float | None,
) -> None:
    """ADR 0018: a page not clearly safe enough to fetch a picture from is not shown either."""
    page = make_page("clear-but-not-clearly-safe")
    confirm(original, page)
    take_down(page, FIRST_CHECK)
    assert len(customer.get(f"{APP}/removals").json()) == 1

    page.safety_score = score
    page.save(update_fields=["safety_score"])
    detail = customer.get(f"{APP}/cases/{public_id_of(original)}")

    assert customer.get(f"{APP}/findings").json() == []
    assert customer.get(f"{APP}/cases").json() == []
    assert customer.get(f"{APP}/removals").json() == []
    assert detail.json()["case"] is None
    assert page.url not in detail.content.decode()
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (0, 0)


def test_no_case_response_carries_media(
    customer: Client,
    original: Reference,
    make_page: Callable[[str], Page],
    make_media: Callable[..., Media],
) -> None:
    """The guarantee is absence: no schema in `marceline/app` has a media field at all."""
    page = make_page("confirmed-leak")
    media = make_media(page)
    confirm(original, page)
    take_down(page, FIRST_CHECK)

    responses = [
        customer.get(f"{APP}/findings"),
        customer.get(f"{APP}/cases"),
        customer.get(f"{APP}/cases/{public_id_of(original)}"),
        customer.get(f"{APP}/removals"),
    ]

    for response in responses:
        body = response.content.decode()
        assert "media" not in body.lower()
        assert media.url not in body
    assert set(responses[0].json()[0]) == CASE_FINDING_FIELDS
    assert set(responses[1].json()[0]) == CASE_FIELDS
    assert set(responses[2].json()) == DETAIL_FIELDS
    assert set(responses[3].json()[0]) == REMOVAL_FIELDS


def test_the_timeline_and_the_unknowns_are_codes_and_dates_never_prose(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """The UI is bilingual: it owns every word the customer reads, so the API ships none."""
    confirm(original, make_page("confirmed-leak"))

    body = customer.get(f"{APP}/cases/{public_id_of(original)}").json()

    assert [step["step"] for step in body["timeline"]] == STEPS
    for row in (*body["timeline"], *body["not_available"]):
        assert set(row) in (STEP_FIELDS, UNKNOWN_FIELDS)
        for key, value in row.items():
            if key in ("at", "on") or not isinstance(value, str):
                continue
            assert CODE.match(value), f"{key}={value!r} is not a machine-readable code"


def test_what_cannot_be_known_today_says_so_instead_of_inventing_it(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """Notice drafts exist now (#33); nothing sends one (#46)."""
    confirm(original, make_page("confirmed-leak"))

    body = customer.get(f"{APP}/cases/{public_id_of(original)}").json()
    timeline = steps(body)

    # No notice drafted yet for this original: the three notice steps are pending, not invented.
    assert [timeline[step]["status"] for step in STEPS[1:4]] == ["pending"] * 3
    assert [timeline[step]["at"] for step in STEPS[1:4]] == [None, None, None]
    # Hoster metadata and jurisdiction are per finding (ADR 0027); notice preparation and
    # cooperation history are real capabilities now — only Marceline's own transmission is not.
    assert {row["topic"] for row in body["not_available"]} == {
        "notice_delivery",
        "reupload_watch_coverage",
    }
    delivery = next(row for row in body["not_available"] if row["topic"] == "notice_delivery")
    assert delivery["reason"] == "legal_review_pending"
    assert delivery["blocked_by_issue"] == 46


def test_an_original_with_nothing_found_has_no_case_to_follow(
    customer: Client, original: Reference
) -> None:
    body = customer.get(f"{APP}/cases/{public_id_of(original)}").json()

    assert customer.get(f"{APP}/cases").json() == []
    assert (body["case"], body["findings"]) == (None, [])
    assert {step["status"] for step in body["timeline"]} == {"pending"}
    assert LeakCase.objects.count() == 0


def test_an_unconfirmed_copy_does_not_exist_for_a_customer(
    customer: Client, owner: User, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """Operator decision 2026-09-17: no count, no case, no URL, not even a removal receipt.

    An original whose only copy is unconfirmed answers exactly like an original nothing was found
    of, down to the bytes once its own id and title are set aside.
    """
    candidate = make_page("candidate")
    match(original, candidate)
    ReferenceLeak.objects.create(reference=original, page=candidate, status="proposed")
    take_down(candidate, FIRST_CHECK)
    untouched = make_reference(owner, title="Alpha's own original")

    responses = [
        customer.get(f"{APP}/{path}")
        for path in (
            "originals",
            "findings",
            "cases",
            "removals",
            f"cases/{public_id_of(original)}",
        )
    ]

    for response in responses:
        assert response.status_code == 200
        assert candidate.url not in response.content.decode()
    assert [len(response.json()) for response in responses[1:4]] == [0, 0, 0]

    def normalised(reference: Reference) -> dict[str, Any]:
        body: dict[str, Any] = customer.get(f"{APP}/cases/{public_id_of(reference)}").json()
        for key in ("id", "created_at"):
            body["original"].pop(key)
        return body

    assert normalised(original) == normalised(untouched)
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (0, 0)


def test_confirmed_is_one_step_dated_to_the_day(
    customer: Client, original: Reference, make_page: Callable[[str], Page], site: Site
) -> None:
    page = make_page("candidate")
    match(original, page)
    assert steps(customer.get(f"{APP}/cases/{public_id_of(original)}").json())["confirmed"] == {
        "step": "confirmed",
        "status": "pending",
        "at": None,
        "on": None,
        "reason": None,
        "blocked_by_issue": None,
    }

    leak = confirm(original, page)
    body = customer.get(f"{APP}/cases/{public_id_of(original)}").json()
    confirmed = steps(body)["confirmed"]
    [finding] = body["findings"]

    assert (confirmed["status"], confirmed["at"]) == ("done", None)
    assert confirmed["on"] == timezone.localdate(leak.updated_at).isoformat()
    assert DAY.match(confirmed["on"])
    assert finding["confirmed_on"] == confirmed["on"]
    assert body["case"]["opened_on"] == confirmed["on"]
    assert (finding["site"], finding["url"], finding["case_state"]) == (
        site.domain,
        page.url,
        "open",
    )
    assert finding["evidence"] == "fingerprint_match"
    assert (finding["availability"], finding["online"], finding["last_checked"]) == (
        "unknown",
        None,
        None,
    )
    # Internally the stamps stay exact; they just never reach a customer.
    assert CaseFinding.objects.get().verified_at == leak.updated_at


def test_no_customer_field_measures_how_fast_we_review(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("confirmed-leak")
    match(original, page)
    confirm(original, page)
    take_down(page, FIRST_CHECK)

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in keys(child)}
        return set()

    for path in (
        "originals",
        "findings",
        "cases",
        "removals",
        f"cases/{public_id_of(original)}",
        f"originals/{public_id_of(original)}",
    ):
        body = customer.get(f"{APP}/{path}").json()
        assert keys(body) & LATENCY_FIELDS == set(), path
    [entry] = customer.get(f"{APP}/removals").json()
    assert DAY.match(entry["confirmed_on"])


def test_removed_and_rechecked_come_from_the_availability_check(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    online = make_page("still-up")
    gone = make_page("taken-down")
    confirm(original, online)
    confirm(original, gone)
    take_down(gone, FIRST_CHECK)

    timeline = steps(customer.get(f"{APP}/cases/{public_id_of(original)}").json())

    # One of two URLs down is a count, never the case's "removed" step.
    assert timeline["removed"]["status"] == "pending"
    assert timeline["rechecked"]["at"] == "2026-09-10T08:00:00Z"

    take_down(online, LATER_CHECK)
    body = customer.get(f"{APP}/cases/{public_id_of(original)}").json()

    assert body["case"]["state"] == "removed_watching"
    assert steps(body)["removed"]["at"] == "2026-09-14T08:00:00Z"
    assert steps(body)["rechecked"]["at"] == "2026-09-14T08:00:00Z"
    assert CaseFinding.objects.get(page=gone).removed_at == FIRST_CHECK


def test_the_removals_log_lists_only_what_a_human_confirmed_came_down(
    customer: Client, original: Reference, make_page: Callable[[str], Page], site: Site
) -> None:
    unconfirmed = make_page("vanished-candidate")
    match(original, unconfirmed)
    take_down(unconfirmed, FIRST_CHECK)

    assert customer.get(f"{APP}/removals").json() == []
    assert customer.get(f"{APP}/cases").json() == []

    confirmed = make_page("confirmed-and-gone")
    confirm(original, confirmed)
    take_down(confirmed, LATER_CHECK)

    [entry] = customer.get(f"{APP}/removals").json()

    assert (entry["page_id"], entry["site"], entry["url"]) == (
        confirmed.public_id,
        site.domain,
        confirmed.url,
    )
    assert (entry["state"], entry["removed_at"]) == ("removed", "2026-09-14T08:00:00Z")
    assert entry["handled"] is True


def test_a_reupload_is_a_new_finding_on_the_same_case(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """The operator's definition of handled: removed **and** not re-uploaded anywhere."""
    first = make_page("first-upload")
    confirm(original, first)
    take_down(first, FIRST_CHECK)
    assert customer.get(f"{APP}/removals").json()[0]["handled"] is True

    elsewhere = make_page("reuploaded-elsewhere")
    confirm(original, elsewhere)
    body = customer.get(f"{APP}/cases/{public_id_of(original)}").json()

    assert LeakCase.objects.count() == 1, "a re-upload joins the case, it never opens a second one"
    assert body["case"]["state"] == "open"
    assert body["case"]["reopen_count"] == 1
    assert body["case"]["reopened_on"] is not None
    assert {row["page_id"] for row in body["findings"]} == {first.public_id, elsewhere.public_id}
    # The first URL is still gone, but the case is not handled: something is up again.
    [entry] = customer.get(f"{APP}/removals").json()
    assert (entry["page_id"], entry["state"], entry["handled"]) == (
        first.public_id,
        "removed",
        False,
    )
    assert entry["case_state"] == "open"


def test_a_removals_entry_reopens_when_its_own_url_comes_back(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("whack-a-mole")
    confirm(original, page)
    take_down(page, FIRST_CHECK)
    assert customer.get(f"{APP}/removals").json()[0]["state"] == "removed"

    put_back(page, LATER_CHECK)
    [entry] = customer.get(f"{APP}/removals").json()

    assert (entry["state"], entry["handled"]) == ("reopened", False)
    assert entry["reappeared_at"] == "2026-09-14T08:00:00Z"
    assert entry["removed_at"] == "2026-09-10T08:00:00Z", "the first removal is still on the record"
    assert customer.get(f"{APP}/cases").json()[0]["reopen_count"] == 1

    take_down(page, LAST_CHECK)
    [entry] = customer.get(f"{APP}/removals").json()

    assert (entry["state"], entry["handled"]) == ("removed", True)
    assert (entry["removed_at"], entry["reappeared_at"]) == ("2026-09-15T08:00:00Z", None)
    assert customer.get(f"{APP}/cases").json()[0]["reopen_count"] == 1


def test_reconciliation_writes_nothing_the_second_time(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """The case is reconciled on read, so reading it repeatedly must not churn its stamps."""
    page = make_page("confirmed-leak")
    confirm(original, page)
    take_down(page, FIRST_CHECK)

    first = customer.get(f"{APP}/cases/{public_id_of(original)}")
    stamps = (LeakCase.objects.get().updated_at, CaseFinding.objects.get().updated_at)
    second = customer.get(f"{APP}/cases/{public_id_of(original)}")

    assert first.json() == second.json()
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (1, 1)
    assert (LeakCase.objects.get().updated_at, CaseFinding.objects.get().updated_at) == stamps


def test_a_rejected_finding_takes_its_case_with_it(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("not-it-after-all")
    leak = confirm(original, page)
    take_down(page, FIRST_CHECK)
    assert len(customer.get(f"{APP}/removals").json()) == 1
    assert CaseFinding.objects.count() == 1

    leak.status = ReferenceLeak.Status.REJECTED
    leak.save(update_fields=["status"])

    assert customer.get(f"{APP}/findings").json() == []
    assert customer.get(f"{APP}/removals").json() == []
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (0, 0)


def test_findings_of_several_originals_all_carry_their_own_original_id(
    customer: Client, owner: User, original: Reference, make_page: Callable[[str], Page]
) -> None:
    other = make_reference(owner, title="A second original")
    confirm(original, make_page("one"))
    confirm(other, make_page("two"))

    rows = customer.get(f"{APP}/findings").json()

    assert {row["original_id"] for row in rows} == {public_id_of(original), public_id_of(other)}
    assert len(customer.get(f"{APP}/cases").json()) == 2
    assert LeakCase.objects.count() == 2


def test_low_band_noise_is_no_case_until_a_human_looks_at_it(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """Same rule as the originals view: two frames 30 bits apart is not a case to follow."""
    noise = make_page("two-frames-apart")
    match(original, noise, band="low", evidence_frames=1, strength="weak")

    assert customer.get(f"{APP}/cases").json() == []
    assert customer.get(f"{APP}/findings").json() == []
    assert LeakCase.objects.count() == 0

    ReferenceLeak.objects.create(reference=original, page=noise, status="proposed")

    assert customer.get(f"{APP}/findings").json() == []

    ReferenceLeak.objects.filter(page=noise).update(status="confirmed")

    assert len(customer.get(f"{APP}/findings").json()) == 1


def test_the_case_of_a_page_the_crawler_never_checked_says_unknown_not_online(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """`Page.availability` starts as `unknown`, and "unknown" is neither online nor gone."""
    page = make_page("never-checked")
    page.availability = Page.Availability.UNKNOWN
    page.save(update_fields=["availability"])
    confirm(original, page)

    [finding] = customer.get(f"{APP}/findings").json()
    body = customer.get(f"{APP}/cases/{public_id_of(original)}").json()

    assert (finding["availability"], finding["online"]) == ("unknown", None)
    assert body["case"]["state"] == "open"
    assert steps(body)["rechecked"]["status"] == "pending"
    assert customer.get(f"{APP}/removals").json() == []


def test_the_removals_log_is_newest_first(
    customer: Client, owner: User, original: Reference, make_page: Callable[[str], Page]
) -> None:
    older = make_page("older-removal")
    newer = make_page("newer-removal")
    confirm(original, older)
    confirm(original, newer)
    take_down(older, FIRST_CHECK)
    take_down(newer, LAST_CHECK)

    entries = customer.get(f"{APP}/removals").json()

    assert [entry["page_id"] for entry in entries] == [newer.public_id, older.public_id]
    assert all(entry["handled"] for entry in entries)


def test_a_case_opened_when_everything_was_already_gone_counts_no_reopen(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("gone-before-we-looked")
    confirm(original, page)
    take_down(page, FIRST_CHECK)

    [case] = customer.get(f"{APP}/cases").json()

    assert case["state"] == "removed_watching"
    assert (case["reopen_count"], case["reopened_on"]) == (0, None)
    assert case["removed_at"] == "2026-09-10T08:00:00Z"
    assert DAY.match(case["opened_on"])


def test_a_removal_the_crawler_never_stamped_falls_back_to_now(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """A page marked removed without a check date still gets an honest observation date: ours."""
    page = make_page("removed-without-a-date")
    page.availability = Page.Availability.REMOVED
    page.save(update_fields=["availability"])
    confirm(original, page)
    before = timezone.now()

    [entry] = customer.get(f"{APP}/removals").json()
    removed_at = CaseFinding.objects.get().removed_at

    assert entry["last_checked"] is None
    assert removed_at is not None and removed_at >= before


def test_a_second_signal_candidate_is_not_a_finding_until_someone_confirms_it(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """ADR 0037: an embedding candidate is a question for staff, never a customer's finding."""
    page = make_page("proposed-by-the-second-signal")
    match(
        original,
        page,
        candidate_source=ReferenceMatch.CandidateSource.EMBEDDING,
        band="review",
        evidence_frames=0,
        matched_frames=0,
        loose_frames=0,
        strength="weak",
        best_distance=58,
    )

    assert customer.get(f"{APP}/findings").json() == []
    assert customer.get(f"{APP}/cases").json() == []


def test_a_confirmed_second_signal_candidate_never_claims_a_fingerprint_match(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """A human confirmed the pictures; no fingerprint matched, and the customer is told so."""
    page = make_page("confirmed-by-a-person")
    match(
        original,
        page,
        candidate_source=ReferenceMatch.CandidateSource.EMBEDDING,
        band="review",
        evidence_frames=0,
        matched_frames=0,
        loose_frames=0,
        strength="weak",
        best_distance=58,
        status="confirmed",
        decided_by="human",
    )

    [finding] = customer.get(f"{APP}/findings").json()
    assert finding["page_id"] == page.public_id
    assert finding["evidence"] == "no_fingerprint_match"
    assert finding["extent"] == "not_determined"


def test_a_metadata_candidate_is_not_a_finding_until_someone_confirms_it(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """ADR 0040: the third signal compared no pictures at all; it is a question for staff only."""
    page = make_page("proposed-by-the-third-signal")
    match(
        original,
        page,
        candidate_source=ReferenceMatch.CandidateSource.METADATA,
        band="review",
        evidence_frames=0,
        matched_frames=0,
        loose_frames=0,
        strength="weak",
        best_distance=256,
    )

    assert customer.get(f"{APP}/findings").json() == []
    assert customer.get(f"{APP}/cases").json() == []


def test_a_confirmed_metadata_candidate_never_claims_a_fingerprint_match(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    """A human judged the pictures; no fingerprint matched, and the customer is told so."""
    page = make_page("metadata-confirmed-by-a-person")
    match(
        original,
        page,
        candidate_source=ReferenceMatch.CandidateSource.METADATA,
        band="review",
        evidence_frames=0,
        matched_frames=0,
        loose_frames=0,
        strength="weak",
        best_distance=256,
        status="confirmed",
        decided_by="human",
    )

    [finding] = customer.get(f"{APP}/findings").json()
    assert finding["page_id"] == page.public_id
    assert finding["evidence"] == "no_fingerprint_match"
    assert finding["extent"] == "not_determined"


#: How we process and how sure we are: never in a customer payload (operator decision 2026-09-17).
PROCESSING_FIELDS = {"band", "evidence_frames", "confidence", "strength", "matched_frames"}


def test_why_it_is_yours_is_a_code_and_never_says_how_sure_we_are(
    customer: Client, original: Reference, make_page: Callable[[str], Page]
) -> None:
    matched = make_page("fingerprints-match")
    match(original, matched, status="confirmed", band="review", evidence_frames=2)
    by_hand = make_page("found-by-hand")
    confirm(original, by_hand)
    noise = make_page("noise-then-confirmed")
    match(original, noise, band="low", evidence_frames=1, strength="weak")
    confirm(original, noise)

    responses = [
        customer.get(f"{APP}/findings"),
        customer.get(f"{APP}/cases"),
        customer.get(f"{APP}/cases/{public_id_of(original)}"),
        customer.get(f"{APP}/removals"),
        customer.get(f"{APP}/originals"),
        customer.get(f"{APP}/originals/{public_id_of(original)}"),
    ]

    evidence = {row["page_id"]: row["evidence"] for row in responses[0].json()}
    assert evidence == {
        matched.public_id: "fingerprint_match",
        by_hand.public_id: "no_fingerprint_match",
        noise.public_id: "no_fingerprint_match",
    }
    detail = {row["page_id"]: row["evidence"] for row in responses[5].json()["findings"]}
    assert detail == evidence

    def keys(node: Any) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {key for value in node.values() for key in keys(value)}
        if isinstance(node, list):
            return {key for value in node for key in keys(value)}
        return set()

    for response in responses:
        assert keys(response.json()) & PROCESSING_FIELDS == set()
        assert not {"high", "review", "low"} & set(
            re.findall(r'"([a-z_]+)"', response.content.decode())
        )
