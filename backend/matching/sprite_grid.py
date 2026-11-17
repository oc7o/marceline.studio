"""Detect a sprite sheet's tile grid from its own geometry — without Django (issue #14).

Fallback for when no adapter declared the grid (the exact route: an adapter that read it off the
site's own markup, a JSON config or a WebVTT thumbnail track's `#xywh=` cues, stores it on
`index.Media.sprite_columns`/`sprite_rows` and this module is never consulted for that media). A
real generator tiles a fixed-size crop of the video's own frame across a regular grid with no
partial tile at either edge, so `detect_grid` looks for (columns, rows) that:
  - divide the sheet's width and height exactly (a slack border would mean the guess is wrong),
  - produce a tile whose aspect ratio is close to a handful of common video aspect ratios,
  - produce a tile pixel size in the range real storyboard generators actually use.

**Geometry alone cannot always tell.** A sheet that is a clean 5x4 grid of 160x90 tiles is, by
construction, *also* a clean 10x8 grid of 80x45 tiles at exactly the same aspect ratio — every
common factor of both dimensions is another candidate at the same ratio, just finer. `detect_grid`
therefore refuses (returns `None`) whenever more than one candidate survives every filter, exactly
as it refuses when none does. A wrong grid hashes nonsense tiles that pollute the index forever
(CLAUDE.md "Hard rules"); a sprite left unsplit is only ever the coverage we already had.
"""

from dataclasses import dataclass

# Common source/preview aspect ratios (width:height); a tile within TOLERANCE of any of these is
# plausible. Portrait ratios cover vertically shot mobile source video.
_CANONICAL_RATIOS = (16 / 9, 4 / 3, 3 / 2, 1.0, 9 / 16, 3 / 4, 2 / 3)
_TOLERANCE = 0.02  # relative difference from a canonical ratio

# Real storyboard/VTT generators (ffmpeg, video.js, JW Player, Plyr) commonly emit tiles in this
# pixel range. The lower bound keeps a single full-frame image (already handled elsewhere, and far
# too coarse to be a storyboard tile) out of the search; the upper bound keeps the search small and
# rejects an accidental "grid" that is really just one or two oversized tiles.
MIN_TILE_WIDTH = 60
MAX_TILE_WIDTH = 240
MIN_TILE_HEIGHT = 34
MAX_TILE_HEIGHT = 240
MIN_TILES = 6
MAX_TILES = 600
MAX_COLUMNS = 64
MAX_ROWS = 64
# A sheet smaller than this cannot plausibly hold a real storyboard grid at the tile sizes above.
MIN_SHEET_WIDTH = 200
MIN_SHEET_HEIGHT = 120


@dataclass(frozen=True)
class GridCandidate:
    columns: int
    rows: int


def _plausible_ratio(tile_width: int, tile_height: int) -> bool:
    ratio = tile_width / tile_height
    return any(abs(ratio - canon) / canon <= _TOLERANCE for canon in _CANONICAL_RATIOS)


def candidate_grids(width: int, height: int) -> list[GridCandidate]:
    """Every (columns, rows) that exactly tiles `width` x `height` at a plausible size and ratio.

    Exposed mainly for tests and diagnostics; `detect_grid` is what callers should use.
    """
    if width < MIN_SHEET_WIDTH or height < MIN_SHEET_HEIGHT:
        return []
    candidates = []
    for cols in range(1, min(width, MAX_COLUMNS) + 1):
        if width % cols:
            continue
        tile_width = width // cols
        if not (MIN_TILE_WIDTH <= tile_width <= MAX_TILE_WIDTH):
            continue
        for rows in range(1, min(height, MAX_ROWS) + 1):
            if height % rows:
                continue
            tile_height = height // rows
            if not (MIN_TILE_HEIGHT <= tile_height <= MAX_TILE_HEIGHT):
                continue
            if not (MIN_TILES <= cols * rows <= MAX_TILES):
                continue
            if _plausible_ratio(tile_width, tile_height):
                candidates.append(GridCandidate(cols, rows))
    return candidates


def detect_grid(width: int, height: int) -> tuple[int, int] | None:
    """The sheet's (columns, rows), or `None` when geometry alone cannot say confidently.

    `None` on zero matches (nothing plausible) and on more than one (ambiguous — most often a
    self-similar sheet matching several scales, see the module docstring): refusing in both cases
    is the point, not a gap to close.
    """
    candidates = candidate_grids(width, height)
    if len(candidates) != 1:
        return None
    grid = candidates[0]
    return grid.columns, grid.rows
