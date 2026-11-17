"""The PDQ fixtures still describe what this server produces.

`matching/tests/fixtures/pdq/*.json` is the contract between the server's hasher and the
WebAssembly build the browser client uses (`frontend/wasm/pdq`, asserted by
`frontend/src/lib/pdq.test.ts`). If `pdqhash`, numpy or the packing in `matching.fingerprint`
ever changes what the server computes, this test fails here - on the side that owns the
reference hashes - instead of silently making browser uploads incomparable.

Regenerate deliberately (and re-run the frontend test) with:
    uv run python -m matching.tests.generate_pdq_fixtures
"""

import base64
import json
from pathlib import Path
from typing import Any

import numpy as np
import pdqhash
import pytest

from matching.pdq import DIHEDRAL_ORIENTATIONS, PDQ_HASH_BYTES

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pdq"
# Fixtures are inlined base64 RGB; keep them small enough to read in a diff.
MAX_FIXTURE_BYTES = 30_000


def fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def image_of(fixture: dict[str, Any]) -> np.ndarray:
    raw = base64.b64decode(fixture["rgb_base64"])
    height, width = int(fixture["height"]), int(fixture["width"])
    assert len(raw) == width * height * 3, f"{fixture['name']}: RGB length does not match size"
    return np.ascontiguousarray(np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3))


def test_fixtures_exist() -> None:
    paths = fixture_paths()
    assert len(paths) >= 8, "the parity suite needs a handful of shapes and sizes"
    for path in paths:
        assert path.stat().st_size <= MAX_FIXTURE_BYTES, f"{path.name} is too large for a fixture"


@pytest.mark.parametrize("path", fixture_paths(), ids=lambda path: path.stem)
def test_fixture_matches_current_pdqhash(path: Path) -> None:
    fixture = load(path)
    image = image_of(fixture)

    variants, quality = pdqhash.compute_dihedral(image)
    hashes = [np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes() for bits in variants]

    assert len(hashes) == DIHEDRAL_ORIENTATIONS
    assert [value.hex() for value in hashes] == fixture["hashes"], (
        f"{fixture['name']}: server hashes drifted from the stored fixture"
    )
    assert int(quality) == fixture["quality"], f"{fixture['name']}: quality drifted"


@pytest.mark.parametrize("path", fixture_paths(), ids=lambda path: path.stem)
def test_fixture_shape(path: Path) -> None:
    """Guard the format the browser test parses: 8 orientations, 64 hex characters each."""
    fixture = load(path)
    assert fixture["name"] == path.stem
    assert len(fixture["hashes"]) == DIHEDRAL_ORIENTATIONS
    for value in fixture["hashes"]:
        assert len(value) == PDQ_HASH_BYTES * 2
        assert bytes.fromhex(value).hex() == value, "hashes are lowercase hex"
    assert 0 <= int(fixture["quality"]) <= 100


def test_suite_covers_low_and_high_quality() -> None:
    """Parity has to hold for featureless frames too, not only detailed ones."""
    qualities = [int(load(path)["quality"]) for path in fixture_paths()]
    assert min(qualities) < 50, "expected at least one near-uniform, low-quality fixture"
    assert max(qualities) >= 50, "expected at least one detailed fixture above MIN_QUALITY"


def test_suite_covers_odd_and_even_sizes() -> None:
    sizes = {(int(load(path)["width"]), int(load(path)["height"])) for path in fixture_paths()}
    assert any(width % 2 == 1 or height % 2 == 1 for width, height in sizes), "need odd sizes"
    assert any(width != height for width, height in sizes), "need non-square sizes"
    assert (64, 64) in sizes, "need the 64x64 case the hasher downsamples to"
