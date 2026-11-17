"""The database side of the third signal (ADR 0040): metadata candidates as an aiming signal.

`matching.metadata_score` decides; this module loads what it needs and reports what the rule
proposes. **It writes nothing** (ADR 0040 amendment 2026-09-18). The operator declined the
original output — a review-band `ReferenceMatch` a human had to judge — because every field this
signal reads is the pirate site's own text about its own upload: a re-title defeats the title
clause, a stripped credit defeats the cast clause, and a forged duration is cheap to produce, so
a reviewer shown such a row is judging the site's claims and not our evidence.

What a candidate is now: **a fetch instruction**. `matching.capture_triage` turns each admitted
pair into one more source of the browser-capture queue (ADR 0016) — "this page is worth
fingerprinting next" — under every exclusion, budget and window that queue already enforces, plus
two of its own (`matching.triage.metadata_exclusion`): the ordinary media gate must already allow
the page, and the page must still be frame-poor. Then PDQ decides by itself. A capture that
confirms produces an ordinary match with real evidence frames through the normal path; one that
does not lets the candidate die on the next refresh, seen by nobody.

The guards that keep this signal out of evidence stay where they are and are not this module's
business: the `reference_match_metadata_is_never_evidence` constraint,
`confidence.band_for(metadata_only=True)` and `calibration.apply_bands`. Nothing writes such a row
any more; the guards are what makes that an invariant instead of a fact about today's code.

Two things this module refuses to do, both deliberate:

- it never **rejects** a pair. Duration is an indicator, never a hard filter (clips are leaks), so
  a page whose duration disagrees is simply not proposed by this signal; nothing is written, no
  other signal's row is touched, and no status changes;
- it never puts anything in front of a human, by any route at all.
"""

import time
from dataclasses import dataclass

from django.db.models import Count

from index.models import FrameHash, Page
from matching.creators import creator_ids
from matching.duration import page_duration
from matching.metadata_score import (
    SCORER_VERSION,
    MetadataInputs,
    MetadataReason,
    MetadataSignal,
    score_metadata,
    title_tokens,
)
from matching.models import Reference
from matching.triage import FEW_FRAME_HASHES

# Candidates one original may contribute in a single run. Measured over the whole index
# (docs/matching/metadata-scoring.md): 3.4 per original, worst original 12. The cap is more than
# an order of magnitude above that, so it changes nothing today and turns a future data accident —
# a site that starts publishing one duration for its whole catalogue, a creator linked to
# thousands of pages — into a logged number instead of a flooded capture queue.
MAX_CANDIDATES_PER_REFERENCE = 200

# The frame count below which a page is worth aiming a capture at, for the report only — the
# binding check is `matching.triage.metadata_exclusion`, which uses the same number.
FEW_FRAMES = FEW_FRAME_HASHES


@dataclass(frozen=True)
class Candidate:
    """One proposed (original, page) pair and the coincidence that proposed it."""

    reference_id: int
    page_id: int
    signal: MetadataSignal

    def tightness(self) -> tuple[int, float]:
        """Sort key for the per-original cap: the cast clause first, then the closest duration."""
        cast_first = 0 if self.signal.reason == MetadataReason.CAST_AND_DURATION else 1
        difference = self.signal.signals.get("duration_difference_s", 0.0)
        return cast_first, float(difference)


@dataclass(frozen=True)
class ScoredPage:
    """One active page's scorable metadata, loaded once and reused for every original."""

    page_id: int
    duration_s: float
    creator_ids: frozenset[int]
    title_tokens: frozenset[str]


@dataclass(frozen=True)
class MetadataReport:
    """What one pass proposed, in the terms the measurement is written in. Nothing was written."""

    references: int
    pages: int
    pairs: int
    capped: int
    by_reason: dict[str, int]
    frame_poor_pages: int
    no_frame_pages: int
    seconds: float

    def line(self) -> str:
        reasons = ", ".join(f"{name} {count}" for name, count in sorted(self.by_reason.items()))
        return (
            f"Metadata candidates (scorer v{SCORER_VERSION}): {self.references} originals x "
            f"{self.pages} active pages -> {self.pairs} pairs admitted ({reasons or 'none'}); "
            f"{self.capped} over the per-original cap; {self.seconds:.1f} s"
        )

    def aim_line(self) -> str:
        """Where the pairs point, which is the only thing this signal produces."""
        return (
            f"Pages named: {self.frame_poor_pages} hold fewer than {FEW_FRAMES} frame hashes "
            f"(worth a capture), of which {self.no_frame_pages} hold none at all"
        )

    def empty_reason(self) -> str:
        """Why nothing happened, in words that name the missing half."""
        if not self.references:
            return "no originals with a duration: this signal scores videos, not photo collections"
        if not self.pages:
            return "no active pages with a parsed duration"
        if not self.pairs:
            return (
                "no page's duration, cast or title coincides with an original's; nothing is wrong "
                "- this signal proposes nothing when the site text disagrees"
            )
        return ""


