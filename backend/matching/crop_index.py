"""The two-stage crop-grid search: 16 corners in the index, the blob decides (ADR 0039 §8.2).

`matching.crop_grid` is the pure half — the lattice, the blob layout, the minimum. This is the
database half: it loads the stage-1 prefixes, searches them in the same exact `IndexBinaryFlat`
everything else uses (ADR 0024) and then reads the candidate moments' blobs to get the real
distance.

**Stage 1 finds, stage 2 decides.**

1. Every grid moment contributes its **16 corner crops** to a `HashIndex` — +1,228,720 hashes for
   the current library — and the page frames are searched against it at **radius 64**. Measured:
   the corners at that radius find 0.99 of 3 % re-frames, at a false rate of 5.0e-6 per pair.

   **Measured resident set at library scale: +96 MB** (peak RSS over a bare numpy+faiss baseline,
   1,228,720 hashes, 2,000 queries at radius 64; the harness is throwaway). ADR 0039 predicted
   +79 MB and counted two copies of the codes: 37.5 MB in FAISS and 37.5 MB in `HashIndex`'s
   exact-rerank array. The other ~17 MB is real and was simply not counted — 9.4 MB of `HashIndex`
   id array (one int64 per hash), 1.8 MB of this module's per-moment side columns and the search
   buffers. Keeping the side columns **per moment rather than per hash** is what holds that number
   down: repeating them per corner cost a measured 29 MB more. At the ADR's 500-original ceiling
   (~873 k moments) this is ~1.1 GB rather than ~900 MB, which moves that ceiling nearer, not
   further — ADR 0024's IVF backend or a per-organisation index is what answers it.
2. Each surviving (page frame, moment) pair reads that moment's blob and takes the exact minimum
   over all 256 crops. Composite recall 0.95. Stage 1's false rate costs about 35,000 blob reads
   per full scan, each 256 XORs (~3 µs) — cheap work, never a false match, because the decision is
   taken here at the ordinary PDQ threshold.

**There is no radius query over the blobs**, and adding one is the mistake this design exists to
avoid: it would mean putting 19.7 M hashes in the searchable index (9.6 GB of rows, 630 MB of
FAISS, doubled by the re-rank copy) to answer what two stages answer for +79 MB. The blob is
reached only after stage 1 has already named the moment.

**A grid hit is ordinary PDQ evidence.** Unlike ADR 0037's sketch — which by its own measurement
can never carry a threshold — a crop is a PDQ hash of the same original in the same units, so its
distance goes into `PageEvidence` exactly like an uncropped one and is judged at the same
`max_distance`. That is also the trap: **a minimum over 256 correlated hashes shifts the whole
distance distribution down 10-16 bits** (`reference-crop-grid.md` §4.4, negative median 128 ->
112), and `matching/confidence.py` maps distance to a score on the old scale. **Adopting the grid
therefore requires re-running `calibrate_confidence`**, or grid-derived matches score generously.
Nothing here does that for you; ADR 0039 §5 and the pilot's own output say it again.

Off unless `MATCHING_REFERENCE_CROP_GRID` — see `enabled()`. A library without grids behaves
exactly as it does today either way, because `load_stage1` finds nothing.
"""

from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.db.models import BinaryField, QuerySet
from django.db.models.functions import Substr
from numpy.typing import NDArray

from matching import crop_grid
from matching.hamming import HASH_BYTES, HashIndex, to_words
from matching.models import ReferenceFrameHash
from matching.pdq import MIN_QUALITY

#: Rows whose blobs one stage-2 pass pulls out of the database at a time.
BLOB_CHUNK = 500
#: Page frames searched against the corner index per stage-1 call (the result buffer is the cost).
QUERY_CHUNK = 20_000
_PREFIX_BYTES = crop_grid.STAGE1_CROPS * HASH_BYTES


def enabled() -> bool:
    """Django setting `MATCHING_REFERENCE_CROP_GRID` (default off, ADR 0039 §7).

    It decides one thing only: whether the matcher puts stage-1 crops in the index and consults
    the blobs. It never changes what is stored, and turning it off again restores today's
    behaviour exactly — the blobs simply stop being read.
    """
    return bool(getattr(settings, "MATCHING_REFERENCE_CROP_GRID", False))


