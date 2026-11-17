"""Candidates from the second signal — and the wall that keeps them candidates (ADR 0037).

The embedding's job is ranking, never deciding. Measured over 805,683 real negative pairs, its
nearest unrelated pair scores *above* the median of a genuine same-picture pair, so there is no
threshold at which "the sketches are close" means anything on its own. What it does have is
recall: the right moment is in the top ten essentially always, including on the asymmetric
re-frame where PDQ's best hit is simply wrong.

So this module does exactly two things:

1. `refresh_candidates` writes `EmbeddingCandidate` rows — a page frame's nearest reference
   moments, and whether the page frame is among that moment's nearest neighbours too (`mutual`).
2. `refresh_matches` turns the mutual ones into `ReferenceMatch` rows with
   `candidate_source=embedding`: **band `review`, zero evidence frames, zero matched frames**, so
   they enter the human review queue and nothing else. They are not evidence anywhere:
   `matching.references` never reads this table, the timeline never sees them, they earn no
   confidence component, they can never be in the high band, and therefore they can never reach
   the notice gate (which needs `high` plus a human confirmation).

Promotion happens in one of the two ways the research demands, and both are existing paths: a
later PDQ match takes the same `(reference, page)` row over and sets `candidate_source=pdq`
(`matching.references.match_references`), or a human confirms it in review like any other
proposal. Nothing here ever confirms anything.
"""

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Count, Q

from index.models import FrameHash
from matching.confidence import EMBEDDING_CANDIDATE_CONFIDENCE, REVIEW
from matching.embedding import EMBEDDING_PIPELINE
from matching.models import EmbeddingCandidate, Reference, ReferenceFrameHash, ReferenceMatch
from matching.pdq import MIN_QUALITY, hamming_distance
from matching.sketch_index import SketchIndex

# Neighbours per side. Ten is what the research measured recall@10 on; more only buys review work.
DEFAULT_TOP_K = 10
# Mutual candidate frames a page needs before the signal proposes the original for review. Two,
# for the same reason a high-band PDQ match needs two evidence frames: one picture that happens to
# rank near one moment is what this signal produces by the thousand.
MIN_CANDIDATE_FRAMES = 2
_INSERT_BATCH = 2000


@dataclass(frozen=True)
class CandidateReport:
    """What one refresh did, in the terms the pilot's exit criterion is written in."""

    pipeline: str
    reference_moments: int
    page_frames: int
    candidates: int
    mutual: int
    proposed_matches: int
    removed_matches: int
    seconds: float

    def line(self) -> str:
        return (
            f"Embedding candidates ({self.pipeline}): {self.page_frames} page frames x "
            f"{self.reference_moments} reference moments -> {self.candidates} candidate pairs, "
            f"{self.mutual} mutual; {self.proposed_matches} review-band candidate matches "
            f"({self.removed_matches} withdrawn); {self.seconds:.1f} s"
        )

    def empty_reason(self) -> str:
        """Why nothing happened, in words that name the missing half."""
        if not self.reference_moments and not self.page_frames:
            return (
                "no sketches at all: re-fingerprint the originals with "
                "`client/fingerprint.py --embeddings` and fetch page media with embeddings on"
            )
        if not self.reference_moments:
            return (
                "no reference sketches: re-fingerprint the originals with "
                "`client/fingerprint.py --embeddings` and import them"
            )
        if not self.page_frames:
            return (
                "no page sketches: page pixels are discarded after hashing, so only media "
                "fetched with MATCHING_EMBEDDINGS_ENABLED on has one (ADR 0037 is not retroactive)"
            )
        return ""


def reference_sketches(pipeline: str) -> list[tuple[int, bytes]]:
    """(reference frame id, sketch) of every moment on this recipe. Unflipped rows only."""
    return [
        (pk, bytes(sketch))  # type: ignore[arg-type]
        for pk, sketch in ReferenceFrameHash.objects.filter(
            embedding__isnull=False,
            dihedral=0,
            reference__embedding_pipeline=pipeline,
            quality__gte=MIN_QUALITY,
        )
        .order_by("pk")
        .values_list("pk", "embedding")
    ]


def page_sketches(pipeline: str, min_quality: int = MIN_QUALITY) -> list[tuple[int, bytes]]:
    """(frame id, sketch) of every active page's embedded frames on this recipe.

    Active pages only, like every other page-side loader in matching (issue #71).
    """
    return [
        (pk, bytes(sketch))  # type: ignore[arg-type]
        for pk, sketch in FrameHash.objects.filter(
            embedding__isnull=False,
            embedding_pipeline=pipeline,
            quality__gte=min_quality,
            media__page__active=True,
        )
        .order_by("pk")
        .values_list("pk", "embedding")
    ]


