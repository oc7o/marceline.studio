"""The third signal aims a fetch and never asks a human (ADR 0040 and its 2026-09-18 amendment).

Every test here is about a boundary, not about retrieval quality. Two of them are the amendment
itself and must never be deleted without a new operator decision:

- `test_the_signal_puts_nothing_in_front_of_a_human` — a run writes no match, no leak, nothing a
  reviewer could ever be shown. The output is a browser-capture candidate, after which PDQ decides
  by itself;
- `test_a_page_the_media_gate_refuses_is_never_aimed_at` — a metadata candidate may not become a
  way to reach a page the ordinary path would refuse. The fields this signal reads are the pirate
  site's own text; they may buy a *look*, never a permission.

The guards that keep such a row out of evidence are asserted here too, although nothing writes one
any more: they are what makes "nothing writes one" an invariant rather than a fact about today's
code.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from catalog.models import Creator
from index.models import FrameHash, Media, Page
from index.services import page_media_allowed
from matching import metadata_candidates, triage
from matching.calibration import apply_bands
from matching.capture_triage import GateSignals, collect_candidates, refresh_candidates
from matching.confidence import HIGH, LOW, REVIEW, Thresholds, band_for
from matching.models import (
    BrowserCaptureCandidate,
    Cluster,
    ClusterMember,
    Reference,
    ReferenceLeak,
    ReferenceMatch,
)
from matching.references import match_references

pytestmark = pytest.mark.django_db

DURATION = 600.0
# Two seconds off: inside this signal's cast clause (3 s) and outside the exact-duration triage
# source of ADR 0016 (1.5 s), so these tests see the metadata source on its own. A page that is
# creator-linked, single-thumbed and exactly as long as the original is queued for capture by
# `exact_duration_single_thumb` whatever the third signal says — that source is older and stands.
AIMED_DURATION = DURATION + 2.0
NOW = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)
NO_DISTANCE = 256


@pytest.fixture
def performer() -> Creator:
    return Creator.objects.create(name="Metadata Performer")


@pytest.fixture
def original(performer: Creator) -> Reference:
    reference = Reference.objects.create(
        creator=performer,
        title="the long winter drive",
        sha256="c" * 64,
        frame_fps=1.0,
        duration_s=DURATION,
    )
    reference.creators.set([performer])
    return reference


@pytest.fixture
def make_scored_page(make_page: Callable[[str], Page]) -> Callable[..., Page]:
    """A page carrying the metadata this signal reads: a duration, a cast and a title."""

    def _make(
        slug: str,
        *,
        duration: float | None = DURATION,
        cast: Creator | None = None,
        title: str | None = None,
        frames: int = 0,
    ) -> Page:
        page = make_page(slug)
        page.raw_extraction = {} if duration is None else {"duration_s": duration}
        page.title = slug if title is None else title
        page.save(update_fields=("raw_extraction", "title"))
        if cast is not None:
            page.creators.add(cast)
        if frames:
            media = Media.objects.create(
                page=page,
                kind=Media.Kind.POSTER,
                url=f"https://cdn.example.test/{slug}.jpg",
                status=Media.Status.DONE,
            )
            FrameHash.objects.bulk_create(
                FrameHash(media=media, frame_index=index, pdq=bytes(32), quality=100)
                for index in range(frames)
            )
        return page

    return _make


def aimed(**kwargs: object) -> list[BrowserCaptureCandidate]:
    """Refresh the capture queue with the third signal on and return its metadata candidates."""
    refresh_candidates(NOW, gate_signals=GateSignals(), metadata=True, **kwargs)  # type: ignore[arg-type]
    return list(
        BrowserCaptureCandidate.objects.filter(
            reason=BrowserCaptureCandidate.Reason.METADATA_CANDIDATE
        ).order_by("pk")
    )


def skips(metadata: bool = True) -> dict[str, int]:
    return dict(collect_candidates(GateSignals(), metadata=metadata).skips)


# --- what the signal now produces --------------------------------------------------------------


def test_a_shared_performer_and_a_matching_duration_aim_a_capture(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    page = make_scored_page("leak", duration=AIMED_DURATION, cast=performer, frames=1)

    rows = aimed()

    assert len(rows) == 1
    row = rows[0]
    assert (row.page_id, row.reference_id) == (page.pk, original.pk)
    assert row.status == BrowserCaptureCandidate.Status.QUEUED
    source = row.signals["sources"][0]
    assert source["reason"] == triage.METADATA_CANDIDATE
    assert source["metadata_reason"] == "cast_and_duration"
    assert source["component"] == triage.METADATA_CANDIDATE_WEIGHT
    assert source["corroborated_by"] is None


def test_the_signal_puts_nothing_in_front_of_a_human(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    """The amendment, asserted: no review row of any kind, by any route.

    A capture candidate is a fetch instruction the worker reads, not a question a reviewer is
    shown, and the only thing that can follow it is an ordinary PDQ match with evidence frames.
    """
    make_scored_page("silent", duration=AIMED_DURATION, cast=performer, frames=1)

    report = metadata_candidates.aim()
    rows = aimed()

    assert report.pairs == 1
    assert len(rows) == 1
    assert not ReferenceMatch.objects.exists()
    assert not ReferenceLeak.objects.exists()
    # The module has no writer at all: scoring and reporting is all it does.
    assert not hasattr(metadata_candidates, "write_candidates")
    assert not hasattr(metadata_candidates, "refresh")


def test_the_report_writes_nothing_and_names_the_pages_worth_a_capture(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    make_scored_page("frameless", duration=DURATION, cast=performer)
    make_scored_page("thumbed", duration=DURATION, cast=performer, frames=1)

    report = metadata_candidates.aim()

    assert (report.pairs, report.frame_poor_pages, report.no_frame_pages) == (2, 2, 1)
    assert "2 pairs admitted (cast_and_duration 2)" in report.line()
    assert not ReferenceMatch.objects.exists()
    assert not BrowserCaptureCandidate.objects.exists()


def test_the_switch_decides_whether_the_signal_aims_anything(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    make_scored_page("switched", duration=AIMED_DURATION, cast=performer, frames=1)

    refresh_candidates(NOW, gate_signals=GateSignals())

    assert not BrowserCaptureCandidate.objects.exists()
    with override_settings(MATCHING_METADATA_CANDIDATES_ENABLED=True):
        refresh_candidates(NOW, gate_signals=GateSignals())
    assert BrowserCaptureCandidate.objects.count() == 1


# --- what it may never reach ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"safety_score": 0.5}, id="scored below the gate"),
        pytest.param({"safety_score": None}, id="never scored"),
    ],
)
def test_a_page_the_media_gate_refuses_is_never_aimed_at(
    original: Reference,
    performer: Creator,
    make_scored_page: Callable[..., Page],
    fields: dict[str, object],
) -> None:
    """The load-bearing rule of the amendment (ADR 0018 + ADR 0040 amendment).

    A browser pulls far more of a page than a poster fetch does, so it can never be allowed where
    a poster is not. The page below has the tightest possible coincidence — same cast, same
    duration to the second — and it still aims nothing.
    """
    page = make_scored_page("gated", duration=AIMED_DURATION, cast=performer, frames=1)
    Page.objects.filter(pk=page.pk).update(**fields)
    page.refresh_from_db()
    assert not page_media_allowed(page.safety_status, page.safety_score)

    assert aimed() == []
    assert skips()[triage.MEDIA_GATE] == 1
    assert not BrowserCaptureCandidate.objects.exists()


@pytest.mark.parametrize(
    "status", [Page.SafetyStatus.BLOCKED, Page.SafetyStatus.REVIEW], ids=["blocked", "in review"]
)
def test_a_safety_flagged_page_is_never_aimed_at(
    original: Reference,
    performer: Creator,
    make_scored_page: Callable[..., Page],
    status: str,
) -> None:
    page = make_scored_page("flagged", duration=AIMED_DURATION, cast=performer, frames=1)
    Page.objects.filter(pk=page.pk).update(safety_status=status)

    assert aimed() == []


def test_a_removed_page_and_an_inactive_page_are_never_aimed_at(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    gone = make_scored_page("gone", duration=AIMED_DURATION, cast=performer, frames=1)
    Page.objects.filter(pk=gone.pk).update(availability=Page.Availability.REMOVED)
    untargeted = make_scored_page("untargeted", duration=AIMED_DURATION, cast=performer, frames=1)
    Page.objects.filter(pk=untargeted.pk).update(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED
    )

    assert aimed() == []


def test_a_page_pdq_has_already_seen_properly_is_not_aimed_at(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    """Where PDQ had a proper look and said nothing, the site's own text is no reason to look
    again with a browser."""
    make_scored_page(
        "well-covered", duration=AIMED_DURATION, cast=performer, frames=triage.FEW_FRAME_HASHES
    )

    assert aimed() == []
    assert skips()[triage.ENOUGH_FRAMES] == 1


def test_a_pair_another_signal_owns_is_not_aimed_at(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    page = make_scored_page("already-matched", duration=AIMED_DURATION, cast=performer, frames=1)
    ReferenceMatch.objects.create(
        reference=original,
        page=page,
        candidate_source=ReferenceMatch.CandidateSource.PDQ,
        band=HIGH,
        confidence=0.9,
        best_distance=4,
        best_reference_timestamp_s=1.0,
        matched_frames=3,
        evidence_frames=3,
        page_frames=1,
        status=ReferenceMatch.Status.CONFIRMED,
    )

    assert aimed() == []
    assert skips()[triage.PAIR_ALREADY_PROPOSED] == 1


def test_a_page_whose_media_is_still_pending_waits_for_the_cheap_path(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    """The division between the two fetch paths: while an HTTP fetch is queued, it runs first."""
    page = make_scored_page("pending", duration=AIMED_DURATION, cast=performer)
    Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url="https://cdn.example.test/p.jpg", eligible_at=NOW
    )

    assert aimed() == []
    assert skips()[triage.MEDIA_PENDING] == 1


# --- corroboration ------------------------------------------------------------------------------


def test_a_cluster_link_to_a_confirmed_page_corroborates_and_ranks_higher(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    """A signal the candidate did not come from: PDQ's own grouping, or a human's confirmation.

    It is a better aim, never a stronger claim — the capture still has to produce frames and PDQ
    still has to match them.
    """
    confirmed = make_scored_page("confirmed", duration=DURATION, cast=performer, frames=1)
    ReferenceLeak.objects.create(
        reference=original, page=confirmed, status=ReferenceLeak.Status.CONFIRMED
    )
    sibling = make_scored_page("sibling", duration=AIMED_DURATION, cast=performer, frames=1)
    cluster = Cluster.objects.create()
    for page in (confirmed, sibling):
        ClusterMember.objects.create(cluster=cluster, page=page)

    rows = {row.page_id: row for row in aimed()}

    assert sibling.pk in rows
    source = rows[sibling.pk].signals["sources"][0]
    assert source["component"] == triage.METADATA_CORROBORATED_WEIGHT
    assert source["corroborated_by"] == "cluster"
    assert source["seed_page_id"] == confirmed.pk


def test_a_cluster_link_to_a_page_confirmed_for_another_original_does_not_corroborate(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    other = Reference.objects.create(
        creator=performer, title="another film", sha256="e" * 64, frame_fps=1.0, duration_s=90.0
    )
    confirmed = make_scored_page("other-confirmed", duration=92.0, cast=performer, frames=1)
    ReferenceLeak.objects.create(
        reference=other, page=confirmed, status=ReferenceLeak.Status.CONFIRMED
    )
    sibling = make_scored_page("other-sibling", duration=AIMED_DURATION, cast=performer, frames=1)
    cluster = Cluster.objects.create()
    for page in (confirmed, sibling):
        ClusterMember.objects.create(cluster=cluster, page=page)

    rows = {row.page_id: row for row in aimed()}

    assert rows[sibling.pk].signals["sources"][0]["component"] == triage.METADATA_CANDIDATE_WEIGHT


# --- the rule itself, unchanged by the amendment --------------------------------------------------


def test_a_page_with_no_duration_proposes_nothing(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    make_scored_page("no-duration", duration=None, cast=performer)
    report = metadata_candidates.aim()
    assert (report.pages, report.pairs) == (0, 0)
    assert "no active pages with a parsed duration" in report.empty_reason()
    assert aimed() == []


def test_a_clip_is_not_proposed_and_nothing_is_written_against_it(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    """Duration is an indicator, never a hard filter: a clip is simply not this signal's business.

    The point of the test is the *absence* of anything, not a rejection: nothing here may write a
    negative verdict that another signal or a human would then have to argue with.
    """
    make_scored_page("clip", duration=45.0, cast=performer, frames=1)
    report = metadata_candidates.aim()
    assert report.pairs == 0
    assert "proposes nothing when the site text disagrees" in report.empty_reason()
    assert aimed() == []
    assert not ReferenceMatch.objects.exists()


def test_the_title_clause_reaches_a_page_that_credits_nobody(
    original: Reference, make_scored_page: Callable[..., Page]
) -> None:
    make_scored_page(
        "unlinked",
        duration=DURATION + 15.0,
        cast=None,
        title="the long winter drive home",
        frames=1,
    )
    report = metadata_candidates.aim()
    assert report.by_reason == {"duration_and_title": 1}
    assert len(aimed()) == 1


def test_an_inactive_page_is_never_scored(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    page = make_scored_page("inactive", duration=DURATION, cast=performer)
    Page.objects.filter(pk=page.pk).update(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED
    )
    report = metadata_candidates.aim()
    assert (report.pages, report.pairs) == (0, 0)


def test_a_photo_collection_is_never_scored(
    performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    """A collection has no duration by constraint, so neither clause can fire on one."""
    Reference.objects.create(
        creator=performer, title="a photo set", sha256="d" * 64, kind=Reference.Kind.PHOTOS
    )
    make_scored_page("photo-page", duration=DURATION, cast=performer)
    report = metadata_candidates.aim()
    assert (report.references, report.pairs) == (0, 0)
    assert "not photo collections" in report.empty_reason()


def test_the_per_original_cap_keeps_the_tightest_coincidences(
    original: Reference,
    performer: Creator,
    make_scored_page: Callable[..., Page],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A data accident becomes a logged number, not a flooded capture queue."""
    monkeypatch.setattr(metadata_candidates, "MAX_CANDIDATES_PER_REFERENCE", 2)
    for offset in (2.0, 2.2, 2.5, 2.8):
        make_scored_page(f"cap-{offset}", duration=DURATION + offset, cast=performer, frames=1)
    report = metadata_candidates.aim()
    assert (report.pairs, report.capped) == (4, 2)
    rows = aimed()
    assert len(rows) == 2
    assert {row.signals["sources"][0]["duration_difference_s"] for row in rows} == {2.0, 2.2}


