"""Reference-side crop grids: many hashes per reference moment, one per page picture.

`docs/matching/reference-crop-grid.md` measures why this exists. A site re-frames the picture it
publishes — it trims 1-3 % off the edges, asymmetrically — and that costs PDQ 44-70 bits, which is
the whole of the recall the index is losing (`docs/matching/second-signal.md` §4.6 measured the
regrade, the sharpen and the recompression at 0-2 bits, and `docs/matching/black-bars.md` measured
that there is no padding in our index at all).

The asymmetry that makes this fixable: **we own the originals**. Enumerating crops on the
*reference* side is offline and retroactive over every page hash already stored, where the page
side costs a re-fetch of media whose URLs partly no longer resolve. So the reference moment gets a
lattice of plausible crops and a page picture matches if it is within threshold of **any** of them.

Measured on 160 real film frames in the production geometry, against 805,683 genuine negative
pairs (`docs/matching/reference-crop-grid.md` §3, §4):

| grid | 3 % re-frame within 31 | closest of 805,683 negatives |
|---|---:|---:|
| 1 hash (today) | 0.02 | 58 bits |
| 81 crops (step 3 %, max 6 %) | 0.88 | 54 bits |
| 256 crops (step 2 %, max 6 %) | **0.95** | 54 bits |
| 2,401 crops (step 1 %, max 6 %) | 0.98 | 54 bits |

The precision cost lives above 48 bits: **not one unrelated pair came within 48 bits of any crop of
a moment**, at any grid size, and the closest of the whole study sits at 54. So the grid buys recall
at the production threshold of 31 for no measurable precision.

This module is the lattice, the constants with their measurements, the blob layout and the
minimum-distance query (ADR 0039, issue #80). Everything it feeds is **off by default**:
`matching.fingerprint` only produces a grid when asked, and the matcher only consults one when
`MATCHING_REFERENCE_CROP_GRID` is on. The database side is `matching.crop_index`.

**Adopting the grid requires re-running `calibrate_confidence`.** A minimum over 256 correlated
hashes shifts the *whole* distance distribution down — measured 10-16 bits, negative median 128 ->
112 (`reference-crop-grid.md` §4.4) — and `matching/confidence.py` maps distance to a score on the
old scale. Grid-derived matches score slightly generously until the bands are re-derived. This is
the one adoption step that cannot be skipped, and it is stated again in `matching/crop_index.py`
and in ADR 0039 §5.

Pure: no Django, no I/O, numpy and Pillow only, so `matching.fingerprint` and
`client/fingerprint.py` can both import it.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import product

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from matching.pdq import PDQ_HASH_BYTES, PdqResult, hash_image

# The grid the study recommends. Both numbers are measured, not chosen:
#
# STEP 2 % — the lattice's rounding error is what a grid pays, and the cost of being wrong by s on
#   one edge is ~20 bits per percent (§2). A 2 % step leaves at most 1 % per edge, and the true
#   minimum over the lattice is better than that bound because neighbouring crops are also close:
#   0.95-0.96 of 2-3 % re-frames land inside 31 bits (§3). A 3 % step gives 0.88, a 4 % step 0.41.
# MAX_TRIM 6 % — a re-frame of magnitude m draws each edge from U(0, 2m), so 6 % covers the 1-3 %
#   range the sites in our index actually apply. Widening it to 10 % buys 5 % re-frames (0.67 at
#   256 crops) and to 20 % buys almost nothing affordable (0.09 at 256 crops): past ~3 % the grid
#   stops being the cheap answer (§3.4).
STEP = 0.02
MAX_TRIM = 0.06
# The small grid meant for the searchable index: the 2^4 corners of the same box. On its own it
# recalls only 0.09 of 3 % re-frames at 31 bits, but at a radius of 64 it *finds* 0.99 of them, and
# the full grid then decides. That two-stage split is what keeps the index small (§8.2).
STAGE1_LEVELS = (0.0, MAX_TRIM)
# How many crops stage 1 puts in the searchable index, and the radius it searches at. Both are
# measured (§8.2): 16 corners at radius 64 find 0.99 of 3 % re-frames, composite recall 0.95, at a
# stage-1 false rate of 5.0e-6 per pair — about 35,000 blob reads per full scan, which buys cheap
# work and never a false match, because stage 2 decides at the real threshold. Radius 56 costs
# 0.07 of composite recall; 81 crops at radius 48 match 64 and cost 5x the index.
STAGE1_CROPS = 2**4
STAGE1_RADIUS = 64
# A crop must not be hashed below the quality floor the rest of the pipeline uses.
MIN_QUALITY = 50
# Optional: drop a crop whose hash is within this many bits of one already kept, exactly as
# `matching.media_hash` prunes a preview clip. The crops of one moment are highly correlated, so
# this is nearly free storage. Measured over 60 moments of the 2 % lattice (§3.3): at 24 bits a
# median of 139 of 256 crops survive (4.3 KB instead of 8.2) and recall goes 0.95 -> 0.92 at a
# threshold of 31 and 0.98 -> 0.97 at 36. The negative tail does not move at all. Pruning is off by
# default because at the current library size the storage it saves is not worth the recall.
DEDUP_BITS = 24
# Cut the grid from a picture at least this wide. Cropping the 512 px picture the reference recipe
# already produced, instead of the master frame, costs a median of 8 bits (p90 12, max 34) — a
# third of the threshold budget for nothing. From a 1024 px intermediate it costs a median of 2
# (p90 4, max 10); from the master, 0 (§8.1).
MIN_SOURCE_WIDTH = 1024
# What a crop is scaled to before hashing, and how. The study's geometry (§1.2) is "crop the master
# frame, then scale='min(512,iw)':-2, then PDQ", i.e. the same 512 px the reference recipe hashes
# at — but applied *after* the crop, which is the whole of §8.1. ffmpeg is not in the loop here
# (it emits the master), so the scaler is Pillow's and the resulting hashes are a few bits from the
# ffmpeg ones. That is exactly why a grid reference carries its own pipeline tag under ADR 0022
# (`matching.fingerprint.CROP_GRID_PIPELINE`): the recipe changed, so the tag does, and the old
# rows keep meaning what they meant.
OUTPUT_WIDTH = 512
RESAMPLING = Image.Resampling.BICUBIC


@dataclass(frozen=True, slots=True)
class Crop:
    """Edge trims as fractions of the corresponding dimension."""

    left: float
    top: float
    right: float
    bottom: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.left, self.top, self.right, self.bottom)


def lattice(step: float = STEP, max_trim: float = MAX_TRIM) -> list[Crop]:
    """Every combination of the four edge trims on a lattice of `step`, up to `max_trim`.

    Size is `(max_trim / step + 1) ** 4`: 16 at step 6 %, 81 at 3 %, 256 at 2 %, 2401 at 1 %.
    """
    if step <= 0 or max_trim < 0 or step > max_trim:
        raise ValueError(f"step {step} must be positive and no larger than max_trim {max_trim}")
    levels = round(max_trim / step)
    values = [round(i * step, 10) for i in range(levels + 1)]
    return [Crop(*combo) for combo in product(values, values, values, values)]


def corner_lattice(max_trim: float = MAX_TRIM) -> list[Crop]:
    """The 16 corners of the same box — the small grid that goes into the searchable index."""
    values = [0.0, max_trim]
    return [Crop(*combo) for combo in product(values, values, values, values)]


def grid_specs(step: float = STEP, max_trim: float = MAX_TRIM) -> list[Crop]:
    """The lattice in **stored order**: the 16 corners first, then everything else.

    This is the blob's layout and the one thing a reader has to agree with a writer about. Stage 1
    finds a moment by the first `STAGE1_CROPS` entries of its blob, so those entries must be the
    corner crops, in `corner_lattice` order, always. The rest follows in `lattice` order, which
    makes a blob self-describing given (step, max_trim) even after pruning removed entries from the
    tail — pruning never touches the prefix (`prune(keep_first=STAGE1_CROPS)`).

    The zero crop is `corner_lattice()[0]`, so entry 0 of every blob is the uncropped moment.
    """
    corners = corner_lattice(max_trim)
    seen = set(corners)
    return corners + [spec for spec in lattice(step, max_trim) if spec not in seen]


def scale_to_output(image: Image.Image, width: int = OUTPUT_WIDTH) -> Image.Image:
    """ffmpeg's `scale='min(W,iw)':-2` in Pillow: at most W wide, height rounded to an even number.

    The `-2` is what makes this reproducible: ffmpeg picks the height that preserves the aspect
    ratio and is divisible by two, rounding to the *nearest* even value. `matching.fingerprint`'s
    browser pipeline records the same rule (and the v0 tag that floored it instead).
    """
    target = min(width, image.width)
    height = max(2, round(image.height * target / image.width / 2) * 2)
    if (target, height) == (image.width, image.height):
        return image
    return image.resize((target, height), RESAMPLING)


def render(image: Image.Image, spec: Crop, width: int = OUTPUT_WIDTH) -> Image.Image:
    """One grid point: trim the master frame's edges, then scale. Crop first — that is §8.1."""
    return scale_to_output(crop(image, spec), width)


