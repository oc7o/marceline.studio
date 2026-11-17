"""FAISS binary backends for the 256-bit PDQ Hamming index (ADR 0024).

`matching.hamming.HashIndex` keeps its own exact copy of every hash, so everything here is a
*candidate generator* that the caller re-ranks exactly. Three backends:

- `NUMPY` — no FAISS at all, the original XOR/popcount scan. The default below `MIN_FAISS_SIZE`,
  and whenever FAISS is missing. That threshold is a conservative choice, not a measured
  crossover: once a built index is reused across many queries FAISS wins well below it, but the
  small indexes here (common-frame anchors, reusable groups) are built for a handful of queries,
  where the build has nothing to amortise over. At that size the whole scan costs well under a
  millisecond either way, so the choice is not worth tuning.
- `FLAT` — `IndexBinaryFlat`. Exact by construction (SIMD popcount over the whole index, same
  answers as numpy, verified in the tests), ~30x faster at match-run scale. The default.
- `IVF` — `IndexBinaryIVF`. Approximate: it only visits `nprobe` of `nlist` cells, so it can
  miss a true neighbour. Opt-in, never automatic, because at our scale its k-means training
  costs more than the exact scan it replaces (ADR 0024 has the table).

Radius note: FAISS binary `range_search` is **exclusive** (`d < radius`), so every call passes
`max_distance + 1`.

Query batching is not optional. `range_search` materialises every hit of the whole batch in one
result; an unbatched 110k-query scan is exactly the shape that got the match run OOM-killed on
the 8 GB box. `QUERY_BATCH` bounds it.
"""

import os
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

try:  # pragma: no cover - exercised by the fallback test via monkeypatching
    import faiss

    FAISS_AVAILABLE = True
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore[assignment]
    FAISS_AVAILABLE = False

NUMPY = "numpy"
FLAT = "flat"
IVF = "ivf"
AUTO = "auto"
BACKENDS = (AUTO, NUMPY, FLAT, IVF)

# Below this many hashes the numpy scan wins: the index is small enough that the XOR/popcount
# runs in a few milliseconds and FAISS's setup is pure overhead.
MIN_FAISS_SIZE = 4096
# Queries per `range_search` call, to bound the result buffer (see module docstring).
QUERY_BATCH = 8192
# FAISS OpenMP threads for a search (RE5). The dev box has 4 cores shared with crawlers, media
# workers and Postgres; FAISS's default of every core starves them during a match run. Override
# with the `MATCHING_FAISS_THREADS` Django setting or environment variable.
DEFAULT_THREADS = 2
# A batch this small runs on one thread: OpenMP start-up costs more than the scan itself
# (a single query on 580k hashes measured 22 ms threaded vs under 1 ms flat, ADR 0024).
SMALL_BATCH = 64
# IVF defaults, used only when a caller explicitly asks for the approximate backend.
IVF_NLIST = 1024
IVF_NPROBE = 32
# FAISS refuses to train IVF with fewer than this many points per centroid.
IVF_MIN_POINTS_PER_CENTROID = 39


def resolve_backend(size: int, requested: str | None = None) -> str:
    """Which backend to use for an index of `size` hashes.

    `requested` comes from the caller or the `MATCHING_ANN_BACKEND` Django setting; `auto`
    (the default) picks numpy for small inputs and the exact FAISS index above that.
    """
    choice = requested or _setting("MATCHING_ANN_BACKEND", AUTO)
    if choice not in BACKENDS:
        raise ValueError(f"Unknown ANN backend {choice!r}; expected one of {BACKENDS}")
    if choice == AUTO:
        if not FAISS_AVAILABLE or size < MIN_FAISS_SIZE:
            return NUMPY
        return FLAT
    if choice != NUMPY and not FAISS_AVAILABLE:
        # A missing wheel must degrade to correct-but-slower, never to wrong answers.
        return NUMPY
    return choice


def _setting(name: str, default: object) -> str:
    """Read a Django setting without requiring Django to be configured."""
    try:
        from django.conf import settings

        return str(getattr(settings, name, default))
    except Exception:
        return str(default)


def search_threads(queries: int) -> int:
    """OpenMP threads for a `range_search` over `queries` hashes (RE5)."""
    if queries < SMALL_BATCH:
        return 1
    raw = _setting("MATCHING_FAISS_THREADS", os.environ.get("MATCHING_FAISS_THREADS", ""))
    try:
        threads = int(raw)
    except ValueError:
        threads = DEFAULT_THREADS
    return max(1, threads)


