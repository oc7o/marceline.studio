"""Deriving a dihedral PDQ orientation from the stored hash alone (issue #8).

The pixels of an indexed page frame are discarded after hashing (Hard rules), so any answer to
"is this page a mirrored re-upload" that is retroactive has to work from the 256-bit hash. It can,
approximately: each dihedral transform of the picture permutes the 16x16 DCT matrix and negates
its odd-frequency coefficients, so it is a transpose plus an XOR mask on the hash bits — exact
where nothing is negated, and drifting by the shift it puts on the median everywhere else.

Ground truth here is `pdqhash.compute_dihedral` on real pixels, on the committed ThreatExchange
fixtures (whose own correctness `test_pdq_threatexchange` pins against Meta's published hashes)
plus generated images. Nothing is downloaded and no media is read.

Measured 2026-09-18 over 159 frames (ffmpeg synthetic sources + these fixtures), full method and
numbers in `docs/matching/page-side-flips.md`:

| orientation          | median | p90 | max |
|----------------------|--------|-----|-----|
| orig, flipp          | 0      | 0   | 0   |
| the other six        | 8      | 18  | 34  |
"""

from pathlib import Path

import numpy as np
import pdqhash
import pytest
from PIL import Image

from matching.pdq import (
    DIHEDRAL_DERIVATION_MAX,
    DIHEDRAL_ORIENTATIONS,
    EXACT_DERIVED_ORIENTATIONS,
    MIRROR_ORIENTATION,
    ORIENTATION_NAMES,
    dihedral_from_hash,
    dihedral_hashes_from_hash,
    hamming_distance,
    hash_image,
    open_image,
)
from matching.tests.images import synthetic_image

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pdq-threatexchange"
# Headroom over the measured maximum of 34: this is a property of PDQ, not of our code, so a
# future Pillow or libjpeg may move it a little. A broken recipe lands ~128 bits away.
DERIVATION_TOLERANCE = 44
# A wrong transpose or mask makes an orientation indistinguishable from an unrelated picture.
MIN_ORIENTATION_MARGIN = 48


def _fixture_images() -> list[Image.Image]:
    return [
        open_image(path.read_bytes())
        for path in sorted(FIXTURE_DIR.iterdir())
        if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]


def _true_dihedral(image: Image.Image) -> list[bytes]:
    pixels = np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))
    variants, _ = pdqhash.compute_dihedral(pixels)
    return [np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes() for bits in variants]


def test_orientation_names_match_the_group_size() -> None:
    assert len(ORIENTATION_NAMES) == DIHEDRAL_ORIENTATIONS
    assert ORIENTATION_NAMES[MIRROR_ORIENTATION] == "flipy"


def test_rejects_a_bad_orientation() -> None:
    hash_ = hash_image(synthetic_image(1)).hash
    with pytest.raises(ValueError):
        dihedral_from_hash(hash_, DIHEDRAL_ORIENTATIONS)
    with pytest.raises(ValueError):
        dihedral_from_hash(hash_, -1)


def test_permutation_only_orientations_are_exact() -> None:
    """`orig` and `flipp` negate no coefficient, so the median cannot move: 0 bits, always."""
    for image in _fixture_images():
        truth = _true_dihedral(image)
        for orientation in sorted(EXACT_DERIVED_ORIENTATIONS):
            derived = dihedral_from_hash(truth[0], orientation)
            assert derived == truth[orientation], (
                f"{ORIENTATION_NAMES[orientation]} must be derivable exactly"
            )


def test_derivation_is_an_involution_for_the_mirror() -> None:
    """Mirroring twice is the identity, on the hash as on the picture."""
    for image in _fixture_images():
        hash_ = hash_image(image).hash
        mirrored = dihedral_from_hash(hash_, MIRROR_ORIENTATION)
        assert dihedral_from_hash(mirrored, MIRROR_ORIENTATION) == hash_


@pytest.mark.parametrize("orientation", range(DIHEDRAL_ORIENTATIONS))
def test_derived_orientation_is_close_to_the_pixel_hash(orientation: int) -> None:
    """Every orientation, against `compute_dihedral` on the same pixels."""
    for image in _fixture_images():
        truth = _true_dihedral(image)
        distance = hamming_distance(dihedral_from_hash(truth[0], orientation), truth[orientation])
        assert distance <= DERIVATION_TOLERANCE, (
            f"{ORIENTATION_NAMES[orientation]}: {distance} bits from the pixel hash "
            f"(allowed {DERIVATION_TOLERANCE}; measured at most {DIHEDRAL_DERIVATION_MAX})"
        )


def test_each_derived_orientation_is_nearest_to_its_own_transform() -> None:
    """The recipe table is pinned to the real geometry, not merely to itself.

    Derived variant k must be closer to the pixel hash of transform k than any other derived
    variant is — the check that would fail if the transpose flags or the masks were shuffled.
    """
    for image in _fixture_images():
        truth = _true_dihedral(image)
        derived = dihedral_hashes_from_hash(truth[0])
        for orientation in range(DIHEDRAL_ORIENTATIONS):
            distances = [hamming_distance(value, truth[orientation]) for value in derived]
            nearest = min(range(DIHEDRAL_ORIENTATIONS), key=lambda i: distances[i])
            runner_up = sorted(distances)[1]
            assert nearest == orientation, (
                f"{ORIENTATION_NAMES[orientation]}: derived "
                f"{ORIENTATION_NAMES[nearest]} is nearer ({distances[nearest]} vs "
                f"{distances[orientation]} bits) - the recipe table is wrong"
            )
            assert runner_up >= MIN_ORIENTATION_MARGIN, (
                f"{ORIENTATION_NAMES[orientation]}: the next-best derived variant is only "
                f"{runner_up} bits away, so this test no longer distinguishes orientations"
            )


def test_a_mirrored_picture_is_found_by_the_derived_mirror_and_not_by_the_stored_hash() -> None:
    """The whole point: today's stored hash cannot see a mirror; the derived one can."""
    for image in (synthetic_image(1), synthetic_image(7)):
        stored = hash_image(image).hash
        mirrored = hash_image(image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)).hash
        assert hamming_distance(stored, mirrored) > MIN_ORIENTATION_MARGIN
        derived = dihedral_from_hash(stored, MIRROR_ORIENTATION)
        assert hamming_distance(derived, mirrored) <= DERIVATION_TOLERANCE


def test_derivation_does_not_invent_neighbours() -> None:
    """Mirroring a hash must not drag it towards unrelated pictures."""
    images = [synthetic_image(1), synthetic_image(11), synthetic_image(12), *_fixture_images()]
    hashes = [hash_image(image).hash for image in images]
    for i, value in enumerate(hashes):
        mirrored = dihedral_from_hash(value, MIRROR_ORIENTATION)
        for j, other in enumerate(hashes):
            if i != j:
                assert hamming_distance(mirrored, other) > MIN_ORIENTATION_MARGIN
