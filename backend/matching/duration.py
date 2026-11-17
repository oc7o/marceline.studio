"""Duration indicators shared by evidence collection and human review."""

import math

DURATION_TOLERANCE_SECONDS = 30
DURATION_TOLERANCE_RATIO = 0.03


def page_duration(raw: object) -> float | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("duration_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def duration_tolerance(longer: float) -> float:
    return max(DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE_RATIO * longer)


def matching_durations(first: float | None, second: float | None) -> bool:
    return (
        first is not None
        and second is not None
        and abs(first - second) <= duration_tolerance(max(first, second))
    )
