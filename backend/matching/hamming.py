"""In-memory Hamming search over 256-bit PDQ hashes.

Hashes are held as an (N, 4) uint64 array. The exact scan is one XOR plus popcount
(`np.bitwise_count`) over the whole array; that is what small indexes use.

Above `ann.MIN_FAISS_SIZE` the search is delegated to a FAISS binary index
(`matching.ann`, ADR 0024). The default backend is `IndexBinaryFlat`, which is **exact** — it
returns the same pairs as the numpy scan, just with SIMD popcount and threading, ~30x faster at
match-run scale. The approximate `IndexBinaryIVF` backend exists but is opt-in and lossy; see
ADR 0024 for the measured recall.

Candidates coming back from FAISS are always re-ranked with the exact distance from this class's
own uint64 copy of the hashes, so a reported distance is never an approximation.

Rough cost on the dev machine with 432k hashes: one query ~22 ms exact-numpy, ~0.75 ms
exact-FAISS. Building a FAISS flat index over them takes ~0.01 s.
"""

from collections.abc import Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

from matching import ann

HASH_BYTES = 32
_WORDS = HASH_BYTES // 8
# Upper bound for the temporary (queries x hashes x words) array in query_many.
_BATCH_BUDGET_WORDS = 8_000_000


def to_words(hashes: Sequence[bytes]) -> NDArray[np.uint64]:
    for value in hashes:
        if len(value) != HASH_BYTES:
            raise ValueError(f"PDQ hash must be {HASH_BYTES} bytes, got {len(value)}")
    if not hashes:
        return np.empty((0, _WORDS), dtype=np.uint64)
    buffer = b"".join(bytes(value) for value in hashes)
    return np.frombuffer(buffer, dtype=np.uint64).reshape(-1, _WORDS)


def pairwise_distances(first: Sequence[bytes], second: Sequence[bytes]) -> NDArray[np.int64]:
    """Distance matrix of shape (len(first), len(second))."""
    a = to_words(first)
    b = to_words(second)
    xor = a[:, None, :] ^ b[None, :, :]
    return np.bitwise_count(xor).sum(axis=2, dtype=np.int64)


