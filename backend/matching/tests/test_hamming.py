import pytest

import matching.hamming
from matching.hamming import HashIndex, pairwise_distances
from matching.pdq import hamming_distance
from matching.tests.hashes import flip_bits, random_hashes


def brute_force(
    ids: list[int], hashes: list[bytes], query: bytes, limit: int
) -> list[tuple[int, int]]:
    hits = [(i, hamming_distance(h, query)) for i, h in zip(ids, hashes, strict=True)]
    return sorted(((i, d) for i, d in hits if d <= limit), key=lambda hit: (hit[1], hit[0]))


@pytest.fixture
def corpus() -> tuple[list[int], list[bytes], bytes]:
    target = random_hashes(1, seed=1)[0]
    hashes = random_hashes(300, seed=2) + [flip_bits(target, n, seed=n) for n in (0, 3, 17, 31, 32)]
    ids = [1000 + 7 * i for i in range(len(hashes))]
    return ids, hashes, target


@pytest.mark.parametrize("limit", [0, 17, 31, 128, 256])
def test_query_matches_brute_force(
    corpus: tuple[list[int], list[bytes], bytes], limit: int
) -> None:
    ids, hashes, target = corpus
    assert HashIndex(ids, hashes).query(target, limit) == brute_force(ids, hashes, target, limit)


def test_near_copies_found_in_order(corpus: tuple[list[int], list[bytes], bytes]) -> None:
    ids, hashes, target = corpus
    assert [d for _, d in HashIndex(ids, hashes).query(target, 31)] == [0, 3, 17, 31]


def test_query_many_equals_query_across_batches(
    corpus: tuple[list[int], list[bytes], bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids, hashes, _ = corpus
    index = HashIndex(ids, hashes)
    queries = hashes[:25]
    expected = [index.query(q, 40) for q in queries]
    monkeypatch.setattr(matching.hamming, "_BATCH_BUDGET_WORDS", 4 * len(hashes) * 3)
    assert index.query_many(queries, 40) == expected


def test_empty_index() -> None:
    index = HashIndex([], [])
    query = random_hashes(1, seed=3)[0]
    assert len(index) == 0
    assert index.query(query, 256) == []
    assert index.query_many([query, query], 256) == [[], []]


def test_from_pairs() -> None:
    hashes = random_hashes(3, seed=4)
    index = HashIndex.from_pairs(zip([5, 6, 7], hashes, strict=True))
    assert index.query(hashes[1], 0) == [(6, 0)]


def test_neighbor_pairs_match_brute_force(monkeypatch: pytest.MonkeyPatch) -> None:
    base = random_hashes(4, seed=8)
    hashes = [flip_bits(value, n, seed=n) for value in base for n in (0, 2, 5, 9)]
    ids = [100 + i for i in range(len(hashes))]
    expected = sorted(
        (ids[i], ids[j], hamming_distance(hashes[i], hashes[j]))
        for i in range(len(hashes))
        for j in range(i + 1, len(hashes))
        if hamming_distance(hashes[i], hashes[j]) <= 8
    )
    # A tiny batch budget exercises the upper-triangle batching.
    monkeypatch.setattr(matching.hamming, "_BATCH_BUDGET_WORDS", 16 * 4 * 3)
    firsts, seconds, distances = HashIndex(ids, hashes).neighbor_pairs(8)
    pairs = zip(firsts.tolist(), seconds.tolist(), distances.tolist(), strict=True)
    assert sorted(pairs) == expected
    assert [len(a) for a in HashIndex([], []).neighbor_pairs(8)] == [0, 0, 0]


def test_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        HashIndex([1], [b"short"])
    with pytest.raises(ValueError, match="same length"):
        HashIndex([1, 2], random_hashes(1, seed=5))


def test_pairwise_distances() -> None:
    first, second = random_hashes(3, seed=6), random_hashes(2, seed=7)
    matrix = pairwise_distances(first, second)
    assert matrix.shape == (3, 2)
    assert matrix.tolist() == [[hamming_distance(a, b) for b in second] for a in first]
