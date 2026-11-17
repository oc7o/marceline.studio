"""Our PDQ hashes against Meta's published ThreatExchange reference vectors (issue #6).

Every other PDQ test in this repo compares us with us: `test_pdq_parity` pins today's output
against stored fixtures, and the browser build is compared against the same fixtures. That proves
stability, not correctness. This module compares our hashes with the hashes **Meta publishes for
these exact files**, so a wrong bit order, a wrong hex convention or a shuffled dihedral order
fails here. Fixtures and expected hashes live in `fixtures/pdq-threatexchange/` (see its README for
provenance and licence); nothing is downloaded at test time.

What the reference data proves
------------------------------
* `pillow_pdqhash` — Meta's own Python vectors (`python-threatexchange`), produced by Pillow plus
  the `pdqhash` package, i.e. exactly the stack `matching.pdq.hash_image` uses. Measured distance
  **0** for all four images (RGB JPEG, LA PNG, 16-bit grey PNG, RGBA PNG), qualities identical, so
  these are asserted as exact equality of the 64-character hex string. This is the strongest
  statement available: our hex hash *is* the ThreatExchange hex hash.
* `photo_hasher` / `dihedral` — Meta's C++ `pdq-photo-hasher` on `bridge-1-original.jpg`, both the
  plain hash and all eight `--pdqdih` orientations. Measured distances 4-8 bits (see table below).
* `transformed_files` — the published hashes of the eight separately rotated/flipped JPEGs of the
  same photo. For each orientation *k*, our `compute_dihedral` variant *k* is the nearest of our
  eight variants to the reference hash of the file transformed by *k*, and the runner-up is
  104-108 bits away. That pins the variant order to the real geometric transform, not merely to
  upstream's dihedral routine.

What it does not prove
----------------------
* Not that our decode equals Meta's C++ decode: it does not, by 4-8 bits on JPEG input.
* Nothing about video frame extraction (`matching.frames` ffmpeg scaling), the quality gate, or
  our matching thresholds. Only single-image hashing is covered.
* No EXIF-rotation behaviour: upstream's `exif-rotn` images have no published hashes.
* Only one photo goes through the C++ hasher, so the JPEG-decoder drift below is measured on one
  image, not a distribution.

Tolerances (measured 2026-09-16; Pillow 12.3.0, numpy 2.5.3, pdqhash pinned in `backend/uv.lock`)
-------------------------------------------------------------------------------------------------
| comparison                                   | measured                | tolerance used |
|----------------------------------------------|-------------------------|----------------|
| Pillow+pdqhash vectors (4 images)             | 0, 0, 0, 0              | 0 (exact)      |
| C++ hasher, same image                        | 6                       | 12             |
| C++ `--pdqdih`, 8 orientations                | 6, 8, 4, 4, 4, 8, 6, 4  | 12             |
| our variant vs separately transformed file    | 6, 6, 14, 10, 10, 6, 6, 14 | 20          |

The tolerances are the measured maximum plus a little headroom for a future libjpeg-turbo or
Pillow version, and they are an order of magnitude below the ~128 bits a bit-order or ordering bug
produces (unrelated orientations of this photo are 104-120 bits apart). They are deliberately not
"below MATCH_DISTANCE": that would be loose enough to hide a real regression.
"""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pdqhash
import pytest
from PIL import Image

from matching.pdq import (
    DIHEDRAL_ORIENTATIONS,
    from_hex,
    hamming_distance,
    hash_image,
    open_image,
    to_hex,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pdq-threatexchange"
VECTORS: dict[str, Any] = json.loads((FIXTURE_DIR / "vectors.json").read_text())

# Meta's Python vectors come from our own stack, so anything but an exact match is a bug.
EXACT = 0
# JPEG decoder drift between Meta's CImg/libjpeg + float32 luma and Pillow + pdqhash's numpy luma.
DECODER_TOLERANCE = 12
# Same drift plus a second, independent JPEG encode of the rotated image.
TRANSFORM_TOLERANCE = 20
# A wrong orientation mapping lands ~128 bits away; keep a wide, unambiguous margin.
MIN_ORIENTATION_MARGIN = 64
# The committed images are Meta's sample data, not media: keep the folder reviewable.
MAX_FIXTURE_BYTES = 1_000_000

PILLOW_VECTORS: list[dict[str, Any]] = VECTORS["pillow_pdqhash"]["hashes"]
DIHEDRAL_VECTORS: list[dict[str, Any]] = VECTORS["dihedral"]["hashes"]
TRANSFORMED_VECTORS: list[dict[str, Any]] = VECTORS["transformed_files"]["hashes"]


def load_image(name: str) -> Image.Image:
    return open_image((FIXTURE_DIR / name).read_bytes())


def dihedral_hashes(image: Image.Image) -> tuple[list[bytes], int]:
    """The eight orientations exactly as `matching.fingerprint.fingerprint_video` computes them."""
    pixels = np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))
    variants, quality = pdqhash.compute_dihedral(pixels)
    hashes = [np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes() for bits in variants]
    return hashes, int(quality)


