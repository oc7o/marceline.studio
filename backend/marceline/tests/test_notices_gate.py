"""The notice gate (issue #33, ADR 0035): every reason a finding may not go into a draft."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import User

from accounts.models import Organisation
from index.models import Page
from marceline.models import OriginalOwnership
from marceline.notices.gate import (
    FindingBlocker,
    FindingFacts,
    file_matches,
    finding_blockers,
    finding_gate_blockers,
    finding_gate_facts,
)
from marceline.tests.app_helpers import add_member, make_organisation, make_reference
from marceline.tests.notices_helpers import make_hosting_record, playable, rights_ready
from marceline.tests.test_app_original_groups import twin
from marceline.tests.test_app_originals import match
from matching.copy_extent import CopyExtent
from matching.fingerprint import PIPELINE_V0
from matching.models import Reference, ReferenceMatch

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)

CLEAR = FindingFacts(
    now=NOW,
    match_confirmed_high=True,
    page_availability=Page.Availability.PLAYABLE,
    availability_checked_at=NOW,
    licensed_partner=False,
    extent_requires_review=False,
    exception_confirmed=False,
    has_supported_channel=True,
    max_availability_age=timedelta(hours=48),
)


def test_a_clear_finding_has_no_blockers() -> None:
    assert finding_blockers(CLEAR) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"match_confirmed_high": False}, [FindingBlocker.NO_HIGH_CONFIRMED_MATCH]),
        (
            {"page_availability": Page.Availability.REMOVED},
            [FindingBlocker.PAGE_NOT_AVAILABLE],
        ),
        (
            {"page_availability": Page.Availability.UNKNOWN},
            [FindingBlocker.PAGE_NOT_AVAILABLE],
        ),
        ({"availability_checked_at": None}, [FindingBlocker.AVAILABILITY_NOT_RECENT]),
        (
            {"availability_checked_at": NOW - timedelta(hours=49)},
            [FindingBlocker.AVAILABILITY_NOT_RECENT],
        ),
        ({"availability_checked_at": NOW - timedelta(hours=47)}, []),
        ({"licensed_partner": True}, [FindingBlocker.LICENSED_PARTNER_MATCH]),
        (
            {"extent_requires_review": True, "exception_confirmed": False},
            [FindingBlocker.EXCEPTION_REVIEW_REQUIRED],
        ),
        ({"extent_requires_review": True, "exception_confirmed": True}, []),
        ({"has_supported_channel": False}, [FindingBlocker.NO_SUPPORTED_CHANNEL]),
    ],
)
def test_each_missing_fact_is_one_blocker(change: dict[str, object], expected: list[str]) -> None:
    assert finding_blockers(replace(CLEAR, **change)) == expected  # type: ignore[arg-type]


def test_blockers_come_in_a_fixed_order() -> None:
    facts = replace(
        CLEAR,
        match_confirmed_high=False,
        page_availability=Page.Availability.REMOVED,
        licensed_partner=True,
        extent_requires_review=True,
        has_supported_channel=False,
    )

    assert finding_blockers(facts) == [
        FindingBlocker.NO_HIGH_CONFIRMED_MATCH,
        FindingBlocker.PAGE_NOT_AVAILABLE,
        FindingBlocker.LICENSED_PARTNER_MATCH,
        FindingBlocker.EXCEPTION_REVIEW_REQUIRED,
        FindingBlocker.NO_SUPPORTED_CHANNEL,
    ]


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def staff(db: None) -> User:
    return User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)


def test_from_the_database_review_band_never_reaches_high_confirmed(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    match(reference, page, band="review", status="confirmed")
    verdict = file_matches(organisation, reference.sha256, [page.pk])[page.pk]

    facts = finding_gate_facts(organisation, reference, page, verdict, now=NOW)

    assert facts.match_confirmed_high is False


def test_from_the_database_a_fully_clear_finding_has_no_blockers(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    match(
        reference,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    rights_ready(organisation, reference.sha256, member, staff)
    verdict = file_matches(organisation, reference.sha256, [page.pk])[page.pk]

    assert finding_gate_blockers(organisation, reference, page, verdict, now=NOW) == []


def test_from_the_database_an_excerpt_needs_the_per_finding_exception_tick(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    match(
        reference,
        page,
        band="high",
        status="confirmed",
        copy_extent="excerpt",
        copy_extent_version=1,
    )
    rights_ready(organisation, reference.sha256, member, staff)
    verdict = file_matches(organisation, reference.sha256, [page.pk])[page.pk]

    blockers = finding_gate_blockers(organisation, reference, page, verdict, now=NOW)
    assert "exception_review_required" in blockers

    blockers = finding_gate_blockers(
        organisation, reference, page, verdict, exception_confirmed=True, now=NOW
    )
    assert "exception_review_required" not in blockers


def test_from_the_database_rights_blockers_pass_through_unchanged(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    match(reference, page, band="high", status="confirmed")
    verdict = file_matches(organisation, reference.sha256, [page.pk])[page.pk]

    blockers = finding_gate_blockers(organisation, reference, page, verdict, now=NOW)

    assert "claimant_type_missing" in blockers
    assert "identity_not_verified" in blockers


# --- file_matches: one verdict over every reference of the file the organisation owns -----------


def test_file_matches_takes_a_confirmation_from_any_reference_of_the_file(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    match(reviewed, page, band="high", status="confirmed")
    match(representative, page, band="high", status="proposed")

    verdict = file_matches(organisation, reviewed.sha256, [page.pk])[page.pk]

    assert (verdict.confirmed_high, verdict.rejected, verdict.eligible) == (True, False, True)


def test_file_matches_lets_a_rejection_on_any_reference_veto(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    """A human's explicit "not the same work" is not overruled by another sampling grid."""
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    match(reviewed, page, band="high", status="confirmed")
    match(representative, page, band="high", status="rejected")

    verdict = file_matches(organisation, reviewed.sha256, [page.pk])[page.pk]

    assert (verdict.confirmed_high, verdict.rejected, verdict.eligible) == (True, True, False)