def test_the_run_is_idempotent(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    make_scored_page("twice", duration=DURATION, cast=performer, frames=1)
    first, second = aimed(), aimed()
    assert [row.pk for row in first] == [row.pk for row in second]
    assert BrowserCaptureCandidate.objects.count() == 1


def test_a_candidate_that_loses_its_support_leaves_the_queue(
    original: Reference, performer: Creator, make_scored_page: Callable[..., Page]
) -> None:
    """A silent death: the page keeps its capture history, the pair becomes no verdict at all."""
    page = make_scored_page("fading", duration=AIMED_DURATION, cast=performer, frames=1)
    assert len(aimed()) == 1

    Page.objects.filter(pk=page.pk).update(raw_extraction={"duration_s": 45.0})
    row = aimed()[0]
    assert row.status == BrowserCaptureCandidate.Status.EXPIRED  # left the queue, unvisited
    assert "no capture source left" in row.last_error
    assert not ReferenceMatch.objects.exists()


# --- the three guards, still standing -------------------------------------------------------------


def test_the_review_band_is_the_ceiling_in_the_database(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("forced")
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceMatch.objects.create(
            reference=original,
            page=page,
            candidate_source=ReferenceMatch.CandidateSource.METADATA,
            band=HIGH,
            best_distance=NO_DISTANCE,
            best_reference_timestamp_s=0.0,
            matched_frames=0,
            page_frames=0,
        )


def test_a_metadata_row_may_not_claim_evidence_in_the_database(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("forced2")
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceMatch.objects.create(
            reference=original,
            page=page,
            candidate_source=ReferenceMatch.CandidateSource.METADATA,
            band=REVIEW,
            best_distance=NO_DISTANCE,
            best_reference_timestamp_s=0.0,
            matched_frames=0,
            evidence_frames=2,
            page_frames=0,
        )


def test_band_for_never_scores_a_metadata_candidate_up_or_hides_it() -> None:
    thresholds = Thresholds(high=0.75, review=0.40)
    assert band_for(0.99, 5, thresholds, metadata_only=True) == REVIEW
    assert band_for(0.0, 0, thresholds, metadata_only=True) == REVIEW
    # The ordinary rules are untouched.
    assert band_for(0.99, 5, thresholds) == HIGH
    assert band_for(0.0, 0, thresholds) == LOW


def test_calibration_leaves_a_metadata_row_in_the_review_band(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    """Nothing writes such a row any more; a row left from before the amendment stays put."""
    page = make_page("legacy")
    ReferenceMatch.objects.create(
        reference=original,
        page=page,
        candidate_source=ReferenceMatch.CandidateSource.METADATA,
        band=REVIEW,
        best_distance=NO_DISTANCE,
        best_reference_timestamp_s=0.0,
        matched_frames=0,
        page_frames=0,
    )

    apply_bands(Thresholds(high=0.5, review=0.2))

    assert ReferenceMatch.objects.get().band == REVIEW


def test_a_matching_run_neither_reads_nor_deletes_a_row_left_from_before(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    """`match_references` owns the PDQ rows and leaves another signal's candidates alone."""
    page = make_page("legacy-kept")
    ReferenceMatch.objects.create(
        reference=original,
        page=page,
        candidate_source=ReferenceMatch.CandidateSource.METADATA,
        band=REVIEW,
        best_distance=NO_DISTANCE,
        best_reference_timestamp_s=0.0,
        matched_frames=0,
        page_frames=0,
    )

    match_references([original], full=True)

    row = ReferenceMatch.objects.get()
    assert row.candidate_source == ReferenceMatch.CandidateSource.METADATA
    assert (row.band, row.evidence_frames) == (REVIEW, 0)
