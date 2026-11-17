"""Every reference frame hash of a matching run, loaded once as numpy arrays (RE3).

A `match_references` run used to read all `ReferenceFrameHash` rows three times — for the candidate
scan, for shared-frame detection and for aggregation — each time as Python tuples of `bytes`
(~2 s and ~400 MB extra at 580k hashes). This holds one copy as parallel arrays that every stage
masks instead of re-querying. It is the prerequisite for denser reference sampling (2-4 fps
multiplies the row count).

The arrays are a snapshot: `shared` is updated in place by `mark_shared_frames`, nothing else
changes after loading.
"""

from collections.abc import Collection
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from matching.hamming import HASH_BYTES
from matching.models import ReferenceFrameHash

_WORDS = HASH_BYTES // 8
_CHUNK = 20_000


@dataclass(frozen=True)
class ReferenceHashes:
    pk: NDArray[np.int64]
    reference_id: NDArray[np.int64]
    frame_index: NDArray[np.int64]
    dihedral: NDArray[np.int64]
    quality: NDArray[np.int64]
    timestamp_s: NDArray[np.float64]
    shared: NDArray[np.bool_]
    words: NDArray[np.uint64]

    def __len__(self) -> int:
        return len(self.pk)

    def eligible(self, min_quality: int) -> NDArray[np.bool_]:
        return self.quality >= min_quality

    def of_references(self, reference_ids: Collection[int]) -> NDArray[np.bool_]:
        return np.isin(self.reference_id, np.fromiter(reference_ids, dtype=np.int64))

    @classmethod
    def from_rows(
        cls, rows: "list[tuple[int, int, int, int, int, float, bool, bytes]]"
    ) -> "ReferenceHashes":
        """(pk, reference_id, frame_index, dihedral, quality, timestamp_s, shared, pdq) rows."""
        if not rows:
            empty = np.empty(0, dtype=np.int64)
            return cls(
                empty,
                empty.copy(),
                empty.copy(),
                empty.copy(),
                empty.copy(),
                np.empty(0, dtype=np.float64),
                np.empty(0, dtype=np.bool_),
                np.empty((0, _WORDS), dtype=np.uint64),
            )
        columns = list(zip(*rows, strict=True))
        pdqs = columns[7]
        for value in pdqs:
            if len(value) != HASH_BYTES:
                raise ValueError(f"PDQ hash must be {HASH_BYTES} bytes, got {len(value)}")
        words = np.frombuffer(b"".join(pdqs), dtype=np.uint64).reshape(-1, _WORDS)
        return cls(
            np.asarray(columns[0], dtype=np.int64),
            np.asarray(columns[1], dtype=np.int64),
            np.asarray(columns[2], dtype=np.int64),
            np.asarray(columns[3], dtype=np.int64),
            np.asarray(columns[4], dtype=np.int64),
            np.asarray(columns[5], dtype=np.float64),
            np.asarray(columns[6], dtype=np.bool_),
            words.copy(),
        )


_FIELDS = ("pk", "reference_id", "frame_index", "dihedral", "quality", "timestamp_s", "shared")


def load_reference_hashes() -> ReferenceHashes:
    """All reference frame hashes, ordered by pk (quality filtering is the caller's mask).

    Each batch of `_CHUNK` rows becomes numpy arrays at once and its Python objects are dropped,
    so the peak is the finished arrays plus one batch rather than a full Python column per field:
    614,360 rows used to build 7 lists of 614,360 objects plus 614,360 `bytes` before any array
    existed, and those objects — not the ~50 MB of arrays — were the peak.

    Measured read-only on the live index, 614,360 hashes, same 117 MB baseline process
    (2026-09-17): peak RSS +217 MB in 1.94 s before, **+112 MB in 1.64 s** after, with all eight
    columns compared against the database row by row. This is the largest single allocation of a
    `match_references` run, and it grows with every customer, so it decides whether a denser
    reference grid (2-4 fps, ADR 0022) fits in the development box's spare gigabyte.
    """
    ints: list[list[NDArray[np.int64]]] = [[], [], [], [], []]
    stamps: list[NDArray[np.float64]] = []
    flags: list[NDArray[np.bool_]] = []
    word_batches: list[NDArray[np.uint64]] = []
    batch: list[tuple[int, int, int, int, int, float, bool, bytes]] = []

    def take() -> None:
        pdqs = [bytes(row[7]) for row in batch]
        for value in pdqs:
            if len(value) != HASH_BYTES:
                raise ValueError(f"PDQ hash must be {HASH_BYTES} bytes, got {len(value)}")
        word_batches.append(
            np.frombuffer(b"".join(pdqs), dtype=np.uint64).reshape(-1, _WORDS).copy()
        )
        for position, column in enumerate(ints):
            column.append(np.fromiter((row[position] for row in batch), dtype=np.int64))
        stamps.append(np.fromiter((row[5] for row in batch), dtype=np.float64))
        flags.append(np.fromiter((row[6] for row in batch), dtype=np.bool_))
        batch.clear()

    for row in (
        ReferenceFrameHash.objects.order_by("pk")
        .values_list(*_FIELDS, "pdq")
        .iterator(chunk_size=_CHUNK)
    ):
        batch.append(row)
        if len(batch) >= _CHUNK:
            take()
    if batch:
        take()
    if not word_batches:
        return ReferenceHashes.from_rows([])
    return ReferenceHashes(
        np.concatenate(ints[0]),
        np.concatenate(ints[1]),
        np.concatenate(ints[2]),
        np.concatenate(ints[3]),
        np.concatenate(ints[4]),
        np.concatenate(stamps),
        np.concatenate(flags),
        np.concatenate(word_batches),
    )


def signatures(
    hashes: ReferenceHashes, reference_ids: Collection[int], min_quality: int
) -> dict[str, list[int]]:
    """Reference id -> [eligible frame count, last eligible frame id], from the same snapshot the
    scan uses, so a reference imported mid-run can never be recorded as scanned without its frames.
    """
    result = {str(pk): [0, 0] for pk in reference_ids}
    mask = hashes.eligible(min_quality)
    if not mask.any():
        return result
    rids = hashes.reference_id[mask]
    pks = hashes.pk[mask]
    unique, inverse, counts = np.unique(rids, return_inverse=True, return_counts=True)
    last = np.zeros(len(unique), dtype=np.int64)
    np.maximum.at(last, inverse, pks)
    for rid, count, top in zip(unique.tolist(), counts.tolist(), last.tolist(), strict=True):
        result[str(rid)] = [count, top]
    return result