def load_references() -> list[tuple[int, MetadataInputs]]:
    """Every video original with a usable duration, as half-filled scoring inputs.

    A photo collection has no duration (`reference_shape_matches_kind`), so neither clause of the
    rule can fire on one; they are reached by PDQ on the images themselves (ADR 0020).
    """
    return [
        (
            reference.pk,
            MetadataInputs(
                page_duration_s=None,
                original_duration_s=reference.duration_s,
                original_creator_ids=frozenset(creator_ids(reference)),
                original_title_tokens=title_tokens(reference.title),
            ),
        )
        for reference in Reference.objects.filter(
            kind=Reference.Kind.VIDEO, duration_s__isnull=False, duration_s__gt=0
        ).order_by("pk")
    ]


def load_pages() -> list[ScoredPage]:
    """Active pages with a parsed duration, with their creator links and title tokens.

    Active pages only, like every other page-side loader in matching (issue #71): a page of no
    target has had its title and performer credits scrubbed anyway (ADR 0029), so there would be
    nothing left to score.
    """
    links: dict[int, set[int]] = {}
    for page_id, creator_id in Page.creators.through.objects.filter(page__active=True).values_list(
        "page_id", "creator_id"
    ):
        links.setdefault(page_id, set()).add(creator_id)
    pages = []
    for page_id, raw, title in (
        Page.objects.filter(active=True).order_by("pk").values_list("pk", "raw_extraction", "title")
    ):
        duration = page_duration(raw)
        if duration is None:
            continue
        pages.append(
            ScoredPage(
                page_id=page_id,
                duration_s=duration,
                creator_ids=frozenset(links.get(page_id, ())),
                title_tokens=title_tokens(title),
            )
        )
    return pages


@dataclass(frozen=True)
class Collected:
    candidates: list[Candidate]
    references: int
    pages: int
    admitted: int
    capped: int


def collect_candidates() -> Collected:
    """Score every (original, active page) pair and keep the admitted ones.

    The rule itself is `metadata_score.score_metadata`, which sees floats, integers and strings
    and no ORM at all; everything here is loading and bookkeeping.
    """
    references = load_references()
    pages = load_pages()
    candidates: list[Candidate] = []
    admitted = 0
    capped = 0
    for reference_id, base in references:
        mine: list[Candidate] = []
        for page in pages:
            signal = score_metadata(
                MetadataInputs(
                    page_duration_s=page.duration_s,
                    original_duration_s=base.original_duration_s,
                    page_creator_ids=page.creator_ids,
                    original_creator_ids=base.original_creator_ids,
                    page_title_tokens=page.title_tokens,
                    original_title_tokens=base.original_title_tokens,
                )
            )
            if signal.proposes:
                mine.append(Candidate(reference_id, page.page_id, signal))
        admitted += len(mine)
        if len(mine) > MAX_CANDIDATES_PER_REFERENCE:
            # Keep the tightest coincidences; a cap that cut arbitrarily would be worse than none.
            mine.sort(key=Candidate.tightness)
            capped += len(mine) - MAX_CANDIDATES_PER_REFERENCE
            mine = mine[:MAX_CANDIDATES_PER_REFERENCE]
        candidates.extend(mine)
    return Collected(candidates, len(references), len(pages), admitted, capped)


def page_frame_counts(page_ids: set[int]) -> dict[int, int]:
    """How many frame hashes each page holds — for the report, never part of the rule.

    Few frames is the case worth a capture: PDQ has not had a proper look at such a page yet.
    Zero is the case that stays out of reach — measured, every candidate page with no frame hash
    at all is one the media gate refuses, so no fetch may go there (ADR 0040 amendment).
    """
    if not page_ids:
        return {}
    return dict(
        FrameHash.objects.filter(media__page_id__in=page_ids)
        .values_list("media__page_id")
        .annotate(total=Count("pk"))
    )


def aim() -> MetadataReport:
    """Score every (original, active page) pair and report what the rule aims at.

    Read-only, by construction: this module has no writer. The pairs themselves are collected by
    `collect_candidates`, which is what `matching.capture_triage` calls.
    """
    clock = time.perf_counter()
    collected = collect_candidates()
    by_reason: dict[str, int] = {}
    for item in collected.candidates:
        key = str(item.signal.reason)
        by_reason[key] = by_reason.get(key, 0) + 1
    page_ids = {item.page_id for item in collected.candidates}
    frames = page_frame_counts(page_ids)
    return MetadataReport(
        references=collected.references,
        pages=collected.pages,
        pairs=collected.admitted,
        capped=collected.capped,
        by_reason=by_reason,
        frame_poor_pages=sum(1 for page_id in page_ids if frames.get(page_id, 0) < FEW_FRAMES),
        no_frame_pages=sum(1 for page_id in page_ids if not frames.get(page_id, 0)),
        seconds=time.perf_counter() - clock,
    )