@dataclass(frozen=True)
class Neighbour:
    """One page frame's nearest moment of one original."""

    frame_id: int
    reference_id: int
    reference_frame_id: int
    distance: int
    rank: int
    mutual: bool


def rank_neighbours(
    page: Sequence[tuple[int, bytes]],
    reference: Sequence[tuple[int, bytes]],
    moment_reference: dict[int, int],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[Neighbour]:
    """Top-`top_k` reference moments per page frame, collapsed to the best one per original.

    `mutual` is set when that page frame is also among the moment's own `top_k` nearest page
    frames — the pilot's criterion, and a much stronger statement than a one-sided neighbour.
    Pure apart from the index itself: no database access, so a test can drive it with bytes.
    """
    if not page or not reference:
        return []
    forward = SketchIndex([pk for pk, _ in reference], [value for _, value in reference])
    backward = SketchIndex([pk for pk, _ in page], [value for _, value in page])
    page_ids = [pk for pk, _ in page]
    moment_ids, _ = backward.nearest([value for _, value in reference], top_k)
    back_pairs = {
        (int(frame_id), moment)
        for moment, row in zip([pk for pk, _ in reference], moment_ids.tolist(), strict=True)
        for frame_id in row
    }
    found: dict[tuple[int, int], Neighbour] = {}
    ids, distances = forward.nearest([value for _, value in page], top_k)
    for position, frame_id in enumerate(page_ids):
        for rank, (moment, distance) in enumerate(
            zip(ids[position].tolist(), distances[position].tolist(), strict=True), start=1
        ):
            reference_id = moment_reference[int(moment)]
            key = (frame_id, reference_id)
            current = found.get(key)
            if current is not None and current.distance <= int(distance):
                continue
            found[key] = Neighbour(
                frame_id=frame_id,
                reference_id=reference_id,
                reference_frame_id=int(moment),
                distance=int(distance),
                rank=rank,
                mutual=(frame_id, int(moment)) in back_pairs,
            )
    return sorted(found.values(), key=lambda n: (n.frame_id, n.reference_id))


def _store(neighbours: Iterable[Neighbour], pipeline: str) -> int:
    rows = [
        EmbeddingCandidate(
            frame_id=item.frame_id,
            reference_id=item.reference_id,
            reference_frame_id=item.reference_frame_id,
            distance=item.distance,
            rank=item.rank,
            mutual=item.mutual,
            pipeline=pipeline,
        )
        for item in neighbours
    ]
    EmbeddingCandidate.objects.bulk_create(rows, batch_size=_INSERT_BATCH)
    return len(rows)


def pdq_distance(frame_id: int, reference_frame_id: int) -> int:
    """Exact PDQ distance of a candidate pair, for the record.

    Stored on the proposed match as `best_distance` so a reviewer can see what PDQ made of the
    same pair — normally far beyond the threshold, which is the entire reason the row exists. It
    is never compared against a threshold here.
    """
    page = FrameHash.objects.values_list("pdq", flat=True).get(pk=frame_id)
    moment = ReferenceFrameHash.objects.values_list("pdq", flat=True).get(pk=reference_frame_id)
    return hamming_distance(bytes(page), bytes(moment))


@transaction.atomic
def refresh_matches(pipeline: str) -> tuple[int, int]:
    """(proposed, withdrawn) review-band candidate matches from the mutual candidates.

    A pair the PDQ matcher already knows is left completely alone: its row is a real match, and
    this signal has nothing to add to it. A candidate row whose support disappeared is withdrawn
    only while nobody has decided it — a human decision, confirmed or rejected, is never touched.
    """
    rows = list(
        EmbeddingCandidate.objects.filter(pipeline=pipeline, mutual=True).order_by("distance")
    )
    pages = dict(
        FrameHash.objects.filter(pk__in={row.frame_id for row in rows}).values_list(
            "pk", "media__page_id"
        )
    )
    timestamps = dict(
        ReferenceFrameHash.objects.filter(
            pk__in={row.reference_frame_id for row in rows}
        ).values_list("pk", "timestamp_s")
    )
    support: dict[tuple[int, int], list[EmbeddingCandidate]] = {}
    for candidate in rows:
        page = pages.get(candidate.frame_id)
        if page is None:  # the frame vanished between the two queries
            continue
        support.setdefault((candidate.reference_id, int(page)), []).append(candidate)
    owned_by_pdq = {
        (reference_id, page_id)
        for reference_id, page_id in ReferenceMatch.objects.exclude(
            candidate_source=ReferenceMatch.CandidateSource.EMBEDDING
        ).values_list("reference_id", "page_id")
    }
    frame_counts = dict(
        FrameHash.objects.filter(media__page_id__in={page for _, page in support})
        .values_list("media__page_id")
        .annotate(total=Count("pk"))
    )
    proposed = 0
    wanted: set[tuple[int, int]] = set()
    for (reference_id, page_id), items in support.items():
        if len({item.frame_id for item in items}) < MIN_CANDIDATE_FRAMES:
            continue
        if (reference_id, page_id) in owned_by_pdq:
            # PDQ owns this row; the second signal is not consulted about a real match.
            continue
        wanted.add((reference_id, page_id))
        best = items[0]
        ReferenceMatch.objects.update_or_create(
            reference_id=reference_id,
            page_id=page_id,
            defaults={
                "candidate_source": ReferenceMatch.CandidateSource.EMBEDDING,
                # Evidence is zero by construction, and the database refuses anything else for
                # an embedding row. `best_distance` is the PDQ distance of the pair, recorded so
                # a reviewer sees how far out it is.
                "best_distance": min(pdq_distance(best.frame_id, best.reference_frame_id), 256),
                "best_reference_timestamp_s": timestamps[best.reference_frame_id],
                "matched_frames": 0,
                "evidence_frames": 0,
                "loose_frames": 0,
                "common_frames": 0,
                "matched_seconds": [],
                "shared_seconds": [],
                "common_seconds": [],
                "timeline_score": None,
                "consistent_frames": 0,
                "timeline_reason": "embedding candidate: no timeline, no evidence (ADR 0037)",
                "confidence": EMBEDDING_CANDIDATE_CONFIDENCE,
                "band": REVIEW,
                "confidence_signals": {
                    "source": "embedding",
                    "sketch_distance": best.distance,
                    "candidate_frames": len({item.frame_id for item in items}),
                    "pipeline": pipeline,
                },
                "strength": ReferenceMatch.Strength.WEAK,
                "page_frames": frame_counts.get(page_id, 0),
                "cluster_id": None,
            },
        )
        proposed += 1
    stale = ReferenceMatch.objects.filter(
        candidate_source=ReferenceMatch.CandidateSource.EMBEDDING, status="proposed"
    ).filter(Q(decided_by="") | Q(decided_by="auto"))
    removed = 0
    for pk, reference_id, page_id in stale.values_list("pk", "reference_id", "page_id"):
        if (reference_id, page_id) not in wanted:
            ReferenceMatch.objects.filter(pk=pk).delete()
            removed += 1
    return proposed, removed


def refresh(
    *, top_k: int = DEFAULT_TOP_K, pipeline: str = EMBEDDING_PIPELINE, dry_run: bool = False
) -> CandidateReport:
    """Recompute the second signal's candidates and its review-band proposals.

    Idempotent and cheap to repeat: the whole candidate table for this recipe is rebuilt, because
    a sketch never changes once stored and the set of embedded frames only grows.
    """
    clock = time.perf_counter()
    reference = reference_sketches(pipeline)
    page = page_sketches(pipeline)
    moment_reference = dict(
        ReferenceFrameHash.objects.filter(pk__in=[pk for pk, _ in reference]).values_list(
            "pk", "reference_id"
        )
    )
    neighbours = rank_neighbours(page, reference, moment_reference, top_k=top_k)
    mutual = sum(1 for item in neighbours if item.mutual)
    if dry_run:
        return CandidateReport(
            pipeline=pipeline,
            reference_moments=len(reference),
            page_frames=len(page),
            candidates=len(neighbours),
            mutual=mutual,
            proposed_matches=0,
            removed_matches=0,
            seconds=time.perf_counter() - clock,
        )
    with transaction.atomic():
        EmbeddingCandidate.objects.filter(pipeline=pipeline).delete()
        stored = _store(neighbours, pipeline)
    proposed, removed = refresh_matches(pipeline)
    return CandidateReport(
        pipeline=pipeline,
        reference_moments=len(reference),
        page_frames=len(page),
        candidates=stored,
        mutual=mutual,
        proposed_matches=proposed,
        removed_matches=removed,
        seconds=time.perf_counter() - clock,
    )


def references_on(pipeline: str) -> int:
    """How many originals carry sketches of this recipe (for reports)."""
    return Reference.objects.filter(embedding_pipeline=pipeline).count()