@dataclass(frozen=True)
class Stage1Index:
    """The corner crops of every grid moment, and the moments they belong to.

    `words` holds the hashes **`STAGE1_CROPS` per moment, contiguously and in blob order**, so
    entry `i` belongs to moment `i // STAGE1_CROPS` and the four side columns stay one row per
    moment instead of one per hash. That is not a micro-optimisation: repeating them per hash cost
    a measured 29 MB of the matcher's resident set at library scale, against 1.8 MB this way.
    """

    #: One row per moment: the `ReferenceFrameHash` pk, its reference, its moment and its flag.
    row_pk: NDArray[np.int64]
    reference_id: NDArray[np.int64]
    timestamp_s: NDArray[np.float64]
    shared: NDArray[np.bool_]
    #: (moments * STAGE1_CROPS, 4) uint64, moment-major.
    words: NDArray[np.uint64]

    def __len__(self) -> int:
        """Corner hashes, i.e. what goes into the searchable index."""
        return len(self.words)

    @property
    def moments(self) -> int:
        return len(self.row_pk)

    def moment_of(self, entry: int) -> int:
        """Which moment a stage-1 hit at index `entry` belongs to."""
        return entry // crop_grid.STAGE1_CROPS

    @classmethod
    def empty(cls) -> "Stage1Index":
        return cls(
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.float64),
            np.empty(0, dtype=np.bool_),
            np.empty((0, HASH_BYTES // 8), dtype=np.uint64),
        )

    def ram_bytes(self) -> int:
        """What this index costs before FAISS builds its own copy of the codes."""
        return sum(
            array.nbytes
            for array in (self.row_pk, self.reference_id, self.timestamp_s, self.shared, self.words)
        )


def grid_rows(
    reference_ids: Collection[int] | None = None, min_quality: int = MIN_QUALITY
) -> QuerySet[ReferenceFrameHash]:
    """Moments that carry a grid, quality-eligible, unflipped. The one place that filter lives."""
    rows = ReferenceFrameHash.objects.filter(
        crop_grid__isnull=False, dihedral=0, quality__gte=min_quality
    )
    if reference_ids is not None:
        rows = rows.filter(reference_id__in=sorted(reference_ids))
    return rows


def load_stage1(
    reference_ids: Collection[int] | None = None, min_quality: int = MIN_QUALITY
) -> Stage1Index:
    """The corner prefixes of every grid moment, as arrays.

    Only the **prefix** is read (`SUBSTRING(crop_grid FROM 1 FOR 512)`), so this costs 39 MB of I/O
    for the current library instead of the 629 MB the whole blobs would be. The blobs themselves
    are read in stage 2, for the few thousand moments that stage 1 actually named.
    """
    pks: list[int] = []
    references: list[int] = []
    stamps: list[float] = []
    flags: list[bool] = []
    prefixes: list[bytes] = []
    rows = (
        grid_rows(reference_ids, min_quality)
        .annotate(prefix=Substr("crop_grid", 1, _PREFIX_BYTES, output_field=BinaryField()))
        .order_by("pk")
        .values_list("pk", "reference_id", "timestamp_s", "shared", "prefix")
    )
    for pk, reference_id, timestamp, shared, prefix in rows.iterator(chunk_size=2000):
        value = bytes(prefix)
        if len(value) != _PREFIX_BYTES:
            # The database constraint makes this unreachable; a blob that lost its prefix would
            # make stage 1 search crops it cannot name, so it is skipped rather than guessed at.
            continue
        pks.append(int(pk))
        references.append(int(reference_id))
        stamps.append(float(timestamp))
        flags.append(bool(shared))
        prefixes.append(value)
    if not pks:
        return Stage1Index.empty()
    words = np.frombuffer(b"".join(prefixes), dtype=np.uint64).reshape(-1, HASH_BYTES // 8).copy()
    return Stage1Index(
        row_pk=np.asarray(pks, dtype=np.int64),
        reference_id=np.asarray(references, dtype=np.int64),
        timestamp_s=np.asarray(stamps, dtype=np.float64),
        shared=np.asarray(flags, dtype=np.bool_),
        words=words,
    )


@dataclass(frozen=True)
class GridHit:
    """One page frame against one reference moment, at the exact minimum over that moment's grid.

    `position` indexes the page frames handed to `search`. `distance` is a PDQ Hamming distance in
    PDQ's own units — the same number an uncropped hit carries, and usable as evidence for exactly
    that reason.
    """

    position: int
    row_pk: int
    reference_id: int
    timestamp_s: float
    shared: bool
    distance: int


def _blobs(row_pks: Sequence[int]) -> Iterator[tuple[int, bytes]]:
    for start in range(0, len(row_pks), BLOB_CHUNK):
        chunk = row_pks[start : start + BLOB_CHUNK]
        for pk, blob in ReferenceFrameHash.objects.filter(pk__in=chunk).values_list(
            "pk", "crop_grid"
        ):
            yield int(pk), bytes(blob or b"")


def _candidates(
    stage1: Stage1Index, queries: NDArray[np.uint64], stage1_radius: int
) -> dict[int, list[int]]:
    """Stage 1: moment index -> page frame positions whose corner search reached it.

    Sixteen corners of one moment routinely answer the same query, which is why this collapses to
    moments before stage 2 reads anything: the 8 KB blob is read once per moment, never per corner.
    """
    index = HashIndex.from_words(np.arange(len(stage1), dtype=np.int64), stage1.words)
    found: dict[int, set[int]] = {}
    for start in range(0, len(queries), QUERY_CHUNK):
        positions, entries, _ = index.pairs_within(
            queries[start : start + QUERY_CHUNK], stage1_radius
        )
        for position, entry in zip(positions.tolist(), entries.tolist(), strict=True):
            found.setdefault(stage1.moment_of(int(entry)), set()).add(start + int(position))
    return {moment: sorted(seen) for moment, seen in found.items()}


def search(
    stage1: Stage1Index,
    page_words: NDArray[np.uint64],
    *,
    radius: int,
    stage1_radius: int = crop_grid.STAGE1_RADIUS,
) -> list[GridHit]:
    """Both stages: hits whose exact grid minimum is within `radius`.

    `page_words` is the (N, 4) uint64 array of page frame hashes; `position` in the result indexes
    it. `stage1_radius` is the wide search that only has to *find* the moment — never the decision,
    which is `radius` and is the matcher's own threshold.
    """
    if not len(stage1) or not len(page_words):
        return []
    candidates = _candidates(stage1, page_words, stage1_radius)
    if not candidates:
        return []
    moment_of_pk = {int(stage1.row_pk[moment]): moment for moment in candidates}
    hits: list[GridHit] = []
    for row_pk, blob in _blobs(sorted(moment_of_pk)):
        moment = moment_of_pk[row_pk]
        positions = candidates[moment]
        distances = crop_grid.best_distances(page_words[positions], blob)
        if distances is None:
            # No crop of this moment survived the quality floor. That is "no information", never
            # distance 0 — the same rule the sketches follow (ADR 0037).
            continue
        for position, distance in zip(positions, distances.tolist(), strict=True):
            if distance <= radius:
                hits.append(
                    GridHit(
                        position=position,
                        row_pk=row_pk,
                        reference_id=int(stage1.reference_id[moment]),
                        timestamp_s=float(stage1.timestamp_s[moment]),
                        shared=bool(stage1.shared[moment]),
                        distance=int(distance),
                    )
                )
    return hits


def search_hashes(
    stage1: Stage1Index,
    page_hashes: Sequence[bytes],
    *,
    radius: int,
    stage1_radius: int = crop_grid.STAGE1_RADIUS,
) -> list[GridHit]:
    """`search` over `bytes` hashes, for callers that do not already hold a word array."""
    return search(stage1, to_words(page_hashes), radius=radius, stage1_radius=stage1_radius)


def coverage(reference_ids: Collection[int] | None = None) -> tuple[int, int]:
    """(moments with a grid, references with at least one) — what an operator wants to see."""
    rows = grid_rows(reference_ids)
    return rows.count(), rows.values("reference_id").distinct().count()
