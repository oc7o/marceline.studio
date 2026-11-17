"""Metadata scoring: the third signal, cheap and fully retroactive (ADR 0039).

Pure and deterministic, no ORM, no clock, no I/O. `matching.metadata_candidates` is the database
side; `manage.py metadata_candidates` is the CLI.

**What this is.** PDQ compares pictures. The embedding of ADR 0037 compares pictures too, and it
is **not retroactive**: page pixels are discarded after hashing, so a page indexed before the
switch can never get a sketch. This module compares the *text a site published about a page* with
what we know about an original — duration, cast, title. It therefore works on every page already
in the index, without fetching anything at all.

**What this is not, and the wall that keeps it there.** It is a **candidate generator, never
evidence**. It cannot say "these are the same pictures", only "this page's description is
consistent with that original", and the fields it reads are the pirate site's own text: a re-titled
upload defeats it and a forged duration is cheap to produce. So a row it proposes carries **zero
evidence frames, zero matched frames, no timeline, confidence 0 and band `review`** — the human
review queue and nothing else. Exactly as for ADR 0037, that is enforced three times
independently: a database `CheckConstraint`
(`reference_match_metadata_is_never_evidence`), `matching.confidence.band_for(metadata_only=True)`
and `matching.calibration.apply_bands`, which excludes these rows from re-banding. Keep all three.

**Duration is an indicator, never a hard filter** (standing operator rule: clips are leaks). This
module only ever *admits* a pair on a duration coincidence; it never rejects a pair for having the
wrong duration, and a page shorter than its original earns nothing and loses nothing. Measured on
the labelled pool, "the page is clearly shorter" is true of 18.4 % of confirmed leaks and 42.7 % of
human-rejected pairs — it is not a signal in either direction, which is why it is absent below.

**No biometrics of any kind.** The only person-shaped input is `Page.creators` — a set of primary
keys of staff-reviewed `catalog.Creator` rows the crawler linked from a site's own performer
credit. No picture is opened, no face is detected, nobody is identified or aged. This module
receives integers and strings and returns a reason code.

Every constant below carries the number that chose it. The measurement is
`docs/matching/metadata-scoring.md`, over 1,176,868 (original, active page) pairs and the 188
pairs a human has judged.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from matching.duration import duration_tolerance

# Bump when a rule or a threshold below changes: rows written by another version are re-derived on
# the next run rather than trusted (`matching.metadata_candidates.aim`).
SCORER_VERSION = 1

# Seconds the page's duration may differ from the original's for the cast clause. ±1.5 s (the
# `confidence.EXACT_DURATION_S` used elsewhere) measured 74.1 % precision at 52.6 % recall; ±3 s
# buys 3 points of recall for 33 more candidates over the whole index and measured *better*
# precision (75.0 %); ±10 s loses 7 points of precision for 5 of recall. So: 3.
CAST_DURATION_TOLERANCE_S = 3.0

# Title token overlap (Jaccard) needed for the title clause. At 0.3 the labelled pool gives 26.3 %
# of confirmed leaks against 6.7 % of human-rejected pairs; 0.2 doubles recall but admits 16.7 % of
# the negatives, and 0.5 keeps only 13.2 % of the positives.
TITLE_MIN_JACCARD = 0.3

# Tokens shorter than this are noise ("hd", "4k", a stray digit) and inflate the Jaccard of
# two unrelated titles.
MIN_TOKEN_LENGTH = 3

# There is deliberately **no** minimum token count. A guard of three tokens per side was tried on
# the same population: it dropped 5 of the 148 candidates, and one of the 5 was a human-confirmed
# leak while none of them was a human-rejected pair — it cost recall and bought no precision at
# all. An un-measured heuristic that only loses is not worth the line.

_TOKEN = re.compile(r"[a-z0-9]+")


class MetadataReason(StrEnum):
    """Which coincidence admitted the pair, or why none did.

    The two admitting reasons are deliberately independent, so a page that loses one field can
    still be reached by the other:

    `CAST_AND_DURATION` — the page credits one of the original's performers *and* its duration is
    within `CAST_DURATION_TOLERANCE_S`. The creator link is what makes this affordable: one
    original's cast matches ~581 of 26,747 active pages, a 98 % cut of the search space.

    `DURATION_AND_TITLE` — the duration is within the ordinary `matching.duration` tolerance *and*
    the titles share `TITLE_MIN_JACCARD` of their tokens. This clause needs **no** creator link on
    purpose: 54.8 % of active pages credit nobody, and it is the only part of the rule that can
    ever reach them. Measured 2026-09-18: it costs nothing today (every pair satisfying it already
    has a link), so it is capacity for the case we cannot yet see rather than volume we pay for.
    """

    CAST_AND_DURATION = "cast_and_duration"
    DURATION_AND_TITLE = "duration_and_title"
    NO_SIGNAL = "no_signal"
    NO_PAGE_DURATION = "no_page_duration"
    NO_ORIGINAL_DURATION = "no_original_duration"


#: Reasons that admit a pair as a candidate. Everything else proposes nothing.
ADMITTING_REASONS = frozenset({MetadataReason.CAST_AND_DURATION, MetadataReason.DURATION_AND_TITLE})


def title_tokens(title: str) -> frozenset[str]:
    """Lower-cased alphanumeric tokens of at least `MIN_TOKEN_LENGTH` characters.

    Deliberately crude: no stemming, no stop-word list, no language detection. A site's title is
    untrusted input in an unknown language, and a heavier normaliser would only make the overlap
    harder to reason about without moving the measured numbers.
    """
    return frozenset(t for t in _TOKEN.findall(title.lower()) if len(t) >= MIN_TOKEN_LENGTH)


def token_jaccard(first: Iterable[str], second: Iterable[str]) -> float:
    """Size of the intersection over size of the union; 0.0 when neither side has a token."""
    left, right = frozenset(first), frozenset(second)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


@dataclass(frozen=True)
class MetadataInputs:
    """One (original, page) pair's stored metadata: numbers and strings, never a picture."""

    page_duration_s: float | None
    original_duration_s: float | None
    #: `catalog.Creator` primary keys the crawler linked from the page's own performer credits.
    page_creator_ids: frozenset[int] = frozenset()
    #: Primary keys of every performer of the original, the rights holder included.
    original_creator_ids: frozenset[int] = frozenset()
    page_title_tokens: frozenset[str] = frozenset()
    original_title_tokens: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MetadataSignal:
    """The verdict on one pair. `proposes` is the only thing a caller may act on."""

    reason: MetadataReason
    #: Every component, for the record on the proposed row. Never a score to threshold on.
    signals: dict[str, float | int | bool]

    @property
    def proposes(self) -> bool:
        return self.reason in ADMITTING_REASONS


