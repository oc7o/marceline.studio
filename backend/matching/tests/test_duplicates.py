from matching.duplicates import DuplicateCandidate, find_duplicate_pages, pair_stats
from matching.tests.hashes import flip_bits, random_hashes


def test_ranks_pairs_by_score() -> None:
    h1, h2, h3, r1, r2, r3 = random_hashes(6, seed=10)
    pages = {
        1: [h1, h2, h3],
        2: [flip_bits(h1, 4), flip_bits(h2, 8)],
        3: [flip_bits(h3, 6), r1, r2],
        4: [r3],
    }
    assert find_duplicate_pages(pages) == [
        DuplicateCandidate(page_a=1, page_b=2, matched_frames=2, min_distance=4, score=1.0),
        DuplicateCandidate(page_a=1, page_b=3, matched_frames=1, min_distance=6, score=1 / 3),
    ]
    assert [(c.page_a, c.page_b) for c in find_duplicate_pages(pages, min_score=0.5)] == [(1, 2)]
    assert find_duplicate_pages(pages, max_distance=3) == []


def test_frames_of_the_same_page_never_pair() -> None:
    (x,) = random_hashes(1, seed=11)
    assert find_duplicate_pages({5: [x, flip_bits(x, 1)]}) == []


def test_page_ids_are_ordered_regardless_of_input_order() -> None:
    (x,) = random_hashes(1, seed=12)
    [candidate] = find_duplicate_pages({9: [x], 3: [flip_bits(x, 2)]})
    assert (candidate.page_a, candidate.page_b) == (3, 9)


def test_many_similar_frames_on_one_side_keep_score_at_most_one() -> None:
    (x,) = random_hashes(1, seed=13)
    pages = {1: [x], 2: [x, flip_bits(x, 2), flip_bits(x, 3, seed=1)]}
    [candidate] = find_duplicate_pages(pages)
    assert (candidate.matched_frames, candidate.score, candidate.min_distance) == (1, 1.0, 0)


def test_pair_stats() -> None:
    h1, h2, r1 = random_hashes(3, seed=14)
    stats = pair_stats([h1, h2], [flip_bits(h1, 5), r1])
    assert (stats.matched_frames, stats.min_distance, stats.score) == (1, 5, 0.5)
    empty = pair_stats([], [h1])
    assert (empty.matched_frames, empty.min_distance, empty.score) == (0, None, 0.0)