class FaissIndex:
    """A FAISS binary index over (N, 32) uint8 PDQ hashes.

    Holds no ids: it works in *positions*, exactly like the numpy path, and the caller maps
    positions back to its own ids. That keeps `HashIndex` the single owner of identity.
    """

    def __init__(
        self,
        codes: NDArray[np.uint8],
        kind: str,
        *,
        nlist: int = IVF_NLIST,
        nprobe: int = IVF_NPROBE,
    ) -> None:
        if not FAISS_AVAILABLE:  # pragma: no cover
            raise RuntimeError("faiss is not installed")
        self.kind = kind
        self.approximate = kind == IVF
        codes = np.ascontiguousarray(codes, dtype=np.uint8)
        # One attribute holding either FAISS index type, so annotate the union away.
        self._index: Any
        if kind == FLAT:
            self._index = faiss.IndexBinaryFlat(256)
        elif kind == IVF:
            # Never train more centroids than FAISS will accept for this many points.
            cells = max(1, min(nlist, max(1, len(codes) // IVF_MIN_POINTS_PER_CENTROID)))
            self._index = faiss.IndexBinaryIVF(faiss.IndexBinaryFlat(256), 256, cells)
            self._index.train(codes)
            self._index.nprobe = min(nprobe, cells)
        else:  # pragma: no cover - resolve_backend never yields another kind
            raise ValueError(f"cannot build a FAISS index of kind {kind!r}")
        if len(codes):
            self._index.add(codes)

    def add(self, codes: NDArray[np.uint8]) -> None:
        """Append hashes without rebuilding (incremental matching, ADR 0015)."""
        if len(codes):
            self._index.add(np.ascontiguousarray(codes, dtype=np.uint8))

    def range_search(
        self, queries: NDArray[np.uint8], max_distance: int
    ) -> tuple[NDArray[np.int64], NDArray[np.int64]]:
        """(query positions, index positions) of every candidate within `max_distance`.

        `queries` must already be (N, 32) uint8 codes (`to_codes`): coercing a uint64 word
        array here would cast the values instead of viewing the bytes.

        Batched so the result buffer stays bounded. Distances are recomputed exactly by the
        caller, so the ones FAISS returns are deliberately dropped here.
        """
        rows: list[NDArray[np.int64]] = []
        cols: list[NDArray[np.int64]] = []
        queries = np.ascontiguousarray(queries)
        if queries.dtype != np.uint8 or queries.ndim != 2 or queries.shape[1] != 32:
            raise ValueError("queries must be an (N, 32) uint8 array of PDQ codes")
        faiss.omp_set_num_threads(search_threads(len(queries)))
        for start in range(0, len(queries), QUERY_BATCH):
            chunk = queries[start : start + QUERY_BATCH]
            # FAISS binary range search is exclusive: d < radius.
            lims, _, ids = self._index.range_search(chunk, max_distance + 1)
            # FAISS returns uint64 limits; numpy will not cast those to int64 implicitly.
            counts = np.diff(np.asarray(lims, dtype=np.int64))
            rows.append(start + np.repeat(np.arange(len(chunk), dtype=np.int64), counts))
            cols.append(np.asarray(ids, dtype=np.int64))
        if not rows:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty.copy()
        return np.concatenate(rows), np.concatenate(cols)


def to_codes(words: NDArray[np.uint64]) -> NDArray[np.uint8]:
    """View the (N, 4) uint64 hash array as the (N, 32) uint8 codes FAISS expects.

    Same bytes in the same order, so FAISS and numpy compute identical Hamming distances.
    """
    if len(words) == 0:
        return np.empty((0, 32), dtype=np.uint8)
    return np.ascontiguousarray(words).view(np.uint8).reshape(len(words), 32)


def build(
    words: NDArray[np.uint64], backend: str, *, nlist: int = IVF_NLIST, nprobe: int = IVF_NPROBE
) -> FaissIndex | None:
    """A FAISS index for `words`, or None when the numpy path should be used."""
    if backend == NUMPY or not FAISS_AVAILABLE or len(words) == 0:
        return None
    return FaissIndex(to_codes(words), backend, nlist=nlist, nprobe=nprobe)


def exact_distances(
    words: NDArray[np.uint64],
    queries: NDArray[np.uint64],
    rows: NDArray[np.int64],
    cols: NDArray[np.int64],
) -> NDArray[np.int64]:
    """Exact Hamming distance for each (query row, index column) candidate pair.

    This is the re-ranking step: FAISS proposes, numpy disposes. Cheap because it touches only
    the candidates, not the whole index.
    """
    if len(rows) == 0:
        return np.empty(0, dtype=np.int64)
    return np.bitwise_count(queries[rows] ^ words[cols]).sum(axis=1, dtype=np.int64)


def sequence_to_words(hashes: Sequence[bytes], hash_bytes: int = 32) -> NDArray[np.uint64]:
    """Bytes -> (N, 4) uint64, shared by the index and the query side."""
    if not hashes:
        return np.empty((0, hash_bytes // 8), dtype=np.uint64)
    return np.frombuffer(b"".join(bytes(v) for v in hashes), dtype=np.uint64).reshape(
        -1, hash_bytes // 8
    )
