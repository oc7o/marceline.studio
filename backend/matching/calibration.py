"""Confidence-band thresholds from human decisions; DB side of `matching.confidence`."""

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Case, CharField, Value, When

from matching.confidence import (
    GUARD_HIGH_WOULD_FALL,
    GUARD_OPERATOR_OVERRIDE,
    HIGH,
    HIGH_MIN_EVIDENCE_FRAMES,
    LOW,
    REVIEW,
    Calibration,
    Thresholds,
    band_for,
    calibrate,
    collection_images,
)
from matching.models import ConfidenceSettings, Reference, ReferenceLeak, ReferenceMatch

SETTINGS_PK = 1


@dataclass(frozen=True)
class SampleAccounting:
    """What the labelled pairs are made of; every run prints it (issue #90).

    Calibration maps a *confidence* to a precision, so only a pair the matcher scored — one with a
    stored `ReferenceMatch` row — can be a sample. The two `without_match` counts are human
    decisions on pairs nothing ever scored: `suggest_leaks` also proposes duration-only
    candidates, and a verdict on one of those is a verdict on that suggestion, not on our evidence.
    """

    positives: int
    negatives: int
    rejected_without_match: int
    confirmed_without_match: int

    def lines(self) -> list[str]:
        return [
            "Sample accounting:",
            f"  positives (confirmed, with a stored match): {self.positives}",
            f"  informative negatives (rejected, with a stored match): {self.negatives}",
            "  rejected without a stored match (excluded, no confidence to place): "
            f"{self.rejected_without_match}",
            "  Confirmed leaks without a stored match (no confidence): "
            f"{self.confirmed_without_match}",
        ]


def current_thresholds() -> Thresholds:
    """Stored thresholds, or the conservative defaults when no settings row exists."""
    row = ConfidenceSettings.objects.filter(pk=SETTINGS_PK).first()
    return Thresholds(row.high_threshold, row.review_threshold) if row else Thresholds()


def labeled_samples() -> tuple[list[tuple[float, bool]], SampleAccounting]:
    """(confidence, is_leak) for stored matches with a human decision in either table, plus the
    accounting of the human decisions that are not samples.

    Rejection in either table wins (as in the M2 report). Auto decisions are not labels.

    A decision on a pair with **no** stored match is counted and excluded, never scored at 0: the
    scorer never looked at that pair, so 0 would be a fabricated value — and it would land below
    every candidate threshold, removing the `high >= 0.00` symptom while teaching the curve above
    0 nothing at all and inflating the negative count the guards read (issue #90).
    """
    leaks = {
        (reference_id, page_id): status
        for reference_id, page_id, status in ReferenceLeak.objects.exclude(
            status="proposed"
        ).values_list("reference_id", "page_id", "status")
    }
    samples: list[tuple[float, bool]] = []
    matched: set[tuple[int, int]] = set()
    for reference_id, page_id, status, decided_by, confidence in ReferenceMatch.objects.order_by(
        "pk"
    ).values_list("reference_id", "page_id", "status", "decided_by", "confidence"):
        key = (reference_id, page_id)
        matched.add(key)
        human_match = status if status != "proposed" and decided_by != "auto" else None
        decisions = {human_match, leaks.get(key)} - {None}
        if "rejected" in decisions:
            samples.append((confidence, False))
        elif "confirmed" in decisions:
            samples.append((confidence, True))
    unscored = [status for key, status in leaks.items() if key not in matched]
    positives = sum(label for _, label in samples)
    return samples, SampleAccounting(
        positives=positives,
        negatives=len(samples) - positives,
        rejected_without_match=unscored.count("rejected"),
        confirmed_without_match=unscored.count("confirmed"),
    )


