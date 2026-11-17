from matching.sprite_grid import (
    MAX_TILES,
    MIN_SHEET_HEIGHT,
    MIN_SHEET_WIDTH,
    GridCandidate,
    candidate_grids,
    detect_grid,
)


def test_detects_a_clean_square_tile_grid() -> None:
    # 5x4 tiles of 100x100 (aspect 1:1): the only factorisation of 500x400 that clears every
    # filter (checked against the codebase's synthetic_image default size below, which is not).
    assert detect_grid(500, 400) == (5, 4)
    assert candidate_grids(500, 400) == [GridCandidate(5, 4)]


def test_detects_a_16_9_tile_grid() -> None:
    # 6x4 tiles of 121x68 (~16:9).
    assert detect_grid(726, 272) == (6, 4)


def test_refuses_a_self_similar_sheet_as_ambiguous() -> None:
    """800x360 is a clean 5x4 grid of 160x90 tiles, AND a clean 10x8 grid of 80x45 tiles at the
    same aspect ratio, AND a clean 25x20 grid of 32x18 tiles — geometry alone cannot choose."""
    candidates = candidate_grids(800, 360)
    assert len(candidates) > 1
    assert detect_grid(800, 360) is None


def test_refuses_a_generic_photo_as_ambiguous() -> None:
    """A non-sprite image (no real grid at all) still divides several plausible ways; refusing is
    correct here too — geometry cannot distinguish "no grid" from "more than one guess"."""
    assert detect_grid(640, 360) is None


def test_refuses_dimensions_with_no_plausible_grid() -> None:
    # Both prime: no divisor pair, however loose the filters.
    assert candidate_grids(577, 331) == []
    assert detect_grid(577, 331) is None


def test_refuses_a_sheet_too_small_to_hold_a_storyboard() -> None:
    assert candidate_grids(MIN_SHEET_WIDTH - 1, 200) == []
    assert candidate_grids(200, MIN_SHEET_HEIGHT - 1) == []
    assert detect_grid(100, 80) is None


def test_candidate_tile_count_stays_bounded() -> None:
    for candidate in candidate_grids(3000, 3000):
        assert candidate.columns * candidate.rows <= MAX_TILES
