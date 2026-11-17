"""Nearest-neighbour search over 512-bit embedding sketches (ADR 0037, on top of ADR 0024).

The same engine as the PDQ index — FAISS's `IndexBinaryFlat`, exact by construction, with a numpy
XOR/popcount fallback when the wheel is missing — but a class of its own for two reasons:

- a sketch is 512 bits, not 256, and `matching.hamming.HashIndex` is the PDQ index down to its
  `(N, 4)` word shape and its `HASH_BYTES` fingerprint in `matching.incremental`. Widening it
  would put the second signal into the hot path of every match run for nothing;
- the query is **top-k**, not a radius. A sketch distance may never be thresholded (an embedding
  has no safe fixed threshold, ADR 0037), so a radius search would be the one operation this
  signal must not offer.

Threads are capped like every other FAISS search here (`matching.ann.search_threads`).
"""

from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

from matching import ann
from matching.embedding import SKETCH_BITS, SKETCH_BYTES, sketches_to_words

_WORDS = SKETCH_BYTES // 8
# Upper bound for the temporary (queries x sketches x words) array of the numpy fallback.
_BATCH_BUDGET_WORDS = 8_000_000


def to_codes(words: NDArray[np.uint64]) -> NDArray[np.uint8]:
    """The (N, 8) uint64 sketch array as the (N, 64) uint8 codes FAISS expects."""
    if len(words) == 0:
        return np.empty((0, SKETCH_BYTES), dtype=np.uint8)
    return np.ascontiguousarray(words).view(np.uint8).reshape(len(words), SKETCH_BYTES)


class SketchIndex:
    """Top-k Hamming neighbours over sketches. Ids are caller-chosen integers."""

    def __init__(self, ids: Sequence[int], sketches: Sequence[bytes]) -> None:
        if len(ids) != len(sketches):
            raise ValueError("ids and sketches must have the same length")
        self._ids = np.asarray(ids, dtype=np.int64)
        self._words = sketches_to_words(sketches)
        # One attribute holding a FAISS index, so annotate the untyped SWIG binding away.
        self._faiss: Any = None
        self._built = False

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> NDArray[np.int64]:
        return self._ids

    def _index(self) -> Any:
        """The FAISS index, built on first search; None when FAISS is unavailable or empty."""
        if not self._built:
            self._built = True
            if ann.FAISS_AVAILABLE and len(self._words):
                import faiss

                index = faiss.IndexBinaryFlat(SKETCH_BITS)
                index.add(to_codes(self._words))
                self._faiss = index
        return self._faiss

    def nearest(
        self, sketches: Sequence[bytes] | NDArray[np.uint64], k: int
    ) -> tuple[NDArray[np.int64], NDArray[np.int64]]:
        """(ids, distances) of the k nearest indexed sketches per query, nearest first.

        Both arrays are (queries, min(k, len(self))). Ties break on the smaller id, so the result
        is deterministic whichever backend ran.
        """
        queries = sketches if isinstance(sketches, np.ndarray) else sketches_to_words(sketches)
        width = min(max(k, 0), len(self))
        if width == 0 or len(queries) == 0:
            shape = (len(queries), 0)
            return np.empty(shape, dtype=np.int64), np.empty(shape, dtype=np.int64)
        index = self._index()
        if index is not None:
            import faiss

            faiss.omp_set_num_threads(ann.search_threads(len(queries)))
            ids = np.empty((len(queries), width), dtype=np.int64)
            distances = np.empty((len(queries), width), dtype=np.int64)
            for start in range(0, len(queries), ann.QUERY_BATCH):
                chunk = to_codes(queries[start : start + ann.QUERY_BATCH])
                found, positions = index.search(chunk, width)
                stop = start + len(chunk)
                # Re-rank exactly from our own copy: FAISS proposes, numpy disposes (ADR 0024).
                exact = np.bitwise_count(
                    queries[start:stop][:, None, :] ^ self._words[positions]
                ).sum(axis=2, dtype=np.int64)
                order = np.lexsort((self._ids[positions], exact), axis=1)
                rows = np.arange(len(chunk))[:, None]
                ids[start:stop] = self._ids[positions][rows, order]
                distances[start:stop] = exact[rows, order]
                del found
            return ids, distances
        return self._numpy_nearest(queries, width)

    def _numpy_nearest(
        self, queries: NDArray[np.uint64], width: int
    ) -> tuple[NDArray[np.int64], NDArray[np.int64]]:
        ids = np.empty((len(queries), width), dtype=np.int64)
        distances = np.empty((len(queries), width), dtype=np.int64)
        batch = max(1, _BATCH_BUDGET_WORDS // (max(len(self), 1) * _WORDS))
        for start in range(0, len(queries), batch):
            chunk = queries[start : start + batch]
            matrix = np.bitwise_count(chunk[:, None, :] ^ self._words[None, :, :]).sum(
                axis=2, dtype=np.int64
            )
            order = np.lexsort((np.broadcast_to(self._ids, matrix.shape), matrix), axis=1)[
                :, :width
            ]
            rows = np.arange(len(chunk))[:, None]
            stop = start + len(chunk)
            ids[start:stop] = self._ids[order]
            distances[start:stop] = matrix[rows, order]
        return ids, distances
