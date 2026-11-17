"""The browser parity harness still reads the thresholds the matcher really uses.

`frontend/parity/parity.py` reports how many browser-vs-CLI rows fall past the strict and the
loose threshold, and `--fail-over` turns that into an exit code. It cannot import
`matching.references` - it is a standalone uv script without Django - so it reads the two
constants out of that module's source. Typing them in instead is what went wrong before:
`STRICT_MAX_DISTANCE` was raised from 31 to 36 by the ADR 0015 amendment and the harness kept
measuring against 31 for weeks, on demand and therefore unnoticed.

This test fails the moment that reading stops working - a renamed constant, a type annotation,
a value moved behind a function - rather than the next time someone runs the harness.
"""

import re
from pathlib import Path

import pytest

from matching.references import LOOSE_MAX_DISTANCE, STRICT_MAX_DISTANCE

HARNESS = Path(__file__).resolve().parents[3] / "frontend" / "parity" / "parity.py"


def read_threshold(name: str) -> int:
    """The harness's own regex, applied to the module it applies it to."""
    source = (Path(__file__).resolve().parents[2] / "matching" / "references.py").read_text(
        encoding="utf-8"
    )
    match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", source, re.MULTILINE)
    assert match is not None, f"{name} is no longer a plain assignment the harness can read"
    return int(match.group(1))


def test_the_harness_still_finds_both_thresholds() -> None:
    assert read_threshold("STRICT_MAX_DISTANCE") == STRICT_MAX_DISTANCE
    assert read_threshold("LOOSE_MAX_DISTANCE") == LOOSE_MAX_DISTANCE


def test_the_harness_has_no_threshold_of_its_own() -> None:
    if not HARNESS.exists():  # pragma: no cover - the harness is part of the repository
        pytest.skip("frontend/parity/parity.py is not in this checkout")
    source = HARNESS.read_text(encoding="utf-8")
    assert "MATCH_THRESHOLD, LOOSE_THRESHOLD = _thresholds()" in source, (
        "the harness must derive its thresholds from matching/references.py, never restate them"
    )
