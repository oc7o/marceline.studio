"""Pure, deterministic confidence scoring and band calibration for ReferenceMatch (ADR 0015).

`score_confidence` adds named, documented components and clamps to 0..1. Bands come from two
thresholds; `calibrate` derives them from human decisions on pairs the matcher actually scored,
and refuses — keeping whatever is in force and naming the guard — when the labels cannot support
a threshold (too few positives, too few negatives, precision unproven). No ORM here.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from matching.duration import matching_durations

# Conservative default band thresholds (used until calibration has enough labels).
DEFAULT_HIGH_THRESHOLD = 0.75
DEFAULT_REVIEW_THRESHOLD = 0.40

# Component weights. The maximum sum exceeds 1 on purpose: a match needs several signals, not
# every signal, to reach the top of the scale.
EVIDENCE_WEIGHT = 0.45  # times 1 - 0.5**evidence_frames (1 frame: half, 2: 3/4, 4: ~0.94)
DISTANCE_WEIGHT = 0.20  # times 1 - best_distance / loose_max_distance
TIMELINE_WEIGHT = 0.20  # times timeline_score
TIMELINE_NEUTRAL = 0.5  # timeline_score when frames have no order (poster only): not penalised
EXACT_DURATION_WEIGHT = 0.10  # page and reference duration within EXACT_DURATION_S
NEAR_DURATION_WEIGHT = 0.05  # within matching.duration tolerance (max(30 s, 3%))
EXACT_DURATION_S = 1.5
CORROBORATION_WEIGHT = 0.15  # active cluster / mirror group with another current strong match
COMMON_PENALTY = 0.15  # times common / (common + evidence) matching page frames
# Photo collections only (ADR 0020): times 1 - 0.5**(images - 1), so one matched image adds
# nothing, 2 add 0.10, 3 add 0.15, 4 add 0.175. It stands in for the two signals a collection
# structurally cannot earn - a verified timeline and a matching duration - and it is the same
# size as the timeline weight, so a collection is never scored more generously than a video.
COLLECTION_IMAGE_WEIGHT = 0.20
# A single evidence frame never reaches the high band, whatever the other signals say.
HIGH_MIN_EVIDENCE_FRAMES = 2
# A photo collection needs three independent (page frame, collection image) pairs instead: each
# image is its own picture, and no timeline chain corroborates any single one of them.
HIGH_MIN_COLLECTION_IMAGES = 3

# The fixed confidence of an embedding candidate (ADR 0037). It is **not** a score: the second
# signal has no calibrated relationship to being right, so there is nothing to compute. The number
# exists because `ReferenceMatch.confidence` is not nullable and the review queue orders by it,
# and it sits below every band threshold on purpose — such a row's band is set to `review`
# directly (`band_for(embedding_only=True)`), never derived from this value.
EMBEDDING_CANDIDATE_CONFIDENCE = 0.0

# The fixed confidence of a metadata candidate (ADR 0040), for exactly the same reasons: the third
# signal compares no pictures at all, so there is nothing about being right to compute. Kept as a
# separate name from the one above so that neither ADR's guarantee is expressed through the
# other's constant.
METADATA_CANDIDATE_CONFIDENCE = 0.0

# Calibration.
MIN_CALIBRATION_POSITIVES = 20
# The negative side of the same bar (issue #90). A precision target is a statement about the
# negatives a threshold lets through, so it can only be tested against negatives that were
# *scored*: with k of them the finest resolution of the admitted share is 1/k, and with zero
# observed above a threshold the 95% upper bound on that share is about 3/k (rule of three). Below
# 20 the test cannot even fail: with P positives above a threshold, precision only drops under 98%
# once more than P/49 negatives sit above it, so on 2026-09-18's shape (95 positives, one scored
# negative) every threshold on the grid "passed" and calibration chose high >= 0.00. 20 is the
# same bar the positive side has carried since ADR 0015, and it puts the resolution of the
# admitted share at 5 points per negative instead of 100.
MIN_CALIBRATION_NEGATIVES = 20
HIGH_TARGET_PRECISION = 0.98
REVIEW_MIN_RECALL = 0.98
# The high band is notice-eligible, so its threshold has a hard floor that no data may push it
# under. Below 0.50 a match's score is carried by the evidence term alone (0.45 at most) with
# nothing from distance, timeline, duration or corroboration — that is not notice-grade, whatever
# a precision estimate on a small labelled set says.
HIGH_THRESHOLD_FLOOR = 0.50
# One-sided 95% bound (z for 0.95). Precision at a threshold is estimated from a finite sample, so
# what has to clear HIGH_TARGET_PRECISION is the *lower* bound, not the point estimate: with zero
# false matches above the threshold that needs roughly 190 decided pairs above it.
PRECISION_CONFIDENCE_Z = 1.96
BUCKET_WIDTH = 0.1
THRESHOLD_STEP = 0.05

# Why a calibration run stored nothing. `""` means it stored.
GUARD_INSUFFICIENT_POSITIVES = "insufficient positives"
GUARD_INSUFFICIENT_NEGATIVES = "insufficient negatives"
GUARD_PRECISION_UNPROVEN = "precision unproven"
GUARD_HIGH_WOULD_FALL = "high would fall without new negatives"
GUARD_OPERATOR_OVERRIDE = "operator override"

HIGH = "high"
REVIEW = "review"
LOW = "low"


@dataclass(frozen=True)
class ConfidenceInputs:
    evidence_frames: int
    best_distance: int
    loose_max_distance: int
    timeline_score: float | None = None
    common_frames: int = 0
    page_duration_s: float | None = None
    reference_duration_s: float | None = None
    corroborated: bool = False
    # None for a video original; for a photo collection, `collection_images(...)`.
    collection_images: int | None = None


def collection_images(matched_positions: int, evidence_frames: int) -> int:
    """Independent (page frame, collection image) evidence pairs of a photo-collection match.

    `matched_positions` is how many distinct images of the collection the page's evidence frames
    hit, `evidence_frames` how many distinct page frames did the hitting. The smaller number is
    the evidence: one page picture matching three near-identical photos of the same shoot is one
    piece of evidence, and three page frames all showing the same photo are one too (ADR 0020).
    """
    return min(max(matched_positions, 0), max(evidence_frames, 0))


@dataclass(frozen=True)
class Confidence:
    value: float
    components: dict[str, float]


def score_confidence(inputs: ConfidenceInputs) -> Confidence:
    """Additive score clamped to 0..1; `components` explains every contribution.

    Shorter pages are never penalised (clips are leaks); near-equal duration only adds.
    """
    components: dict[str, float] = {}
    frames = max(inputs.evidence_frames, 0)
    components["evidence"] = EVIDENCE_WEIGHT * (1 - 0.5**frames)
    if frames:
        scale = max(inputs.loose_max_distance, 1)
        closeness = 1 - min(max(inputs.best_distance, 0), scale) / scale
        components["distance"] = DISTANCE_WEIGHT * closeness
    else:
        components["distance"] = 0.0
    timeline = TIMELINE_NEUTRAL if inputs.timeline_score is None else inputs.timeline_score
    components["timeline"] = TIMELINE_WEIGHT * min(max(timeline, 0.0), 1.0) if frames else 0.0
    components["duration"] = 0.0
    page, original = inputs.page_duration_s, inputs.reference_duration_s
    if frames and page is not None and original is not None:
        if abs(page - original) <= EXACT_DURATION_S:
            components["duration"] = EXACT_DURATION_WEIGHT
        elif matching_durations(page, original):
            components["duration"] = NEAR_DURATION_WEIGHT
    components["corroboration"] = CORROBORATION_WEIGHT if frames and inputs.corroborated else 0.0
    images = inputs.collection_images
    components["images"] = (
        COLLECTION_IMAGE_WEIGHT * (1 - 0.5 ** max(images - 1, 0))
        if frames and images is not None
        else 0.0
    )
    total = inputs.common_frames + frames
    components["common"] = -COMMON_PENALTY * inputs.common_frames / total if total else 0.0
    value = min(max(sum(components.values()), 0.0), 1.0)
    return Confidence(round(value, 4), {k: round(v, 4) for k, v in components.items()})


@dataclass(frozen=True)
class Thresholds:
    high: float = DEFAULT_HIGH_THRESHOLD
    review: float = DEFAULT_REVIEW_THRESHOLD


def band_for(
    confidence: float,
    evidence_frames: int,
    thresholds: Thresholds,
    *,
    collection_images: int | None = None,
    embedding_only: bool = False,
    metadata_only: bool = False,
) -> str:
    """Band of one match; `collection_images` is None for a video original.

    The floor below the high band is what the evidence has to be made of, not how good it looks:
    two evidence frames for a video, three independent image pairs for a photo collection.

    `embedding_only` is a row the second signal proposed and nothing has confirmed (ADR 0037). It
    is always `review`, never `high` and never `low`: an embedding has no safe fixed threshold, so
    it may not be scored *up* into evidence, and it may not be hidden in the band the review queue
    skips either — being looked at is the entire point of it.

    `metadata_only` is the third signal's candidate (ADR 0040) and lands in exactly the same
    place, for a blunter reason: metadata compared no pictures, so it cannot be evidence at any
    threshold. Both flags are honoured before anything else is looked at, so no combination of
    confidence and frame counts can route such a row past them.
    """
    if embedding_only or metadata_only:
        return REVIEW
    floor = (
        evidence_frames >= HIGH_MIN_EVIDENCE_FRAMES
        if collection_images is None
        else collection_images >= HIGH_MIN_COLLECTION_IMAGES
    )
    if confidence >= thresholds.high and floor:
        return HIGH
    if confidence >= thresholds.review:
        return REVIEW
    return LOW


def wilson_lower_bound(successes: int, total: int, z: float = PRECISION_CONFIDENCE_Z) -> float:
    """One-sided lower confidence bound on a proportion (Wilson score interval).

    Used on precision: `successes` true matches out of `total` decided pairs above a threshold.
    It is well behaved at `successes == total`, where the point estimate is 1.0 and says nothing
    about how large the sample was.
    """
    if total <= 0:
        return 0.0
    observed = successes / total
    denominator = 1 + z * z / total
    centre = (observed + z * z / (2 * total)) / denominator
    spread = (
        z / denominator * math.sqrt(observed * (1 - observed) / total + z * z / (4 * total * total))
    )
    return max(centre - spread, 0.0)


@dataclass(frozen=True)
class Calibration:
    """Outcome of one calibration.

    `thresholds` is what should be in force afterwards — the derived pair when `calibrated`, and
    the pair that was already in force when a guard fired. `chosen` is what the data proposed,
    kept even when a guard refused it so the caller can print before → after.
    """

    thresholds: Thresholds
    calibrated: bool
    positives: int
    negatives: int
    lines: list[str]
    guard: str = ""
    chosen: Thresholds | None = None


def calibrate(
    samples: Sequence[tuple[float, bool]], *, current: Thresholds | None = None
) -> Calibration:
    """Band thresholds from (confidence, is_leak) human decisions on *scored* pairs.

    Every sample is a pair the matcher scored: a human decision on a pair with no stored match has
    no confidence to place on the curve and is not a sample (see `matching.calibration`).

    High: the lowest threshold on a `THRESHOLD_STEP` grid, never below the floor, whose decided
    pairs at or above it carry a 95% *lower bound* on precision of at least
    `HIGH_TARGET_PRECISION`. Review: the highest threshold that still keeps `REVIEW_MIN_RECALL` of
    positives out of the hidden low band, capped at high.

    Nothing is derived — `current` is kept and a guard is named — with fewer than
    `MIN_CALIBRATION_POSITIVES` positives, fewer than `MIN_CALIBRATION_NEGATIVES` negatives, or no
    threshold whose precision bound clears the target. Refusing is the honest answer: an emitted
    threshold that the labels cannot support is worse than no threshold, because it silently
    re-bands every stored match (issue #90).
    """
    in_force = current if current is not None else Thresholds()
    positives = sum(label for _, label in samples)
    negatives = len(samples) - positives
    lines = [f"Labeled pairs: {len(samples)} ({positives} leaks, {negatives} false matches)"]
    buckets = math.ceil(1 / BUCKET_WIDTH)
    for bucket in range(buckets):
        low, high = bucket * BUCKET_WIDTH, (bucket + 1) * BUCKET_WIDTH
        inside = [
            label
            for value, label in samples
            if low <= value < high or (bucket == buckets - 1 and value >= high)
        ]
        precision = f"{100 * sum(inside) / len(inside):.1f}%" if inside else "n/a"
        lines.append(
            f"  confidence {low:.1f}-{high:.1f}: {len(inside)} labeled, {sum(inside)} leaks, "
            f"precision {precision}"
        )

    def kept(guard: str, why: str) -> Calibration:
        lines.append(f"Guard {guard}: {why}")
        lines.append(
            f"  keeping the thresholds in force: high>={in_force.high:.2f}, "
            f"review>={in_force.review:.2f}"
        )
        return Calibration(in_force, False, positives, negatives, lines, guard)

    if positives < MIN_CALIBRATION_POSITIVES:
        return kept(
            GUARD_INSUFFICIENT_POSITIVES,
            f"{positives} scored leak(s) < {MIN_CALIBRATION_POSITIVES} required",
        )
    if negatives < MIN_CALIBRATION_NEGATIVES:
        return kept(
            GUARD_INSUFFICIENT_NEGATIVES,
            f"{negatives} scored false match(es) < {MIN_CALIBRATION_NEGATIVES} required; a "
            "precision target cannot be tested without negatives that were scored",
        )
    floor = max(HIGH_THRESHOLD_FLOOR, in_force.review)
    grid = [round(i * THRESHOLD_STEP, 2) for i in range(round(1 / THRESHOLD_STEP) + 1)]
    lines.append(
        f"  high threshold grid starts at {floor:.2f} (floor {HIGH_THRESHOLD_FLOOR:.2f}, review "
        f"in force {in_force.review:.2f})"
    )
    proven: tuple[float, int, int, float] | None = None
    for step in grid:
        if step < floor:
            continue
        above = [label for value, label in samples if value >= step]
        leaks = sum(above)
        bound = wilson_lower_bound(leaks, len(above))
        if leaks and bound >= HIGH_TARGET_PRECISION:
            proven = (step, leaks, len(above), bound)
            break
    if proven is None:
        return kept(
            GUARD_PRECISION_UNPROVEN,
            f"no threshold at or above {floor:.2f} reaches a 95% lower bound of "
            f"{HIGH_TARGET_PRECISION:.0%} precision on the decided pairs above it",
        )
    high_threshold, leaks_above, decided_above, bound = proven
    review_threshold = 0.0
    for step in grid:
        recalled = sum(label for value, label in samples if value >= step)
        if recalled / positives >= REVIEW_MIN_RECALL:
            review_threshold = step
    chosen = Thresholds(high_threshold, min(review_threshold, high_threshold))
    lines.append(
        f"chosen thresholds: high>={chosen.high:.2f} (precision {leaks_above}/{decided_above} "
        f"above it, 95% lower bound {bound:.3f} >= {HIGH_TARGET_PRECISION:.0%}), "
        f"review>={chosen.review:.2f} (recall target {REVIEW_MIN_RECALL:.0%})"
    )
    return Calibration(chosen, True, positives, negatives, lines, "", chosen)