class HashIndex:
    """Hamming index over PDQ hashes. Ids are caller-chosen integers (e.g. FrameHash pk).

    `backend` overrides the choice made by `matching.ann.resolve_backend` (`auto` by default,
    which is the exact numpy scan for small indexes and the exact FAISS flat index above
    `ann.MIN_FAISS_SIZE`). Results are identical whichever exact backend runs; only the
    opt-in `ivf` backend is approximate.
    """

    def __init__(
        self,
        ids: Sequence[int],
        hashes: Sequence[bytes],
        *,
        backend: str | None = None,
        nlist: int = ann.IVF_NLIST,
        nprobe: int = ann.IVF_NPROBE,
    ) -> None:
        if len(ids) != len(hashes):
            raise ValueError("ids and hashes must have the same length")
        self._ids = np.asarray(ids, dtype=np.int64)
        self._words = to_words(hashes)
        self._requested = backend
        self._nlist = nlist
        self._nprobe = nprobe
        self._faiss: ann.FaissIndex | None = None
        self._faiss_built = False

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[int, bytes]],
        *,
        backend: str | None = None,
        nlist: int = ann.IVF_NLIST,
        nprobe: int = ann.IVF_NPROBE,
    ) -> "HashIndex":
        items = list(pairs)
        return cls(
            [item[0] for item in items],
            [item[1] for item in items],
            backend=backend,
            nlist=nlist,
            nprobe=nprobe,
        )

    @classmethod
    def from_words(
        cls, ids: NDArray[np.int64], words: NDArray[np.uint64], *, backend: str | None = None
    ) -> "HashIndex":
        """An index over an already packed (N, 4) uint64 hash array (no bytes round trip).

        Used by matching runs that load reference hashes once as arrays (RE3); the arrays are
        referenced, not copied, so callers must not mutate them afterwards.
        """
        if words.ndim != 2 or words.shape[1] != _WORDS or words.dtype != np.uint64:
            raise ValueError("words must be an (N, 4) uint64 array")
        if len(ids) != len(words):
            raise ValueError("ids and hashes must have the same length")
        index = cls([], [], backend=backend)
        index._ids = np.asarray(ids, dtype=np.int64)
        index._words = words
        return index

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def backend(self) -> str:
        """The backend actually in use for this index."""
        return ann.resolve_backend(len(self), self._requested)

    def _index(self) -> ann.FaissIndex | None:
        """The FAISS index, built on first search (None for the numpy backend)."""
        if not self._faiss_built:
            self._faiss = ann.build(
                self._words, self.backend, nlist=self._nlist, nprobe=self._nprobe
            )
            self._faiss_built = True
        return self._faiss

    def add(self, ids: Sequence[int], hashes: Sequence[bytes]) -> None:
        """Append hashes, keeping any built FAISS index (incremental matching, ADR 0015).

        Appending never invalidates the index: FAISS binary indexes support `add`, and an IVF
        index keeps the centroids it was trained on.
        """
        if len(ids) != len(hashes):
            raise ValueError("ids and hashes must have the same length")
        if not len(ids):
            return
        words = to_words(hashes)
        self._ids = np.concatenate([self._ids, np.asarray(ids, dtype=np.int64)])
        self._words = np.vstack([self._words, words]) if len(self._words) else words
        if self._faiss_built and self._faiss is not None:
            self._faiss.add(ann.to_codes(words))

    def distances(self, hash_: bytes) -> NDArray[np.int64]:
        """Distance from `hash_` to every indexed hash, in insertion order.

        Always the full exact scan: this returns a distance per indexed hash, so there is
        nothing for a neighbour index to prune.
        """
        query = to_words([hash_])
        return np.bitwise_count(self._words ^ query).sum(axis=1, dtype=np.int64)

    def _search(
        self, queries: NDArray[np.uint64], max_distance: int
    ) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.int64]] | None:
        """FAISS candidate generation plus exact re-ranking, or None on the numpy backend.

        Returns (query positions, index positions, exact distances), already filtered to
        `max_distance` by the exact distance rather than by whatever FAISS reported.
        """
        index = self._index()
        if index is None or len(queries) == 0:
            return None
        rows, cols = index.range_search(ann.to_codes(queries), max_distance)
        exact = ann.exact_distances(self._words, queries, rows, cols)
        keep = exact <= max_distance
        return rows[keep], cols[keep], exact[keep]

    def query(self, hash_: bytes, max_distance: int) -> list[tuple[int, int]]:
        """(id, distance) for all hashes within `max_distance`, nearest first."""
        found = self._search(to_words([hash_]), max_distance)
        if found is None:
            return self._select(self.distances(hash_), max_distance)
        _, cols, exact = found
        order = np.lexsort((self._ids[cols], exact))
        return [(int(self._ids[cols[i]]), int(exact[i])) for i in order]

    def query_many(self, hashes: Sequence[bytes], max_distance: int) -> list[list[tuple[int, int]]]:
        """Like `query` for each hash, vectorised in memory-bounded batches."""
        queries = to_words(hashes)
        if len(self) == 0:
            return [[] for _ in range(len(queries))]
        found = self._search(queries, max_distance)
        if found is not None:
            rows, cols, exact = found
            results: list[list[tuple[int, int]]] = [[] for _ in range(len(queries))]
            for row, col, distance in zip(
                rows.tolist(), cols.tolist(), exact.tolist(), strict=True
            ):
                results[row].append((int(self._ids[col]), int(distance)))
            return [sorted(hits, key=lambda hit: (hit[1], hit[0])) for hits in results]
        batch = max(1, _BATCH_BUDGET_WORDS // (len(self) * _WORDS))
        results = []
        for start in range(0, len(queries), batch):
            chunk = queries[start : start + batch]
            xor = chunk[:, None, :] ^ self._words[None, :, :]
            matrix = np.bitwise_count(xor).sum(axis=2, dtype=np.int64)
            results.extend(self._select(row, max_distance) for row in matrix)
        return results

    def pairs_within(
        self, hashes: Sequence[bytes] | NDArray[np.uint64], max_distance: int
    ) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.int64]]:
        """(query positions, ids, distances) of every query/index pair within `max_distance`.

        Same work as `query_many`, but vectorised end to end (no per-query Python loop), so
        sparse hits over large scans cost only the XOR/popcount. `hashes` may be bytes or an
        (N, 4) uint64 word array.
        """
        queries = hashes if isinstance(hashes, np.ndarray) else to_words(hashes)
        empty = np.empty(0, dtype=np.int64)
        if len(self) == 0 or len(queries) == 0:
            return empty, empty.copy(), empty.copy()
        found = self._search(queries, max_distance)
        if found is not None:
            rows, cols, exact = found
            return rows, self._ids[cols], exact
        positions: list[NDArray[np.int64]] = []
        ids: list[NDArray[np.int64]] = []
        distances: list[NDArray[np.int64]] = []
        batch = max(1, _BATCH_BUDGET_WORDS // (len(self) * _WORDS))
        for start in range(0, len(queries), batch):
            chunk = queries[start : start + batch]
            matrix = np.bitwise_count(chunk[:, None, :] ^ self._words[None, :, :]).sum(
                axis=2, dtype=np.int64
            )
            rows_, cols_ = np.nonzero(matrix <= max_distance)
            positions.append(start + rows_)
            ids.append(self._ids[cols_])
            distances.append(matrix[rows_, cols_])
        return np.concatenate(positions), np.concatenate(ids), np.concatenate(distances)

    def neighbor_pairs(
        self, max_distance: int
    ) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.int64]]:
        """Self-join: (first ids, second ids, distances) of all index pairs within
        `max_distance`, each unordered pair once (first inserted before second).

        Vectorised over the upper triangle in memory-bounded batches; no per-hash Python loop.
        """
        n = len(self)
        if n == 0:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty.copy(), empty.copy()
        found = self._search(self._words, max_distance)
        if found is not None:
            rows, cols, exact = found
            # Positions are insertion order, so this keeps each unordered pair once.
            keep = cols > rows
            return self._ids[rows[keep]], self._ids[cols[keep]], exact[keep]
        firsts: list[NDArray[np.int64]] = []
        seconds: list[NDArray[np.int64]] = []
        distances: list[NDArray[np.int64]] = []
        batch = max(1, _BATCH_BUDGET_WORDS // (max(n, 1) * _WORDS))
        for start in range(0, n, batch):
            chunk = self._words[start : start + batch]
            rest = self._words[start:]
            matrix = np.bitwise_count(chunk[:, None, :] ^ rest[None, :, :]).sum(
                axis=2, dtype=np.int64
            )
            rows_, cols_ = np.nonzero(matrix <= max_distance)
            keep = cols_ > rows_
            rows_, cols_ = rows_[keep], cols_[keep]
            firsts.append(self._ids[start + rows_])
            seconds.append(self._ids[start + cols_])
            distances.append(matrix[rows_, cols_])
        if not firsts:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty.copy(), empty.copy()
        return np.concatenate(firsts), np.concatenate(seconds), np.concatenate(distances)

    def _select(self, distances: NDArray[np.int64], max_distance: int) -> list[tuple[int, int]]:
        hits = np.flatnonzero(distances <= max_distance)
        order = hits[np.lexsort((self._ids[hits], distances[hits]))]
        return [(int(self._ids[i]), int(distances[i])) for i in order]
