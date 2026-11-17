"""Meta's PDQ perceptual image hash (256 bits) via the `pdqhash` bindings.

Hash bytes use Meta's canonical bit order: `hash.hex()` is the same 64-character string the
ThreatExchange PDQ tools print, so hashes are interchangeable with other PDQ implementations.

PDQ is not flip or rotation invariant: a mirrored reupload lands far away (see tests).
`pdqhash.compute_dihedral` hashes all 8 orientations from the pixels, which is what every
reference import does; `dihedral_from_hash` below approximates one orientation from the stored
hash alone, for the frames whose pixels are long gone (issue #8,
`docs/matching/page-side-flips.md`).
"""

import io
from typing import NamedTuple

import numpy as np
import pdqhash
from PIL import Image, UnidentifiedImageError

PDQ_HASH_BYTES = 32
# Meta's guidance: below ~50 an image is too featureless (blank, near-black) to match on.
MIN_QUALITY = 50
# Meta's recommended starting threshold; tune against labeled pairs (evaluate_threshold).
MATCH_DISTANCE = 31
# The dihedral group of the square, in ThreatExchange's `--pdqdih` order. `compute_dihedral`
# returns its eight hashes in exactly this order (pinned by test_pdq_threatexchange).
DIHEDRAL_ORIENTATIONS = 8
ORIENTATION_NAMES = ("orig", "rot90", "rot180", "rot270", "flipx", "flipy", "flipp", "flipm")
# The left-right mirror, the only orientation a video re-upload plausibly applies.
MIRROR_ORIENTATION = ORIENTATION_NAMES.index("flipy")
# The hash is a 16x16 bit matrix: row = vertical DCT frequency, column = horizontal. Each dihedral
# transform of the picture permutes that matrix and negates half its coefficients, so deriving an
# orientation from the hash is a transpose plus an XOR mask. See `dihedral_from_hash`.
_HASH_SIDE = 16
_ROW, _COLUMN = np.mgrid[0:_HASH_SIDE, 0:_HASH_SIDE]
_ZERO = np.zeros((_HASH_SIDE, _HASH_SIDE), dtype=np.uint8)
_ODD_ROW = (_ROW & 1).astype(np.uint8)
_ODD_COLUMN = (_COLUMN & 1).astype(np.uint8)
_ODD_SUM = ((_ROW + _COLUMN) & 1).astype(np.uint8)
# (transpose?, negation mask) per orientation, in ORIENTATION_NAMES order.
_DIHEDRAL_RECIPE = (
    (False, _ZERO),
    (True, _ODD_ROW),
    (False, _ODD_SUM),
    (True, _ODD_COLUMN),
    (False, _ODD_ROW),
    (False, _ODD_COLUMN),
    (True, _ZERO),
    (True, _ODD_SUM),
)
# Measured over 159 frames (docs/matching/page-side-flips.md): the two permutation-only
# orientations are exact, the six that negate coefficients cost a median of 8 bits, p90 18, max 34.
DIHEDRAL_DERIVATION_MEDIAN = 8
DIHEDRAL_DERIVATION_MAX = 34
EXACT_DERIVED_ORIENTATIONS = frozenset(
    {ORIENTATION_NAMES.index("orig"), ORIENTATION_NAMES.index("flipp")}
)


class MediaDecodeError(Exception):
    """Media bytes could not be decoded."""


class PdqResult(NamedTuple):
    hash: bytes
    quality: int


class HashedFrame(NamedTuple):
    """One hashed picture. `embedding` is its 512-bit sign sketch, or None (ADR 0037).

    The sketch is optional by design: it exists only where the second signal was switched on when
    the picture was hashed, and nothing may read its absence as a negative.
    """

    frame_index: int
    hash: bytes
    quality: int
    embedding: bytes | None = None


def open_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise MediaDecodeError(f"cannot decode image: {exc}") from exc
    return image


def hash_image(image: Image.Image) -> PdqResult:
    pixels = np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))
    bits, quality = pdqhash.compute(pixels)
    # pdqhash returns bits most-significant first, matching Meta's hex format.
    return PdqResult(np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes(), int(quality))


def hash_image_bytes(data: bytes) -> PdqResult:
    return hash_image(open_image(data))


def _check(hash_: bytes) -> None:
    if len(hash_) != PDQ_HASH_BYTES:
        raise ValueError(f"PDQ hash must be {PDQ_HASH_BYTES} bytes, got {len(hash_)}")


def to_hex(hash_: bytes) -> str:
    _check(hash_)
    return bytes(hash_).hex()


def from_hex(text: str) -> bytes:
    value = bytes.fromhex(text)
    _check(value)
    return value


def hamming_distance(first: bytes, second: bytes) -> int:
    _check(first)
    _check(second)
    return (int.from_bytes(first) ^ int.from_bytes(second)).bit_count()


def dihedral_from_hash(hash_: bytes, orientation: int) -> bytes:
    """One dihedral orientation of a picture, derived from its PDQ hash alone — no pixels.

    **This is an approximation, and it is the only kind available retroactively.** PDQ thresholds a
    16x16 DCT coefficient matrix against that matrix's own median; each dihedral transform of the
    picture permutes the matrix and negates the coefficients of odd frequency, which moves the
    median, so a negated coefficient is not always a flipped bit. `orig` and `flipp` (the
    transpose) negate nothing and are therefore **exact**; the other six drift.

    Measured against `pdqhash.compute_dihedral` on the same pixels over 159 frames
    (`docs/matching/page-side-flips.md`, reproducible from committed fixtures): median 8 bits,
    p90 18, max 34 — well inside the strict threshold, but not free. Use it to *find* a mirrored
    copy, and re-hash the picture if an exact orientation is ever needed.

    Never use it on a reference: every import path hashes all eight orientations from the pixels.
    """
    _check(hash_)
    if not 0 <= orientation < DIHEDRAL_ORIENTATIONS:
        raise ValueError(f"orientation must be 0..{DIHEDRAL_ORIENTATIONS - 1}, got {orientation}")
    transpose, mask = _DIHEDRAL_RECIPE[orientation]
    bits = np.unpackbits(np.frombuffer(bytes(hash_), dtype=np.uint8)).reshape(
        _HASH_SIDE, _HASH_SIDE
    )
    if transpose:
        bits = np.ascontiguousarray(bits.T)
    return np.packbits(bits ^ mask).tobytes()


def dihedral_hashes_from_hash(hash_: bytes) -> list[bytes]:
    """All eight orientations derived from one hash, in `ORIENTATION_NAMES` order."""
    return [dihedral_from_hash(hash_, index) for index in range(DIHEDRAL_ORIENTATIONS)]