def crop(image: Image.Image, spec: Crop) -> Image.Image:
    """Trim the four edges. The same object comes back for the zero crop, so hashing the grid's
    first point is bit-identical to hashing the picture (the tests assert it)."""
    if spec.left == spec.top == spec.right == spec.bottom == 0.0:
        return image
    width, height = image.width, image.height
    x0 = round(spec.left * width)
    y0 = round(spec.top * height)
    x1 = width - round(spec.right * width)
    y1 = height - round(spec.bottom * height)
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError(f"crop {spec} leaves less than 8 px of {width}x{height}")
    return image.crop((x0, y0, x1, y1))


def hash_grid(
    image: Image.Image, specs: Sequence[Crop] | None = None
) -> Iterator[tuple[Crop, PdqResult]]:
    """PDQ every crop of `image`. The caller scales the crop if its pipeline does."""
    for spec in specs if specs is not None else lattice():
        yield spec, hash_image(crop(image, spec))


def prune(
    results: Sequence[PdqResult], *, bits: int = DEDUP_BITS, keep_first: int = 0
) -> list[PdqResult]:
    """Greedily drop crops within `bits` of one already kept, keeping the first `keep_first`
    unconditionally.

    `keep_first` exists so the 16 corner crops — stage 1 of the two-stage search — survive pruning
    and stay a known prefix of the blob. Call it with the corners first in `results`.
    """
    kept: list[PdqResult] = list(results[:keep_first])
    words = [np.frombuffer(bytes(r.hash), dtype=np.uint64) for r in kept]
    for result in results[keep_first:]:
        query = np.frombuffer(bytes(result.hash), dtype=np.uint64)
        if words and int(np.bitwise_count(np.stack(words) ^ query[None, :]).sum(1).min()) <= bits:
            continue
        kept.append(result)
        words.append(query)
    return kept


