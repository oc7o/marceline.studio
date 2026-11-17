"""The second signal proposes and never decides (ADR 0037).

Every test here is about a boundary, not about retrieval quality: an embedding row holds no
evidence, never leaves the review band, never overrides a PDQ match, survives a matching run and
is promoted only by PDQ or by a human.
"""

from collections.abc import Callable

import pytest
from django.db import IntegrityError, transaction

from catalog.models import Creator
from index.models import FrameHash, Media, Page
from matching import embedding_candidates
from matching.calibration import apply_bands
from matching.confidence import HIGH, LOW, REVIEW, Thresholds, band_for
from matching.embedding import EMBEDDING_PIPELINE
from matching.models import EmbeddingCandidate, Reference, ReferenceFrameHash, ReferenceMatch
from matching.references import match_references
from matching.sketch_index import SketchIndex
from matching.tests.hashes import random_hashes
from matching.tests.sketches import flip_bits, random_sketches

pytestmark = pytest.mark.django_db

PDQ = random_hashes(8, seed=41)
SKETCHES = random_sketches(8, seed=42)


@pytest.fixture
def original() -> Reference:
    creator = Creator.objects.create(name="Original Creator")
    reference = Reference.objects.create(
        creator=creator,
        title="an original",
        sha256="a" * 64,
        frame_fps=1.0,
        duration_s=10.0,
        embedding_pipeline=EMBEDDING_PIPELINE,
    )
    for index in range(4):
        ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=index,
            timestamp_s=float(index),
            pdq=PDQ[index],
            quality=90,
            dihedral=0,
            embedding=SKETCHES[index],
        )
    return reference


def add_frames(media: Media, sketches: dict[int, bytes]) -> None:
    """Page frames whose PDQ hashes are nowhere near the original's, but whose sketches are."""
    for index, sketch in sketches.items():
        FrameHash.objects.create(
            media=media,
            frame_index=index,
            pdq=PDQ[4 + index],  # unrelated to every reference hash
            quality=90,
            embedding=sketch,
            embedding_pipeline=EMBEDDING_PIPELINE,
        )


def test_the_sketch_index_finds_the_nearest_and_breaks_ties_on_the_id() -> None:
    index = SketchIndex([7, 3, 9], [SKETCHES[0], SKETCHES[0], SKETCHES[1]])
    ids, distances = index.nearest([SKETCHES[0]], 2)
    assert ids.tolist() == [[3, 7]]
    assert distances.tolist() == [[0, 0]]


def test_the_sketch_index_returns_nothing_for_an_empty_index() -> None:
    ids, distances = SketchIndex([], []).nearest([SKETCHES[0]], 5)
    assert ids.shape == (1, 0)
    assert distances.shape == (1, 0)


def test_rank_neighbours_marks_the_mutual_pairs() -> None:
    """Mutual means both sides list each other: the moment's own nearest frame is the near one."""
    page = [(100, SKETCHES[0]), (101, flip_bits(SKETCHES[0], 200, seed=1))]
    reference = [(1, SKETCHES[0])]
    found = embedding_candidates.rank_neighbours(page, reference, {1: 9}, top_k=1)
    by_frame = {item.frame_id: item for item in found}
    assert by_frame[100].distance == 0
    assert by_frame[100].mutual is True
    # 101 has nowhere else to point, but the moment's own top-1 frame is 100, not 101.
    assert by_frame[101].rank == 1
    assert by_frame[101].mutual is False


def test_one_reference_appears_once_per_page_frame_at_its_best_distance() -> None:
    page = [(100, SKETCHES[1])]
    reference = [(1, SKETCHES[1]), (2, flip_bits(SKETCHES[1], 40, seed=2))]
    found = embedding_candidates.rank_neighbours(page, reference, {1: 9, 2: 9}, top_k=2)
    assert len(found) == 1
    assert found[0].reference_frame_id == 1
    assert found[0].distance == 0


def test_a_page_with_two_mutual_frames_becomes_a_review_band_candidate(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    page = make_page("leak")
    media = make_media(page)
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})

    report = embedding_candidates.refresh()

    assert report.page_frames == 2
    assert report.reference_moments == 4
    assert report.proposed_matches == 1
    assert EmbeddingCandidate.objects.filter(mutual=True).count() == 2
    match = ReferenceMatch.objects.get(reference=original, page=page)
    assert match.candidate_source == ReferenceMatch.CandidateSource.EMBEDDING
    assert match.band == REVIEW
    assert match.status == ReferenceMatch.Status.PROPOSED
    # No evidence of any kind: this row is a question, not an answer.
    assert (match.evidence_frames, match.matched_frames, match.loose_frames) == (0, 0, 0)
    assert match.matched_seconds == []
    assert match.timeline_score is None
    assert match.confidence == 0.0
    assert match.confidence_signals["source"] == "embedding"
    # And the PDQ distance of the pair is recorded, showing how far out it really is.
    assert match.best_distance > 31


def test_one_mutual_frame_is_not_enough(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("single"))
    add_frames(media, {0: SKETCHES[0]})
    report = embedding_candidates.refresh()
    assert report.proposed_matches == 0
    assert not ReferenceMatch.objects.exists()