def test_file_matches_keeps_the_most_cautious_extent_whatever_the_row_order(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    match(
        reviewed,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    match(
        representative,
        page,
        band="high",
        status="confirmed",
        copy_extent="excerpt",
        copy_extent_version=1,
    )

    verdict = file_matches(organisation, reviewed.sha256, [page.pk])[page.pk]

    assert verdict.extent == CopyExtent.EXCERPT
    assert verdict.requires_exception_review is True


def test_file_matches_never_lets_a_second_signal_candidate_speak_for_the_file(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    """ADR 0037: an embedding candidate holds no evidence, so the file's verdict ignores it.

    Left in, it would decide twice over: its `unknown` extent is the most cautious one of the rows,
    and a human rejecting a pair the machine only guessed at would veto the confirmation that a
    real match row carries. On its own it is no verdict at all, which the gate reads as
    `no_high_confirmed_match`.
    """
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    match(
        reviewed,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    match(
        representative,
        page,
        candidate_source=ReferenceMatch.CandidateSource.EMBEDDING,
        band="review",
        status="rejected",
        evidence_frames=0,
        matched_frames=0,
        loose_frames=0,
        strength="weak",
        best_distance=58,
    )

    verdict = file_matches(organisation, reviewed.sha256, [page.pk])[page.pk]

    assert (verdict.confirmed_high, verdict.rejected, verdict.eligible) == (True, False, True)
    assert verdict.extent == CopyExtent.FULL_COPY

    ReferenceMatch.objects.filter(band=ReferenceMatch.Band.HIGH).delete()
    assert file_matches(organisation, reviewed.sha256, [page.pk]) == {}


def test_a_rejected_metadata_candidate_cannot_veto_a_notice(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    """ADR 0040, the same wall as the second signal's: metadata compared no pictures at all.

    A human rejecting a pair the machine guessed at from a duration and a title says nothing about
    the file, so it may not veto the confirmation a real frame match carries.
    """
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("metadata-candidate"))
    match(
        reviewed,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    match(
        representative,
        page,
        candidate_source=ReferenceMatch.CandidateSource.METADATA,
        band="review",
        status="rejected",
        evidence_frames=0,
        matched_frames=0,
        loose_frames=0,
        strength="weak",
        best_distance=256,
    )

    verdict = file_matches(organisation, reviewed.sha256, [page.pk])[page.pk]

    assert (verdict.confirmed_high, verdict.rejected, verdict.eligible) == (True, False, True)
    assert verdict.extent == CopyExtent.FULL_COPY


def test_file_matches_reads_only_references_this_organisation_owns(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    """One file has one owner, but a reference can be moved away (`assign_originals`).

    A decision on a row this organisation does not own is not a decision about *its* notice: no
    entry at all, which the gate reads as `no_high_confirmed_match`.
    """
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    match(reviewed, page, band="high", status="confirmed")
    assert file_matches(organisation, reviewed.sha256, [page.pk])[page.pk].eligible

    OriginalOwnership.objects.filter(pk=reviewed.pk).delete()

    assert file_matches(organisation, representative.sha256, [page.pk]) == {}


def test_file_matches_is_empty_without_pages(organisation: Organisation, member: User) -> None:
    reference = make_reference(member)

    assert file_matches(organisation, reference.sha256, []) == {}