def score_metadata(inputs: MetadataInputs) -> MetadataSignal:
    """Does this page's metadata suggest it carries this original?

    Returns the coincidence that admitted the pair, or why none did. There is no numeric score and
    no threshold to tune at the call site: a score would invite exactly the mistake this signal
    must not make, which is being compared against the confidence of a real frame match.
    """
    page, original = inputs.page_duration_s, inputs.original_duration_s
    shared_cast = inputs.page_creator_ids & inputs.original_creator_ids
    overlap = token_jaccard(inputs.page_title_tokens, inputs.original_title_tokens)
    signals: dict[str, float | int | bool] = {
        "scorer_version": SCORER_VERSION,
        "shared_cast": len(shared_cast),
        "title_jaccard": round(overlap, 4),
    }
    if original is None or original <= 0:
        # A photo collection has no duration, and neither clause can fire without one. Photo
        # collections are reached by PDQ on the images themselves (ADR 0020), not by this signal.
        return MetadataSignal(MetadataReason.NO_ORIGINAL_DURATION, signals)
    if page is None or page <= 0:
        return MetadataSignal(MetadataReason.NO_PAGE_DURATION, signals)
    difference = abs(page - original)
    tolerance = duration_tolerance(max(page, original))
    signals["duration_difference_s"] = round(difference, 3)
    signals["duration_tolerance_s"] = round(tolerance, 3)
    if shared_cast and difference <= CAST_DURATION_TOLERANCE_S:
        return MetadataSignal(MetadataReason.CAST_AND_DURATION, signals)
    if difference <= tolerance and overlap >= TITLE_MIN_JACCARD:
        return MetadataSignal(MetadataReason.DURATION_AND_TITLE, signals)
    return MetadataSignal(MetadataReason.NO_SIGNAL, signals)