def pack(results: Sequence[PdqResult], *, min_quality: int = MIN_QUALITY) -> bytes:
    """The stored blob: the 32-byte hashes of every crop above the quality floor, concatenated.

    Order is the lattice's own order, so a blob plus its (step, max_trim) is self-describing and a
    reader never needs the crops themselves. 8,192 bytes for the 256-crop grid.

    **The quality floor can shift the blob's prefix**, which is the one trap here: stage 1 finds a
    moment by its first `n` entries, and if one of those crops is below the floor it is simply not
    in the blob and entry `n` is a different crop than the caller thinks. `stage1_intact` is the
    check; a moment that fails it gets no grid at all rather than a mislabelled one.
    """
    kept = [bytes(r.hash) for r in results if r.quality >= min_quality]
    for value in kept:
        if len(value) != PDQ_HASH_BYTES:
            raise ValueError(f"PDQ hash must be {PDQ_HASH_BYTES} bytes, got {len(value)}")
    return b"".join(kept)


def stage1_intact(
    results: Sequence[PdqResult], *, keep_first: int, min_quality: int = MIN_QUALITY
) -> bool:
    """True when every stage-1 crop is above the quality floor, i.e. when the blob really does
    begin with them. A moment that fails this must be stored without a grid."""
    return all(r.quality >= min_quality for r in results[:keep_first])


@dataclass(frozen=True)
class Grid:
    """One moment's finished grid: the blob, the uncropped hash and what it took to make it."""

    blob: bytes
    #: The zero crop — entry 0 of the blob, and the hash the `ReferenceFrameHash` row stores.
    base: PdqResult
    #: Crops hashed, before the quality floor and pruning dropped any.
    generated: int
    #: Crops actually in the blob.
    kept: int
    #: The dedup radius used, or None when the grid was stored whole.
    dedup_bits: int | None

    def __len__(self) -> int:
        return self.kept


