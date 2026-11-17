"""Precision/recall of a PDQ distance threshold against human-labeled page pairs. Pure."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LabeledPair:
    same: bool
    min_distance: int | None  # None: no comparable frames, always predicted "different"
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class ThresholdResult:
    threshold: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def precision(self) -> float:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else 0.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 0.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0


def evaluate_thresholds(
    pairs: Iterable[LabeledPair], thresholds: Iterable[int] = range(65), min_score: float = 0.0
) -> list[ThresholdResult]:
    """A pair is predicted "same" when min_distance <= threshold and score >= min_score."""
    labeled = list(pairs)
    results = []
    for threshold in thresholds:
        tp = fp = fn = tn = 0
        for pair in labeled:
            predicted = (
                pair.min_distance is not None
                and pair.min_distance <= threshold
                and pair.score >= min_score
            )
            if predicted and pair.same:
                tp += 1
            elif predicted:
                fp += 1
            elif pair.same:
                fn += 1
            else:
                tn += 1
        results.append(ThresholdResult(threshold, tp, fp, fn, tn))
    return results


def suggest_threshold(
    results: Sequence[ThresholdResult], min_precision: float = 0.95
) -> ThresholdResult | None:
    """Highest recall among thresholds meeting `min_precision`; ties go to the stricter one."""
    eligible = [r for r in results if r.true_positives and r.precision >= min_precision]
    if not eligible:
        return None
    return min(eligible, key=lambda r: (-r.recall, r.threshold))