def apply_bands(thresholds: Thresholds) -> int:
    """Re-band stored matches from their stored confidence; returns rows re-banded.

    Video matches are re-banded in one UPDATE. A photo-collection match carries its floor in
    `matched_seconds` (which images matched) rather than in a column, so those rows are re-banded
    one by one with exactly the rule `match_references` applied (ADR 0020).

    Candidate rows — the embedding's (ADR 0037) and the metadata scorer's (ADR 0040) — are left
    out of both: they hold no evidence, so a calibrated threshold has nothing to say about them,
    and their band is fixed at `review` by `band_for` and by a database constraint. This exclusion
    is the third of the three independent guards, and the one that matters over time: calibration
    runs again whenever labels arrive, so a candidate row left in would be silently re-banded into
    `high` the first time the thresholds moved below its confidence.
    """
    rows = ReferenceMatch.objects.exclude(
        candidate_source__in=ReferenceMatch.CANDIDATE_ONLY_SOURCES
    )
    rebanded = rows.filter(reference__kind=Reference.Kind.VIDEO).update(
        band=Case(
            When(
                confidence__gte=thresholds.high,
                evidence_frames__gte=HIGH_MIN_EVIDENCE_FRAMES,
                then=Value(HIGH),
            ),
            When(confidence__gte=thresholds.review, then=Value(REVIEW)),
            default=Value(LOW),
            output_field=CharField(),
        )
    )
    for pk, confidence, frames, matched, band in rows.filter(
        reference__kind=Reference.Kind.PHOTOS
    ).values_list("pk", "confidence", "evidence_frames", "matched_seconds", "band"):
        images = collection_images(len(matched) if isinstance(matched, list) else 0, frames)
        current = band_for(confidence, frames, thresholds, collection_images=images)
        if current != band:
            ReferenceMatch.objects.filter(pk=pk).update(band=current)
        rebanded += 1
    return rebanded


def operator_override(row: ConfidenceSettings | None) -> bool:
    """Do the thresholds in force differ from the ones calibration itself last wrote?

    Calibration records what it derived beside what it stores, so anything that moved the
    effective thresholds without moving the derived ones was a human — the operator's 0.75 / 0.35
    of 2026-09-17 is the case this exists for. A row from before this field pair (both NULL) has
    no recorded provenance and is treated as the operator's, which is the safe direction: the
    worst outcome is one `--replace-override` flag, against silently discarding a hand-set band.
    """
    if row is None:
        return False
    if row.calibrated_high is None or row.calibrated_review is None:
        return True
    return (row.high_threshold, row.review_threshold) != (
        row.calibrated_high,
        row.calibrated_review,
    )


@transaction.atomic
def calibrate_confidence(*, dry_run: bool = False, replace_override: bool = False) -> list[str]:
    """Derive band thresholds from human decisions and store them, unless a guard says not to.

    Storing is the dangerous half: the thresholds re-band every stored match, and the high band is
    what a notice is drawn from. So this never writes when the labels cannot carry the thresholds
    (`matching.confidence.calibrate`), when the high band would fall without new negatives, or
    when the thresholds in force are an operator's and `replace_override` was not asked for. A
    refused run writes nothing at all — not even the report.
    """
    samples, accounting = labeled_samples()
    row = ConfidenceSettings.objects.filter(pk=SETTINGS_PK).first()
    in_force = current_thresholds()
    result: Calibration = calibrate(samples, current=in_force)
    lines = [*accounting.lines(), *result.lines]
    guard = result.guard
    chosen = result.chosen
    if chosen is not None and row is not None:
        if chosen.high < row.high_threshold and accounting.negatives <= row.labeled_negatives:
            guard = GUARD_HIGH_WOULD_FALL
            lines.append(
                f"Guard {guard}: high {row.high_threshold:.2f} -> {chosen.high:.2f} on "
                f"{accounting.negatives} scored negative(s), no more than the "
                f"{row.labeled_negatives} the stored thresholds were derived from"
            )
        elif operator_override(row) and not replace_override:
            guard = GUARD_OPERATOR_OVERRIDE
            lines.append(
                f"Guard {guard}: the thresholds in force were not written by calibration; "
                "keeping them. Pass --replace-override to store the derived ones."
            )
    if guard or chosen is None:
        lines.append(
            f"Thresholds unchanged: high>={in_force.high:.2f}, review>={in_force.review:.2f}"
        )
        if chosen is not None:
            lines.append(
                f"  derived but not stored: high>={chosen.high:.2f}, review>={chosen.review:.2f}"
            )
        lines.append("Nothing stored.")
        return lines
    lines.append(
        f"Thresholds: high {in_force.high:.2f} -> {chosen.high:.2f}, "
        f"review {in_force.review:.2f} -> {chosen.review:.2f}"
    )
    if dry_run:
        lines.append("Dry run: thresholds not stored.")
        return lines
    ConfidenceSettings.objects.update_or_create(
        pk=SETTINGS_PK,
        defaults={
            "high_threshold": chosen.high,
            "review_threshold": chosen.review,
            "calibrated_high": chosen.high,
            "calibrated_review": chosen.review,
            "calibrated": result.calibrated,
            "labeled_positives": result.positives,
            "labeled_negatives": result.negatives,
            "report": "\n".join(lines),
        },
    )
    lines.append(f"Stored thresholds; re-banded {apply_bands(chosen)} matches.")
    return lines
