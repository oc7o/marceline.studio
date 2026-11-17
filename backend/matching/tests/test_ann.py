"""FAISS backends for the Hamming index (ADR 0024).

The contract these tests pin down:
- the exact backends (`numpy`, `flat`) return *identical* results, not merely similar ones;
- every backend, approximate ones included, is re-ranked exactly, so it never reports a pair
  above the threshold (a subset of the exact answer, never a superset);
- a missing FAISS wheel degrades to the numpy scan rather than to wrong answers;
- `add` (incremental matching) equals rebuilding the index from scratch.
"""

import pytest

from matching import ann
from matching.hamming import HashIndex
from matching.pdq import hamming_distance
from matching.tests.hashes import flip_bits, random_hashes

EXACT_BACKENDS = [ann.NUMPY, ann.FLAT]


@pytest.fixture
def corpus() -> tuple[list[int], list[bytes]]:
    """A clustered corpus: near-duplicates at every interesting radius plus noise."""
    bases = random_hashes(40, seed=11)
    hashes = [
        flip_bits(base, n, seed=n + i)
        for i, base in enumerate(bases)
        for n in (0, 5, 20, 31, 36, 44)
    ]
    hashes += random_hashes(200, seed=12)
    return [500 + 3 * i for i in range(len(hashes))], hashes


def brute_force(
    ids: list[int], hashes: list[bytes], query: bytes, limit: int
) -> list[tuple[int, int]]:
    hits = [(i, hamming_distance(h, query)) for i, h in zip(ids, hashes, strict=True)]
    return sorted(((i, d) for i, d in hits if d <= limit), key=lambda hit: (hit[1], hit[0]))


@pytest.mark.parametrize("backend", EXACT_BACKENDS)
@pytest.mark.parametrize("limit", [0, 8, 31, 40])
def test_exact_backends_equal_brute_force(
    corpus: tuple[list[int], list[bytes]], backend: str, limit: int
) -> None:
    ids, hashes = corpus
    index = HashIndex(ids, hashes, backend=backend)
    for query in hashes[:12]:
        assert index.query(query, limit) == brute_force(ids, hashes, query, limit)


def test_flat_equals_numpy_for_every_entry_point(corpus: tuple[list[int], list[bytes]]) -> None:
    ids, hashes = corpus
    exact = HashIndex(ids, hashes, backend=ann.NUMPY)
    flat = HashIndex(ids, hashes, backend=ann.FLAT)
    queries = hashes[:30]

    assert flat.query_many(queries, 40) == exact.query_many(queries, 40)

    def sorted_pairs(index: HashIndex) -> list[tuple[int, int, int]]:
        positions, found, distances = index.pairs_within(queries, 40)
        return sorted(zip(positions.tolist(), found.tolist(), distances.tolist(), strict=True))

    assert sorted_pairs(flat) == sorted_pairs(exact)

    def sorted_neighbors(index: HashIndex) -> list[tuple[int, int, int]]:
        first, second, distances = index.neighbor_pairs(31)
        return sorted(zip(first.tolist(), second.tolist(), distances.tolist(), strict=True))

    assert sorted_neighbors(flat) == sorted_neighbors(exact)


@pytest.mark.parametrize("backend", [ann.FLAT, ann.IVF])
def test_reranking_never_reports_a_pair_above_the_threshold(
    corpus: tuple[list[int], list[bytes]], backend: str
) -> None:
    """Candidates are re-scored exactly, so results are a subset of the exact answer.

    An approximate backend may miss a pair; it may never invent one or misreport a distance.
    """
    ids, hashes = corpus
    index = HashIndex(ids, hashes, backend=backend, nlist=8, nprobe=8)
    by_id = dict(zip(ids, hashes, strict=True))
    for query in hashes[:20]:
        found = index.query(query, 31)
        exact = dict(brute_force(ids, hashes, query, 31))
        for found_id, distance in found:
            assert distance == hamming_distance(by_id[found_id], query)
            assert distance <= 31
            assert found_id in exact


def test_ivf_with_full_nprobe_loses_nothing(corpus: tuple[list[int], list[bytes]]) -> None:
    """Probing every cell makes IVF exhaustive, i.e. exact — the knob is recall, not truth."""
    ids, hashes = corpus
    index = HashIndex(ids, hashes, backend=ann.IVF, nlist=4, nprobe=4)
    for query in hashes[:10]:
        assert index.query(query, 31) == brute_force(ids, hashes, query, 31)


def test_missing_faiss_falls_back_to_numpy(
    corpus: tuple[list[int], list[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids, hashes = corpus
    monkeypatch.setattr(ann, "FAISS_AVAILABLE", False)
    index = HashIndex(ids, hashes, backend=ann.FLAT)
    assert index.backend == ann.NUMPY
    assert index.query(hashes[0], 31) == brute_force(ids, hashes, hashes[0], 31)
    assert ann.build(index._words, ann.FLAT) is None


def test_auto_picks_numpy_for_small_and_faiss_above_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ann.resolve_backend(10) == ann.NUMPY
    assert ann.resolve_backend(ann.MIN_FAISS_SIZE - 1) == ann.NUMPY
    assert ann.resolve_backend(ann.MIN_FAISS_SIZE) == ann.FLAT
    monkeypatch.setattr(ann, "FAISS_AVAILABLE", False)
    assert ann.resolve_backend(10_000_000) == ann.NUMPY


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown ANN backend"):
        HashIndex([1], random_hashes(1, seed=3), backend="annoy").query(
            random_hashes(1, seed=3)[0], 31
        )


@pytest.mark.parametrize("backend", EXACT_BACKENDS)
def test_add_equals_rebuilding(corpus: tuple[list[int], list[bytes]], backend: str) -> None:
    """Incremental matching appends new frames instead of rebuilding (ADR 0015)."""
    ids, hashes = corpus
    split = len(ids) // 2
    grown = HashIndex(ids[:split], hashes[:split], backend=backend)
    # Search once first, so the append has to update an index that is already built.
    grown.query(hashes[0], 31)
    grown.add(ids[split:], hashes[split:])
    rebuilt = HashIndex(ids, hashes, backend=backend)

    assert len(grown) == len(rebuilt) == len(ids)
    for query in hashes[:15]:
        assert grown.query(query, 40) == rebuilt.query(query, 40)
        assert grown.query(query, 40) == brute_force(ids, hashes, query, 40)


@pytest.mark.parametrize("backend", EXACT_BACKENDS)
def test_add_to_an_empty_index(backend: str) -> None:
    hashes = random_hashes(6, seed=21)
    index = HashIndex([], [], backend=backend)
    index.add([1, 2, 3, 4, 5, 6], hashes)
    assert index.query(hashes[2], 0) == [(3, 0)]
    index.add([], [])
    assert len(index) == 6


def test_add_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        HashIndex([], []).add([1, 2], random_hashes(1, seed=4))


@pytest.mark.parametrize("backend", EXACT_BACKENDS)
def test_empty_index_and_empty_queries(backend: str) -> None:
    index = HashIndex([], [], backend=backend)
    query = random_hashes(1, seed=5)[0]
    assert index.query(query, 256) == []
    assert index.query_many([query], 256) == [[]]
    assert [len(part) for part in index.pairs_within([query], 40)] == [0, 0, 0]
    assert [len(part) for part in index.neighbor_pairs(40)] == [0, 0, 0]