def test_the_review_band_is_the_ceiling_in_the_database(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("forced")
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceMatch.objects.create(
            reference=original,
            page=page,
            candidate_source=ReferenceMatch.CandidateSource.EMBEDDING,
            band=HIGH,
            best_distance=60,
            best_reference_timestamp_s=0.0,
            matched_frames=0,
            page_frames=1,
        )


def test_an_embedding_row_may_not_claim_evidence_in_the_database(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("forced2")
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceMatch.objects.create(
            reference=original,
            page=page,
            candidate_source=ReferenceMatch.CandidateSource.EMBEDDING,
            band=REVIEW,
            best_distance=60,
            best_reference_timestamp_s=0.0,
            matched_frames=0,
            evidence_frames=2,
            page_frames=1,
        )


def test_band_for_never_scores_an_embedding_candidate_up_or_hides_it() -> None:
    thresholds = Thresholds(high=0.75, review=0.40)
    assert band_for(0.99, 5, thresholds, embedding_only=True) == REVIEW
    assert band_for(0.0, 0, thresholds, embedding_only=True) == REVIEW
    # The ordinary rules are untouched.
    assert band_for(0.99, 5, thresholds) == HIGH
    assert band_for(0.0, 0, thresholds) == LOW


def test_calibration_leaves_embedding_candidates_in_the_review_band(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("calibrate"))
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    embedding_candidates.refresh()
    apply_bands(Thresholds(high=0.5, review=0.2))
    match = ReferenceMatch.objects.get(candidate_source=ReferenceMatch.CandidateSource.EMBEDDING)
    assert match.band == REVIEW


def test_a_pdq_match_owns_the_pair_and_the_signal_stays_out(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    """A real match is never replaced, re-banded or commented on by the second signal."""
    page = make_page("already-matched")
    media = make_media(page)
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    real = ReferenceMatch.objects.create(
        reference=original,
        page=page,
        candidate_source=ReferenceMatch.CandidateSource.PDQ,
        band=HIGH,
        confidence=0.9,
        best_distance=4,
        best_reference_timestamp_s=1.0,
        matched_frames=3,
        evidence_frames=3,
        page_frames=2,
        status=ReferenceMatch.Status.CONFIRMED,
    )
    embedding_candidates.refresh()
    real.refresh_from_db()
    assert real.band == HIGH
    assert real.candidate_source == ReferenceMatch.CandidateSource.PDQ
    assert real.status == ReferenceMatch.Status.CONFIRMED
    assert ReferenceMatch.objects.count() == 1


def test_a_candidate_is_withdrawn_when_its_support_disappears(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("fading"))
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    embedding_candidates.refresh()
    assert ReferenceMatch.objects.count() == 1
    FrameHash.objects.filter(media=media).update(embedding=None, embedding_pipeline="")
    report = embedding_candidates.refresh()
    assert report.removed_matches == 1
    assert not ReferenceMatch.objects.exists()


def test_a_human_decision_on_a_candidate_is_never_withdrawn(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("judged"))
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    embedding_candidates.refresh()
    ReferenceMatch.objects.update(status=ReferenceMatch.Status.REJECTED, decided_by="human")
    FrameHash.objects.filter(media=media).update(embedding=None, embedding_pipeline="")
    embedding_candidates.refresh()
    row = ReferenceMatch.objects.get()
    assert row.status == ReferenceMatch.Status.REJECTED
    assert row.decided_by == "human"


def test_a_matching_run_neither_reads_nor_deletes_an_embedding_candidate(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    """`match_references` owns the PDQ rows and leaves the candidate table alone."""
    media = make_media(make_page("kept"))
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    embedding_candidates.refresh()
    match_references([original], full=True)
    row = ReferenceMatch.objects.get()
    assert row.candidate_source == ReferenceMatch.CandidateSource.EMBEDDING
    assert row.band == REVIEW
    assert row.evidence_frames == 0


def test_a_later_pdq_match_promotes_the_row(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    """The promotion path: the same (original, page) row becomes a real match."""
    page = make_page("promoted")
    media = make_media(page)
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    embedding_candidates.refresh()
    assert ReferenceMatch.objects.get().candidate_source == "embedding"
    # Now the page turns out to carry the original's own frames after all.
    FrameHash.objects.filter(media=media, frame_index=0).update(pdq=PDQ[0])
    FrameHash.objects.filter(media=media, frame_index=1).update(pdq=PDQ[1])
    match_references([original], full=True)
    row = ReferenceMatch.objects.get()
    assert row.candidate_source == ReferenceMatch.CandidateSource.PDQ
    assert row.evidence_frames >= 1
    assert row.best_distance == 0


def test_only_sketches_of_the_same_recipe_are_compared(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("other-recipe"))
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    FrameHash.objects.filter(media=media).update(embedding_pipeline="something-else-v1")
    report = embedding_candidates.refresh()
    assert report.page_frames == 0
    assert report.proposed_matches == 0
    assert "page pixels are discarded" in report.empty_reason()


def test_an_inactive_page_is_never_a_candidate(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    page = make_page("inactive")
    media = make_media(page)
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    Page.objects.filter(pk=page.pk).update(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED
    )
    report = embedding_candidates.refresh()
    assert report.page_frames == 0
    assert not ReferenceMatch.objects.exists()


def test_the_report_names_the_missing_half(original: Reference) -> None:
    report = embedding_candidates.refresh()
    assert report.page_frames == 0
    assert "no page sketches" in report.empty_reason()
    Reference.objects.update(embedding_pipeline="")
    ReferenceFrameHash.objects.update(embedding=None)
    assert "no sketches at all" in embedding_candidates.refresh().empty_reason()


def test_a_dry_run_writes_nothing(
    original: Reference, make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("dry"))
    add_frames(media, {0: SKETCHES[0], 1: SKETCHES[1]})
    report = embedding_candidates.refresh(dry_run=True)
    assert report.candidates == 2
    assert not EmbeddingCandidate.objects.exists()
    assert not ReferenceMatch.objects.exists()