def build(
    master: Image.Image,
    *,
    specs: Sequence[Crop] | None = None,
    min_quality: int = MIN_QUALITY,
    dedup_bits: int | None = None,
    width: int = OUTPUT_WIDTH,
) -> Grid | None:
    """Hash every crop of one master frame into a blob. The writer, end to end.

    `master` must be at least `MIN_SOURCE_WIDTH` wide (§8.1) — cutting the grid from the 512 px
    picture the plain recipe emits costs a median of 8 bits of a 23-bit margin, so this raises
    rather than quietly producing a worse grid.

    Returns **None** when the quality floor would shift the blob's prefix (`stage1_intact`): stage 1
    finds a moment by its first `STAGE1_CROPS` entries, and a moment whose corners are not all
    above the floor gets no grid at all rather than a mislabelled one. A moment whose *base* crop is
    below the floor is not stored by the pipeline either, so that case lands here too.

    `dedup_bits` turns on the optional corner-seeded dedup of §3.3: at 24 bits a median of 139 of
    256 crops survive (4.3 KB instead of 8.2) and recall goes 0.95 -> 0.92 at a threshold of 31,
    0.98 -> 0.97 at 36, with the negative tail unmoved. Off by default — at 44 originals the
    storage it saves is not worth the recall.

    The pictures are hashed and dropped; nothing here keeps or returns an image.
    """
    if master.width < MIN_SOURCE_WIDTH:
        raise ValueError(
            f"a crop grid needs a master frame at least {MIN_SOURCE_WIDTH} px wide, "
            f"got {master.width} (docs/matching/reference-crop-grid.md §8.1)"
        )
    points = list(specs) if specs is not None else grid_specs()
    results = [hash_image(render(master, spec, width)) for spec in points]
    if not stage1_intact(results, keep_first=STAGE1_CROPS, min_quality=min_quality):
        return None
    kept = (
        results if dedup_bits is None else prune(results, bits=dedup_bits, keep_first=STAGE1_CROPS)
    )
    return Grid(
        blob=pack(kept, min_quality=min_quality),
        base=results[0],
        generated=len(results),
        kept=sum(1 for result in kept if result.quality >= min_quality),
        dedup_bits=dedup_bits,
    )


def corner_hashes(blob: bytes, count: int = STAGE1_CROPS) -> list[bytes]:
    """The stage-1 prefix of a stored blob: the `count` corner crops, in `grid_specs` order.

    A blob that does not even hold its prefix was not written by `build` and is refused rather
    than searched on whatever happens to be there.
    """
    if len(blob) < count * PDQ_HASH_BYTES:
        raise ValueError(
            f"blob of {len(blob)} bytes cannot hold {count} stage-1 crops "
            f"({count * PDQ_HASH_BYTES} bytes)"
        )
    return [
        bytes(blob[start : start + PDQ_HASH_BYTES])
        for start in range(0, count * PDQ_HASH_BYTES, PDQ_HASH_BYTES)
    ]


def unpack(blob: bytes) -> NDArray[np.uint64]:
    """A blob as an (n, 4) uint64 array, the layout `matching.hamming` compares."""
    if len(blob) % PDQ_HASH_BYTES:
        raise ValueError(f"blob of {len(blob)} bytes is not a whole number of PDQ hashes")
    return np.frombuffer(blob, dtype=np.uint64).reshape(-1, PDQ_HASH_BYTES // 8)


def best_distance(page_hash: bytes, blob: bytes) -> int | None:
    """The smallest Hamming distance from one page hash to any crop in the blob.

    This is the whole of stage 2: one blob read and `n` XORs. None for an empty blob — a moment
    whose every crop fell below the quality floor, which must not read as distance 0.
    """
    if len(page_hash) != PDQ_HASH_BYTES:
        raise ValueError(f"PDQ hash must be {PDQ_HASH_BYTES} bytes, got {len(page_hash)}")
    grid = unpack(blob)
    if not grid.size:
        return None
    query = np.frombuffer(bytes(page_hash), dtype=np.uint64)
    return int(np.bitwise_count(grid ^ query[None, :]).sum(axis=1).min())


def best_distances(queries: NDArray[np.uint64], blob: bytes) -> NDArray[np.int64] | None:
    """`best_distance` for a batch of page hashes against one blob, as an (N,) int64 array.

    Stage 2 of the two-stage search, vectorised: one blob read, `N x n` XORs. None for an empty
    blob, for the same reason `best_distance` returns None — no crop survived the quality floor,
    and that must never read as distance 0.

    **There is deliberately no radius query over a blob.** A blob is reached only because stage 1
    already found the moment in the exact `IndexBinaryFlat` (ADR 0024); what stage 2 answers is
    "how far is this moment, really", a minimum, never "which moments are within r". Searching the
    grids directly would put 19.7 M hashes in the index (9.6 GB of rows, 630 MB of FAISS) to
    answer a question the two stages already answer for +79 MB — §8.2 is the measurement.
    """
    if queries.ndim != 2 or queries.shape[1] != PDQ_HASH_BYTES // 8:
        raise ValueError("queries must be an (N, 4) uint64 array")
    grid = unpack(blob)
    if not grid.size or not len(queries):
        return None if not grid.size else np.empty(0, dtype=np.int64)
    xor = np.bitwise_count(queries[:, None, :] ^ grid[None, :, :])
    return xor.sum(axis=2, dtype=np.int64).min(axis=1)


def blob_bytes(step: float = STEP, max_trim: float = MAX_TRIM) -> int:
    """What one moment's grid costs on disk, before row overhead."""
    return len(lattice(step, max_trim)) * PDQ_HASH_BYTES
