"""Letterbox/pillarbox removal before PDQ hashing (docs/matching/black-bars.md).

A site that pads a picture to its own aspect ratio moves the PDQ hash of that picture by 60-100
bits — far past the match threshold of 31 — because PDQ hashes the whole frame and padding rescales
every feature inside it. Trimming the near-uniform dark edge rows and columns first hashes the
picture in its own geometry again, which is the geometry the reference original was hashed in.

Two rules, both measured (`docs/matching/black-bars.md`):

- `Rule.MAX` — a row is bar if its brightest pixel is at most `dark_level`. Cheap and strict; a
  station logo or a subtitle burnt into the bar defeats it.
- `Rule.PERCENTILE` — a row is bar if the `PERCENTILE`th percentile of its *central* columns is at
  most `dark_level`, so a mark in a corner of the bar does not veto the whole bar. ~10x the cost
  of `MAX` and willing to trim a genuinely dark edge of a picture.

Guards, in this order: a bar smaller than `MIN_BAR_FRACTION` of its dimension is noise and is not
trimmed (so an ordinary frame hashes bit-identically — `no-op` in the tests); a bar must end in a
**step** of at least `MIN_STEP` luma, or it is a picture fading to black rather than a bar (without
that guard, trimming the fade costs 44-88 bits on material that matches today —
`docs/matching/black-bars.md` §6); and the kept area must stay at least `MIN_KEEP` of each
dimension, or nothing is trimmed on that axis.

Pure: no Django, no I/O. `matching.media_hash` (which the runtime's hash children import) may use
it, so this module must not grow a Django import.
"""

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray
from PIL import Image

# Luma at or below this counts as black. JPEG ringing and a dithered bar rarely stay under 16;
# 24 of 255 (9.4 %) was the lowest level that still found every real bar in the sample of
# docs/matching/black-bars.md and trimmed no picture edge.
DARK_LEVEL = 24
# Never keep less than half of a dimension: a picture that is mostly dark is not a bar.
MIN_KEEP = 0.5
# Below this share of the dimension a dark edge is noise, not a bar. 1 % is under a pixel at the
# 512-wide grid PDQ hashes on, so trimming it could not change a hash but could change bytes.
MIN_BAR_FRACTION = 0.01
# A bar ends abruptly; a picture fading to black does not. The first kept line must be at least
# this much brighter than `dark_level`, or nothing is trimmed on that side. Measured: without it,
# a frame whose top fades to black is trimmed by 12-32 rows and moves 44-88 bits away from its own
# original — the detector would break matches that work today.
MIN_STEP = 24
# The step is looked for in the first `STEP_WINDOW` lines inside the bar, not in the very first
# one: rescaling a padded picture blends bar and content into one dim transition line (measured
# 33/255 where the content behind it is 168).
STEP_WINDOW = 3
# PERCENTILE rule: share of columns (rows) ignored at each end when judging a row (column), and
# the percentile of what is left. A 10 % corner logo inside a bar sits inside the ignored band.
MARGIN = 0.15
PERCENTILE = 98.0


class Rule(StrEnum):
    MAX = "max"
    PERCENTILE = "percentile"


@dataclass(frozen=True)
class Bars:
    """Rows/columns to trim from each edge."""

    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0

    @property
    def found(self) -> bool:
        return bool(self.top or self.bottom or self.left or self.right)

    @property
    def letterbox(self) -> bool:
        return bool(self.top or self.bottom)

    @property
    def pillarbox(self) -> bool:
        return bool(self.left or self.right)

    def box(self, width: int, height: int) -> tuple[int, int, int, int]:
        """PIL crop box for an image of this size."""
        return (self.left, self.top, width - self.right, height - self.bottom)


NO_BARS = Bars()


def _line_darkness(luma: NDArray[np.uint8], *, rule: Rule, margin: float) -> NDArray[np.float64]:
    """Per row (axis 0) how bright the row is, by this rule's statistic."""
    if rule is Rule.MAX:
        return luma.max(axis=1).astype(np.float64)
    width = luma.shape[1]
    cut = int(width * margin)
    inner = luma[:, cut : width - cut] if width - 2 * cut >= 1 else luma
    return np.percentile(inner, PERCENTILE, axis=1)


def _runs(flags: NDArray[np.bool_]) -> tuple[int, int]:
    """Length of the leading and trailing run of True, or (0, 0) when everything is True."""
    if flags.size == 0 or flags.all():
        return 0, 0
    lit = np.flatnonzero(~flags)
    return int(lit[0]), int(flags.size - 1 - lit[-1])


def _trim(
    stats: NDArray[np.float64],
    *,
    dark_level: int,
    min_keep: float,
    min_bar_fraction: float,
    min_step: int,
) -> tuple[int, int]:
    size = stats.size
    before, after = _runs(stats <= dark_level)
    floor = max(1, round(size * min_bar_fraction))
    step = dark_level + min_step
    if before < floor or stats[before : before + STEP_WINDOW].max(initial=0.0) < step:
        before = 0
    if (
        after < floor
        or stats[max(0, size - after - STEP_WINDOW) : size - after].max(initial=0.0) < step
    ):
        after = 0
    if size - before - after < max(1, round(size * min_keep)):
        return 0, 0
    return before, after


def detect_bars(
    image: Image.Image,
    *,
    rule: Rule = Rule.MAX,
    dark_level: int = DARK_LEVEL,
    min_keep: float = MIN_KEEP,
    min_bar_fraction: float = MIN_BAR_FRACTION,
    min_step: int = MIN_STEP,
    margin: float = MARGIN,
) -> Bars:
    """The dark edge bars of `image`, or `NO_BARS`. Never raises on odd input."""
    if image.width < 2 or image.height < 2:
        return NO_BARS
    luma = np.asarray(image.convert("L"), dtype=np.uint8)
    if luma.ndim != 2:
        return NO_BARS

    def trim(stats: NDArray[np.float64]) -> tuple[int, int]:
        return _trim(
            stats,
            dark_level=dark_level,
            min_keep=min_keep,
            min_bar_fraction=min_bar_fraction,
            min_step=min_step,
        )

    top, bottom = trim(_line_darkness(luma, rule=rule, margin=margin))
    left, right = trim(_line_darkness(np.ascontiguousarray(luma.T), rule=rule, margin=margin))
    return Bars(top=top, bottom=bottom, left=left, right=right)


def crop_bars(
    image: Image.Image,
    *,
    rule: Rule = Rule.MAX,
    dark_level: int = DARK_LEVEL,
    min_keep: float = MIN_KEEP,
    min_bar_fraction: float = MIN_BAR_FRACTION,
    min_step: int = MIN_STEP,
    margin: float = MARGIN,
) -> tuple[Image.Image, Bars]:
    """`image` without its dark edge bars, and what was trimmed.

    Without bars the **same object** comes back, so hashing is bit-identical to hashing the
    original: switching the setting on can only change frames that really carry bars.
    """
    bars = detect_bars(
        image,
        rule=rule,
        dark_level=dark_level,
        min_keep=min_keep,
        min_bar_fraction=min_bar_fraction,
        min_step=min_step,
        margin=margin,
    )
    if not bars.found:
        return image, bars
    return image.crop(bars.box(image.width, image.height)), bars
