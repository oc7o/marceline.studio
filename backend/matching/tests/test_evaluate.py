import pytest

from matching.evaluate import LabeledPair, ThresholdResult, evaluate_thresholds, suggest_threshold

PAIRS = [
    LabeledPair(same=True, min_distance=5, score=1.0),
    LabeledPair(same=True, min_distance=20, score=0.4),
    LabeledPair(same=True, min_distance=None),
    LabeledPair(same=False, min_distance=10, score=0.2),
    LabeledPair(same=False, min_distance=40, score=1.0),
]


def counts(result: ThresholdResult) -> tuple[int, int, int, int]:
    return (
        result.true_positives,
        result.false_positives,
        result.false_negatives,
        result.true_negatives,
    )


def test_confusion_counts_and_metrics() -> None:
    by_threshold = {r.threshold: r for r in evaluate_thresholds(PAIRS, [4, 10, 20, 40])}
    assert counts(by_threshold[4]) == (0, 0, 3, 2)
    assert (by_threshold[4].precision, by_threshold[4].recall, by_threshold[4].f1) == (0, 0, 0)
    assert counts(by_threshold[10]) == (1, 1, 2, 1)
    assert by_threshold[10].precision == 0.5
    assert by_threshold[10].recall == pytest.approx(1 / 3)
    assert counts(by_threshold[20]) == (2, 1, 1, 1)
    assert by_threshold[20].f1 == pytest.approx(2 / 3)
    assert counts(by_threshold[40]) == (2, 2, 1, 0)


def test_min_score_filters_predictions() -> None:
    [result] = evaluate_thresholds(PAIRS, [40], min_score=0.5)
    assert counts(result) == (1, 1, 2, 1)


def test_suggest_threshold_prefers_recall_then_strictness() -> None:
    results = evaluate_thresholds(PAIRS)
    strict = suggest_threshold(results, min_precision=0.95)
    assert strict is not None
    assert (strict.threshold, strict.precision) == (5, 1.0)
    relaxed = suggest_threshold(results, min_precision=0.6)
    assert relaxed is not None
    assert relaxed.threshold == 20


def test_suggest_threshold_none_when_unreachable() -> None:
    pairs = [LabeledPair(same=False, min_distance=0), LabeledPair(same=True, min_distance=50)]
    assert suggest_threshold(evaluate_thresholds(pairs), min_precision=0.9) is None
