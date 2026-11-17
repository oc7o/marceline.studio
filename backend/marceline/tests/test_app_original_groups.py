"""One original per video: the references of one file are one customer original.

A re-fingerprinted video is stored as several `Reference` rows with the same sha256 — a v0 and a v1
grid (issue #70), a 1 and a 2 fps sampling (ADR 0022 amendment). The operator's test organisation
saw 41 originals for 39 videos. Customers see one original per file under one public id (ADR 0033)
that no member's primary key replaces, findings merged across the references.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.test.utils import override_settings

from accounts.models import Organisation
from index.models import Page
from marceline.app import cases
from marceline.app.ownership import owned_original, owned_originals
from marceline.tests.app_helpers import (
    APP,
    UNKNOWN_ORIGINAL,
    add_member,
    client_for,
    grouped,
    make_organisation,
    make_reference,
    own,
    public_id_of,
)
from matching.copy_extent import CLASSIFIER_VERSION
from matching.fingerprint import BROWSER_PIPELINE_V0, PIPELINE, PIPELINE_V0
from matching.models import CaseFinding, LeakCase, Reference, ReferenceLeak, ReferenceMatch

REGISTERED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FIRST_CHECK = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def owner(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def customer(owner: User) -> Client:
    return client_for(owner)


def registered(reference: Reference, days: int) -> Reference:
    Reference.objects.filter(pk=reference.pk).update(created_at=REGISTERED + timedelta(days=days))
    reference.refresh_from_db()
    return reference


def v0_of(owner: User, title: str = "The video") -> Reference:
    reference = make_reference(owner, title=f"{title} (v0)")
    Reference.objects.filter(pk=reference.pk).update(pipeline=PIPELINE_V0)
    return registered(reference, 0)


def twin(
    of: Reference,
    organisation: Organisation,
    *,
    pipeline: str = PIPELINE,
    fps: float = 1,
    days: int = 1,
    title: str | None = None,
) -> Reference:
    """Another reference of the same file, owned like the first (inherited on import)."""
    reference = Reference.objects.create(
        creator=of.creator,
        title=title or of.title.replace("(v0)", "(v1)"),
        duration_s=of.duration_s,
        sha256=of.sha256,
        frame_fps=fps,
        pipeline=pipeline,
    )
    return registered(own(reference, organisation), days)


def confirm(reference: Reference, page: Page) -> ReferenceLeak:
    return ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")


def take_down(page: Page, at: datetime) -> None:
    page.availability = Page.Availability.REMOVED
    page.availability_checked_at = at
    page.save(update_fields=["availability", "availability_checked_at"])


# Grouping


def test_the_newest_reference_on_a_current_pipeline_represents_the_file(
    owner: User, organisation: Organisation
) -> None:
    v0 = v0_of(owner)
    v1 = twin(v0, organisation, days=1)
    two_fps = twin(v0, organisation, fps=2, days=2, title="The video (2 fps)")
    # Newer, but on a superseded recipe: never the representative while a current one exists.
    late_browser_v0 = twin(v0, organisation, pipeline=BROWSER_PIPELINE_V0, days=3)
    other = registered(make_reference(owner, title="Another video"), 5)

    originals = owned_originals(organisation)

    assert [original.pk for original in originals] == [other.pk, two_fps.pk]
    assert originals[1].member_ids == [v0.pk, v1.pk, two_fps.pk, late_browser_v0.pk]
    # One public id for the whole file, whichever reference represents it (ADR 0033).
    assert originals[1].public_id == public_id_of(v0) == public_id_of(two_fps)
    assert owned_original(organisation, originals[1].public_id) == originals[1]
    for member in (v0, v1, two_fps, late_browser_v0):
        assert owned_original(organisation, str(member.pk)) is None


def test_a_file_only_on_superseded_recipes_is_still_one_original(
    owner: User, organisation: Organisation
) -> None:
    v0 = v0_of(owner)
    browser_v0 = twin(v0, organisation, pipeline=BROWSER_PIPELINE_V0, days=1)

    [original] = grouped([v0, browser_v0])

    assert original.pk == browser_v0.pk


def test_the_overview_lists_one_original_per_video(
    customer: Client, owner: User, organisation: Organisation
) -> None:
    v0 = v0_of(owner)
    v1 = twin(v0, organisation)

    rows = customer.get(f"{APP}/originals").json()

    assert [(row["id"], row["title"]) for row in rows] == [(public_id_of(v1), "The video (v1)")]


# Merged findings


def test_findings_are_merged_across_the_references_of_one_file(
    customer: Client,
    owner: User,
    organisation: Organisation,
    make_page: Callable[[str], Page],
) -> None:
    v0 = v0_of(owner)
    v1 = twin(v0, organisation)
    only_v0 = make_page("only-on-the-v0-grid")
    only_v1 = make_page("only-on-the-v1-grid")
    both = make_page("found-on-both")
    confirm(v0, only_v0)
    confirm(v1, only_v1)
    # Confirmed on the old grid, still merely proposed on the new one: confirmed, and once.
    confirm(v0, both)
    ReferenceLeak.objects.create(reference=v1, page=both, status="proposed")
    unconfirmed = make_page("proposed-everywhere")
    ReferenceLeak.objects.create(reference=v1, page=unconfirmed, status="proposed")

    detail = customer.get(f"{APP}/originals/{public_id_of(v1)}").json()
    case = customer.get(f"{APP}/cases/{public_id_of(v1)}").json()

    expected = sorted([only_v0.public_id, only_v1.public_id, both.public_id])
    assert sorted(row["page_id"] for row in detail["findings"]) == expected
    assert detail["original"]["counts"] == {"confirmed": 3, "removed": 0}
    assert sorted(row["page_id"] for row in case["findings"]) == expected
    assert [row["original_id"] for row in customer.get(f"{APP}/cases").json()] == [public_id_of(v1)]
    assert sorted(row["page_id"] for row in customer.get(f"{APP}/findings").json()) == expected
    assert {row["original_id"] for row in customer.get(f"{APP}/findings").json()} == {
        public_id_of(v1)
    }


def test_a_merged_finding_keeps_the_most_cautious_extent_its_references_determined(
    customer: Client,
    owner: User,
    organisation: Organisation,
    make_page: Callable[[str], Page],
) -> None:
    v0 = v0_of(owner)
    v1 = twin(v0, organisation)
    poster_on_v0 = make_page("poster-on-v0-full-on-v1")
    excerpt_on_v1 = make_page("full-on-v0-excerpt-on-v1")
    for reference, page, extent in (
        (v0, poster_on_v0, "unknown"),
        (v1, poster_on_v0, "full_copy"),
        (v0, excerpt_on_v1, "full_copy"),
        (v1, excerpt_on_v1, "excerpt"),
    ):
        ReferenceMatch.objects.create(
            reference=reference,
            page=page,
            best_distance=8,
            matched_frames=4,
            page_frames=6,
            best_reference_timestamp_s=12.0,
            evidence_frames=3,
            strength="strong",
            band="high",
            status="confirmed",
            decided_by="human",
            copy_extent=extent,
            copy_extent_version=CLASSIFIER_VERSION,
        )

    detail = customer.get(f"{APP}/originals/{public_id_of(v1)}").json()

    assert {row["page_id"]: row["extent"] for row in detail["findings"]} == {
        poster_on_v0.public_id: "full_copy",
        excerpt_on_v1.public_id: "excerpt",
    }


def test_a_merged_finding_is_dated_by_the_earliest_observation(
    owner: User, organisation: Organisation, make_page: Callable[[str], Page]
) -> None:
    v0 = v0_of(owner)
    v1 = twin(v0, organisation)
    page = make_page("found-on-both")
    early = confirm(v0, page)
    late = confirm(v1, page)
    ReferenceLeak.objects.filter(pk=early.pk).update(created_at=REGISTERED)
    ReferenceLeak.objects.filter(pk=late.pk).update(created_at=REGISTERED + timedelta(days=3))

    [view] = cases.sync_cases(owned_originals(organisation))

    assert [row.page.pk for row in view.rows] == [page.pk]
    assert view.rows[0].detected_at == REGISTERED
    assert view.case is not None and view.case.opened_at == REGISTERED


# 404 on member ids


def test_no_members_primary_key_names_the_original(
    customer: Client, owner: User, organisation: Organisation, make_page: Callable[[str], Page]
) -> None:
    v0 = v0_of(owner)
    v1 = twin(v0, organisation)
    confirm(v0, make_page("a-copy"))

    for endpoint in ("originals", "cases"):
        missing = customer.get(f"{APP}/{endpoint}/{UNKNOWN_ORIGINAL}")
        for member in (v0, v1):
            by_key = customer.get(f"{APP}/{endpoint}/{member.pk}")
            assert by_key.status_code == 404
            assert by_key.content == missing.content
        assert customer.get(f"{APP}/{endpoint}/{public_id_of(v1)}").status_code == 200


def test_the_public_id_survives_a_new_representative_and_a_retired_twin(
    customer: Client, owner: User, organisation: Organisation
) -> None:
    """The id belongs to the file as the organisation owns it, not to one reference (ADR 0033)."""
    v0 = v0_of(owner)
    before = customer.get(f"{APP}/originals").json()[0]["id"]

    v1 = twin(v0, organisation)
    after_new_representative = customer.get(f"{APP}/originals").json()[0]
    v0.delete()
    after_retirement = customer.get(f"{APP}/originals").json()[0]

    assert after_new_representative["id"] == before
    assert after_new_representative["title"] == v1.title
    assert after_retirement["id"] == before
    assert customer.get(f"{APP}/originals/{before}").status_code == 200


# The case follows the representative


def test_the_case_moves_to_a_newer_representative_and_keeps_its_stamps(
    customer: Client, owner: User, organisation: Organisation, make_page: Callable[[str], Page]
) -> None:
    v0 = v0_of(owner)
    page = make_page("a-copy")
    confirm(v0, page)
    take_down(page, FIRST_CHECK)
    [before] = cases.sync_cases(owned_originals(organisation))
    assert before.case is not None
    stamps = CaseFinding.objects.values_list(
        "pk", "discovered_at", "verified_at", "removed_at", "reappeared_at"
    ).get()

    # The operator imports the v1 re-fingerprint; its owner is inherited.
    v1 = twin(v0, organisation)
    body = customer.get(f"{APP}/cases/{public_id_of(v1)}").json()

    case = LeakCase.objects.get()
    assert (case.pk, case.reference_id) == (before.case.pk, v1.pk)
    assert case.state == LeakCase.State.REMOVED_WATCHING
    assert (
        CaseFinding.objects.values_list(
            "pk", "discovered_at", "verified_at", "removed_at", "reappeared_at"
        ).get()
        == stamps
    )
    assert body["case"]["original_id"] == public_id_of(v1)
    assert [row["url"] for row in customer.get(f"{APP}/removals").json()] == [page.url]


def test_cases_grown_on_two_references_of_one_file_are_merged(
    owner: User, organisation: Organisation, make_page: Callable[[str], Page]
) -> None:
    """Before grouping, the test organisation's twins each had a case of their own."""
    v0 = v0_of(owner)
    v1 = twin(v0, organisation)
    shared = make_page("on-both")
    only_v0 = make_page("only-v0")
    for reference in (v0, v1):
        cases.sync_cases(grouped([reference]))  # nothing yet: no case
    confirm(v0, shared)
    confirm(v0, only_v0)
    cases._sync_case(grouped([v0])[0], FIRST_CHECK)  # the old, ungrouped world
    confirm(v1, shared)
    cases._sync_case(grouped([v1])[0], FIRST_CHECK + timedelta(days=2))
    assert LeakCase.objects.count() == 2
    old_case = LeakCase.objects.get(reference=v0)
    earliest = CaseFinding.objects.filter(page=shared).order_by("discovered_at").first()
    assert earliest is not None

    [view] = cases.sync_cases(owned_originals(organisation))

    case = LeakCase.objects.get()
    assert case.reference_id == v1.pk
    assert view.case is not None and view.case.pk == case.pk
    entries = {entry.page_id: entry for entry in CaseFinding.objects.filter(case=case)}
    assert set(entries) == {shared.pk, only_v0.pk}
    assert entries[shared.pk].discovered_at == earliest.discovered_at
    assert entries[shared.pk].verified_at == min(
        entry.verified_at
        for entry in (earliest, entries[shared.pk])
        if entry.verified_at is not None
    )
    assert not LeakCase.objects.filter(pk=old_case.pk, reference=v0).exists()
    # Idempotent from here on.
    cases.sync_cases(owned_originals(organisation))
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (1, 2)


def test_with_sync_on_read_off_a_case_left_on_the_old_member_is_still_read(
    customer: Client,
    owner: User,
    organisation: Organisation,
    make_page: Callable[[str], Page],
) -> None:
    v0 = v0_of(owner)
    page = make_page("a-copy")
    confirm(v0, page)
    cases.sync_cases(owned_originals(organisation))
    v1 = twin(v0, organisation)

    with override_settings(CASE_SYNC_ON_READ=False):
        body = customer.get(f"{APP}/cases/{public_id_of(v1)}").json()

    assert body["case"]["original_id"] == public_id_of(v1)
    assert [row["page_id"] for row in body["findings"]] == [page.public_id]
    assert LeakCase.objects.get().reference_id == v0.pk  # a read wrote nothing