@pytest.mark.parametrize("vector", PILLOW_VECTORS, ids=lambda vector: str(vector["file"]))
def test_pillow_vectors_match_bit_for_bit(vector: dict[str, Any]) -> None:
    """Meta's Python vectors were produced by Pillow + pdqhash: we must reproduce them exactly."""
    image = load_image(vector["file"])
    assert image.mode == vector["mode"], "fixture decoded in an unexpected mode"
    result = hash_image(image)
    assert to_hex(result.hash) == vector["hash"], f"{vector['file']}: hash differs from upstream"
    assert result.quality == vector["quality"], f"{vector['file']}: quality differs from upstream"


def test_reference_c_hasher_single_image() -> None:
    """`pdq-photo-hasher --details bridge-1-original.jpg`, decoder drift only."""
    expected = VECTORS["photo_hasher"]
    result = hash_image(load_image(expected["file"]))
    distance = hamming_distance(result.hash, from_hex(expected["hash"]))
    assert distance <= DECODER_TOLERANCE, (
        f"{expected['file']}: {distance} bits from Meta's C++ hasher (allowed "
        f"{DECODER_TOLERANCE}; measured 6 when this test was written)"
    )
    assert result.quality == expected["quality"]


def test_dihedral_variant_count() -> None:
    assert len(DIHEDRAL_VECTORS) == DIHEDRAL_ORIENTATIONS
    assert [entry["xform"] for entry in DIHEDRAL_VECTORS] == [
        entry["xform"] for entry in TRANSFORMED_VECTORS
    ]


@pytest.mark.parametrize(
    "index, vector",
    list(enumerate(DIHEDRAL_VECTORS)),
    ids=[str(entry["xform"]) for entry in DIHEDRAL_VECTORS],
)
def test_dihedral_variants_match_reference(index: int, vector: dict[str, Any]) -> None:
    """Each `compute_dihedral` variant against `pdq-photo-hasher --pdqdih --details`."""
    hashes, quality = dihedral_hashes(load_image(VECTORS["dihedral"]["file"]))
    distance = hamming_distance(hashes[index], from_hex(vector["hash"]))
    assert distance <= DECODER_TOLERANCE, (
        f"variant {index} ({vector['xform']}): {distance} bits from Meta's reference "
        f"(allowed {DECODER_TOLERANCE}; measured 4-8 when this test was written)"
    )
    assert quality == VECTORS["dihedral"]["quality"]


def test_dihedral_order_follows_the_real_transform() -> None:
    """Variant k must be the nearest of our eight to the file Meta transformed by k.

    This is the part the reference data proves that our own fixtures cannot: the orientations are
    in the documented order (orig, rot90, rot180, rot270, flipx, flipy, flipp, flipm), and each one
    really is that geometric transform of the image - the sibling JPEGs were rotated on disk by
    Meta and hashed independently.
    """
    hashes, _ = dihedral_hashes(load_image(VECTORS["dihedral"]["file"]))
    for index, vector in enumerate(TRANSFORMED_VECTORS):
        reference = from_hex(vector["hash"])
        distances = [hamming_distance(candidate, reference) for candidate in hashes]
        nearest = min(range(DIHEDRAL_ORIENTATIONS), key=lambda i: distances[i])
        runner_up = sorted(distances)[1]
        assert nearest == index, (
            f"{vector['file']}: variant {nearest} is closer than variant {index} "
            f"({distances[nearest]} vs {distances[index]} bits) - dihedral order is wrong"
        )
        assert distances[index] <= TRANSFORM_TOLERANCE, (
            f"{vector['file']}: {distances[index]} bits from Meta's hash of the transformed file "
            f"(allowed {TRANSFORM_TOLERANCE}; measured 6-14 when this test was written)"
        )
        assert runner_up >= MIN_ORIENTATION_MARGIN, (
            f"{vector['file']}: the next-best variant is only {runner_up} bits away, so this test "
            "no longer distinguishes the orientations"
        )


def test_fixtures_are_committed_and_small() -> None:
    """`make check` must run this offline, so every image the vectors need is in the repo."""
    needed = {str(VECTORS["photo_hasher"]["file"]), str(VECTORS["dihedral"]["file"])}
    needed.update(str(vector["file"]) for vector in PILLOW_VECTORS)
    for name in sorted(needed):
        assert (FIXTURE_DIR / name).is_file(), f"missing fixture {name}"
    assert not VECTORS["transformed_files"]["committed"], (
        "the rotated sibling images are hashes-only on purpose; keep the fixture set small"
    )
    total = sum(path.stat().st_size for path in FIXTURE_DIR.iterdir())
    assert total <= MAX_FIXTURE_BYTES, f"fixtures grew to {total} bytes"


def test_provenance_is_recorded() -> None:
    """A reference test is worthless without a pinned upstream revision."""
    source = VECTORS["source"]
    assert source["repository"].endswith("/ThreatExchange")
    assert len(source["commit"]) == 40
    assert "LICENSE" in source["licence"]
    assert (FIXTURE_DIR / "LICENSE").is_file()
    assert (FIXTURE_DIR / "README.md").is_file()
