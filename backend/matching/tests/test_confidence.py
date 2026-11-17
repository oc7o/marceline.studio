import hashlib
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching.calibration import current_thresholds, labeled_samples
from matching.confidence import (
    DEFAULT_HIGH_THRESHOLD,
    GUARD_HIGH_WOULD_FALL,
    GUARD_INSUFFICIENT_NEGATIVES,
    GUARD_INSUFFICIENT_POSITIVES,
    GUARD_OPERATOR_OVERRIDE,
    GUARD_PRECISION_UNPROVEN,
    HIGH_MIN_COLLECTION_IMAGES,
    HIGH_TARGET_PRECISION,
    HIGH_THRESHOLD_FLOOR,
    MIN_CALIBRATION_POSITIVES,
    ConfidenceInputs,
    Thresholds,
    band_for,
    calibrate,
    collection_images,
    score_confidence,
    wilson_lower_bound,
)
from matching.models import (
    Cluster,
    ClusterMember,
    ConfidenceSettings,
    Reference,
    ReferenceFrameHash,
    ReferenceLeak,
    ReferenceMatch,
)
from matching.references import STRICT_MAX_DISTANCE, match_references
from matching.tests.hashes import flip_bits, random_hashes

SCENES = random_hashes(4, seed=31)
# A distance that is loose but not strict: evidence only with a timeline or
# corroboration. Derived, so it moves with the threshold instead of breaking.
LOOSE_ONLY = STRICT_MAX_DISTANCE + 2


def inputs(**overrides: object) -> ConfidenceInputs:
    values: dict[str, object] = {
        "evidence_frames": 3,
        "best_distance": 8,
        "loose_max_distance": 40,
        "timeline_score": 1.0,
        "page_duration_s": 1000.0,
        "reference_duration_s": 1000.0,
    }
    values.update(overrides)
    return ConfidenceInputs(**values)  # type: ignore[arg-type]


def test_score_components_and_bands() -> None:
    strong = score_confidence(inputs())
    assert strong.value > DEFAULT_HIGH_THRESHOLD
    assert band_for(strong.value, 3, Thresholds()) == "high"
    assert set(strong.components) == {
        "evidence",
        "distance",
        "timeline",
        "duration",
        "corroboration",
        "common",
        "images",
    }
    # A video original earns nothing from the photo-collection component.
    assert strong.components["images"] == 0.0
    # One frame never reaches the high band, whatever its confidence.
    single = score_confidence(inputs(evidence_frames=1, corroborated=True))
    assert band_for(single.value, 1, Thresholds(high=0.1, review=0.05)) == "review"
    lonely = score_confidence(
        inputs(evidence_frames=1, best_distance=30, timeline_score=None, page_duration_s=None)
    )
    assert band_for(lonely.value, 1, Thresholds()) == "low"
    assert score_confidence(inputs(evidence_frames=0)).value == 0


def test_clips_not_penalised_and_neutral_timeline() -> None:
    unknown = score_confidence(inputs(page_duration_s=None)).value
    assert score_confidence(inputs(page_duration_s=40.0)).value == unknown
    assert score_confidence(inputs(page_duration_s=1020.0)).value > unknown
    assert score_confidence(inputs(page_duration_s=1000.5)).value > unknown
    none = score_confidence(inputs(timeline_score=None)).value
    assert score_confidence(inputs(timeline_score=0.0)).value < none
    assert none < score_confidence(inputs(timeline_score=1.0)).value
    assert score_confidence(inputs(common_frames=3)).value < score_confidence(inputs()).value
    assert score_confidence(inputs(best_distance=36)).value < score_confidence(inputs()).value


def test_photo_collection_evidence_rule() -> None:
    """A collection is judged on independent images, never on order or duration (ADR 0020)."""
    assert HIGH_MIN_COLLECTION_IMAGES == 3
    assert collection_images(3, 3) == 3
    # One page picture matching three similar photos of one shoot is one piece of evidence, and
    # so are three page frames all showing the same photo.
    assert collection_images(3, 1) == 1
    assert collection_images(1, 3) == 1

    def photos(images: int, distance: int = 0) -> float:
        return score_confidence(
            inputs(
                evidence_frames=images,
                best_distance=distance,
                timeline_score=None,
                page_duration_s=None,
                reference_duration_s=None,
                collection_images=images,
            )
        ).value

    # One matched image adds nothing for being an image; more images add more, with a ceiling.
    assert photos(1) < photos(2) < photos(3) < photos(4)
    assert band_for(photos(1), 1, Thresholds(), collection_images=1) == "review"
    assert band_for(photos(2), 2, Thresholds(), collection_images=2) == "review"
    assert band_for(photos(3), 3, Thresholds(), collection_images=3) == "high"
    # Far-away hashes drop back into review even with three images.
    assert band_for(photos(3, 20), 3, Thresholds(), collection_images=3) == "review"
    # Two page frames reach the high band for a *video*, where an order and a duration corroborate
    # them. The same two pieces of evidence never do for a collection - the point of the rule.
    assert band_for(score_confidence(inputs(evidence_frames=2)).value, 2, Thresholds()) == "high"


def poster_only(distance: int, page_duration_s: float | None = None) -> float:
    """Confidence of the commonest page shape: one unordered frame, no timeline."""
    return score_confidence(
        inputs(
            evidence_frames=1,
            best_distance=distance,
            timeline_score=None,
            page_duration_s=page_duration_s,
            reference_duration_s=None if page_duration_s is None else 1000.0,
        )
    ).value


@pytest.mark.parametrize(
    ("page_duration_s", "edge"),
    [(None, 25), (1020.0, 35), (1000.5, 40)],
)
def test_poster_only_band_edge_clips_the_strict_distance(
    page_duration_s: float | None, edge: int
) -> None:
    """Where the review band ends for a single poster (issue #73, docs/matching/).

    A lone unordered frame earns evidence 0.225 + distance 0.20 x (1 - d/40) + a neutral timeline
    0.10, so at the default `review >= 0.40` the *band* stops at distance 25 - eleven bits below
    the matcher's strict threshold (`STRICT_MAX_DISTANCE`) - unless the page duration adds its
    bonus. Most matches on the live index have exactly this shape, and an excerpt can never earn
    the duration bonus, so this boundary decides poster recall. It is measured here, not asserted
    as good: a weight change must show up as a changed number in this test.
    """
    bands = {d: band_for(poster_only(d, page_duration_s), 1, Thresholds()) for d in range(41)}
    review = [d for d, band in bands.items() if band != "low"]
    assert max(review) == edge and review == list(range(edge + 1))
    # Never the high band, whatever the distance: one frame is not two (HIGH_MIN_EVIDENCE_FRAMES).
    assert set(bands.values()) <= {"review", "low"}


def test_calibrated_review_threshold_stops_one_bit_short_of_the_strict_edge() -> None:
    """The band, not the matcher, decides what a lone poster is worth (issue #73, ADR 0015).

    `calibrate` on the live labels chose `review >= 0.35`; at that threshold a poster-only match
    with no comparable duration stays visible up to distance 35 and drops into the hidden low band
    at 36. Raising `STRICT_MAX_DISTANCE` to 36 (ADR 0015, 2026-09-18) therefore buys nothing for
    that one shape at the *stored* thresholds: such a row is created and then hidden. Measured,
    not asserted as good - `calibrate_confidence` may move the edge again, and then this number
    changes with it.
    """
    calibrated = Thresholds(high=0.5, review=0.35)
    assert band_for(poster_only(31), 1, calibrated) == "review"
    assert band_for(poster_only(35), 1, calibrated) == "review"
    assert band_for(poster_only(STRICT_MAX_DISTANCE), 1, calibrated) == "low"


# A labelled set large enough to prove 98% precision: with zero false matches above the threshold
# the 95% lower bound needs about 190 decided pairs above it (issue #90).
HEALTHY = [(0.9, True)] * 190 + [(0.55, True)] * 10 + [(0.78, False)] * 5 + [(0.2, False)] * 20
IN_FORCE = Thresholds(high=0.75, review=0.35)  # the operator's override of 2026-09-17


def test_calibration_keeps_the_thresholds_in_force_without_enough_positives() -> None:
    few = calibrate([(0.9, True)] * 3 + [(0.2, False)] * 50, current=IN_FORCE)
    assert not few.calibrated and few.thresholds == IN_FORCE and few.chosen is None
    assert few.guard == GUARD_INSUFFICIENT_POSITIVES
    assert any(f"Guard {GUARD_INSUFFICIENT_POSITIVES}" in line for line in few.lines)


def test_calibration_refuses_without_scored_negatives() -> None:
    """The 2026-09-18 shape, as pure samples: 95 positives and exactly one scored negative.

    Every threshold on the grid "passed" the old point-estimate test, so the lowest one won and
    `high >= 0.00` would have been stored (issue #90). A precision target is a statement about
    negatives, so with fewer than `MIN_CALIBRATION_NEGATIVES` of them nothing is derived at all.
    """
    result = calibrate([(0.5, True)] * 95 + [(0.45, False)], current=IN_FORCE)
    assert result.guard == GUARD_INSUFFICIENT_NEGATIVES and not result.calibrated
    assert result.thresholds == IN_FORCE and result.chosen is None
    assert not any("high>=0.00" in line for line in result.lines)


def test_calibration_refuses_a_precision_it_cannot_prove() -> None:
    """100% precision over 20 pairs is not evidence of 98% precision (Wilson bound 0.84)."""
    samples = [(0.85, True)] * MIN_CALIBRATION_POSITIVES + [(0.55, True)] * 5
    samples += [(0.6, False)] * 3 + [(0.2, False)] * 20
    result = calibrate(samples, current=IN_FORCE)
    assert (result.positives, result.negatives) == (25, 23)
    assert result.guard == GUARD_PRECISION_UNPROVEN and result.thresholds == IN_FORCE
    assert any(
        "confidence 0.8-0.9: 20 labeled, 20 leaks, precision 100.0%" in x for x in result.lines
    )


def test_calibration_never_chooses_a_high_threshold_below_the_floor() -> None:
    """Perfectly separated labels, all of them below the floor: no threshold, not a low one."""
    result = calibrate([(0.45, True)] * 200 + [(0.05, False)] * 20, current=IN_FORCE)
    assert result.guard == GUARD_PRECISION_UNPROVEN
    assert any(f"grid starts at {HIGH_THRESHOLD_FLOOR:.2f}" in line for line in result.lines)


def test_calibration_on_a_healthy_shape() -> None:
    result = calibrate(HEALTHY, current=IN_FORCE)
    assert result.calibrated and result.guard == ""
    assert (result.positives, result.negatives) == (200, 25)
    assert result.thresholds == Thresholds(high=0.8, review=0.55) == result.chosen
    assert any("95% lower bound 0.980" in line for line in result.lines)


def test_wilson_lower_bound_grows_with_the_sample() -> None:
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(20, 20) < HIGH_TARGET_PRECISION < wilson_lower_bound(190, 190)
    assert wilson_lower_bound(190, 190) < 1.0


def hover_page(site: Site, slug: str, values: list[bytes], duration: float) -> Page:
    page = Page.objects.create(
        site=site,
        title=slug,
        url=f"https://{site.domain}/{slug}",
        raw_extraction={"duration_s": duration},
    )
    for index, value in enumerate(values, start=1):
        media = Media.objects.create(
            page=page, kind="frame", url=f"https://cdn.test/imgs/1/2/{slug}_{index}.jpg"
        )
        FrameHash.objects.create(media=media, pdq=value, quality=100)
    return page


@pytest.fixture
def reference(db: None) -> Reference:
    creator = Creator.objects.create(name="Synthetic Creator")
    reference = Reference.objects.create(
        creator=creator,
        title="Original",
        sha256=hashlib.sha256(b"x").hexdigest(),
        frame_fps=1,
        duration_s=1000,
    )
    for second, value in zip((100, 400, 700, 900), SCENES, strict=True):
        ReferenceFrameHash.objects.create(
            reference=reference, frame_index=second, timestamp_s=second, pdq=value, quality=100
        )
    return reference


@pytest.mark.django_db
def test_high_band_not_auto_confirmed_by_default(reference: Reference, site: Site) -> None:
    page = hover_page(site, "leak", [flip_bits(value, 6) for value in SCENES[:3]], 1000)
    output = match_references([reference])
    assert "bands: 1 high, 0 review, 0 low" in output[0]
    match = ReferenceMatch.objects.get(page=page)
    assert (match.band, match.status, match.decided_by) == ("high", "proposed", "")
    assert match.timeline_score == 1.0 and match.consistent_frames == 3
    assert match.confidence_signals["duration"] > 0

    with override_settings(AUTO_CONFIRM_HIGH=True):
        match_references([reference])
        match.refresh_from_db()
        assert (match.status, match.decided_by) == ("confirmed", "auto")
        # Auto decisions are not human labels.
        assert labeled_samples()[0] == []
        # A human resets it: from now on it is a human decision and is not re-confirmed.
        ReferenceMatch.objects.filter(pk=match.pk).update(status="proposed")
        match_references([reference])
        match.refresh_from_db()
        assert (match.status, match.decided_by) == ("proposed", "human")
        ReferenceMatch.objects.filter(pk=match.pk).update(status="rejected")
        match_references([reference])
        match.refresh_from_db()
        assert (match.status, match.decided_by) == ("rejected", "human")
    samples, accounting = labeled_samples()
    assert samples == [(match.confidence, False)]
    assert (accounting.positives, accounting.negatives) == (0, 1)


@pytest.mark.django_db
def test_corroborated_loose_poster(reference: Reference, site: Site) -> None:
    strong = hover_page(site, "strong", [flip_bits(v, 6) for v in SCENES[:2]], 1000)
    mirror = Page.objects.create(
        site=site, url="https://example.test/mirror", raw_extraction={"duration_s": 1000}
    )
    media = Media.objects.create(page=mirror, kind="poster", url="https://example.test/m.jpg")
    FrameHash.objects.create(media=media, pdq=flip_bits(SCENES[3], LOOSE_ONLY), quality=100)
    match_references([reference])
    assert not ReferenceMatch.objects.filter(page=mirror).exists()
    cluster = Cluster.objects.create()
    for page in (strong, mirror):
        ClusterMember.objects.create(cluster=cluster, page=page, evidence="mirror")
    match_references([reference])
    match = ReferenceMatch.objects.get(page=mirror)
    assert (match.strength, match.best_distance, match.loose_frames) == ("weak", LOOSE_ONLY, 1)
    assert match.timeline_score is None and match.confidence_signals["corroborated"] is True
    assert match.band == "review"
    ReferenceMatch.objects.filter(page=strong).update(status="rejected")
    match_references([reference])
    assert not ReferenceMatch.objects.filter(page=mirror).exists()


@pytest.mark.django_db
def test_review_queue_and_calibration_command(
    reference: Reference, site: Site, admin_client: Client
) -> None:
    rows = []
    for i, (band, confidence) in enumerate(
        [("review", 0.5), ("high", 0.8), ("low", 0.2), ("high", 0.9), ("review", 0.6)]
    ):
        page = Page.objects.create(site=site, url=f"https://example.test/q{i}")
        rows.append(
            ReferenceMatch.objects.create(
                reference=reference,
                page=page,
                best_distance=10,
                matched_frames=2,
                page_frames=2,
                best_reference_timestamp_s=0,
                band=band,
                confidence=confidence,
            )
        )
    order = [rows[i].pk for i in (3, 1, 4, 0)]
    assert list(ReferenceMatch.objects.review_queue().values_list("pk", flat=True)) == order
    queue = ReferenceMatch.objects.review_queue(include_low=True)
    assert list(queue.values_list("pk", flat=True)) == [*order, rows[2].pk]
    ReferenceLeak.objects.create(reference=reference, page=rows[1].page, status="confirmed")
    ReferenceMatch.objects.filter(pk=rows[2].pk).update(status="rejected", decided_by="human")

    output = StringIO()
    call_command("calibrate_confidence", "--dry-run", stdout=output)
    assert "Labeled pairs: 2 (1 leaks, 1 false matches)" in output.getvalue()
    assert f"Guard {GUARD_INSUFFICIENT_POSITIVES}" in output.getvalue()
    assert not ConfidenceSettings.objects.exists()
    assert current_thresholds() == Thresholds()
    # A refused run stores nothing at all - writing "the defaults" over a row is itself the damage
    # this guards against (issue #90).
    call_command("calibrate_confidence", stdout=StringIO())
    assert not ConfidenceSettings.objects.exists()

    response = admin_client.get("/admin/matching/referencematch/?band__exact=high")
    assert response.status_code == 200
    response = admin_client.post(
        "/admin/matching/referencematch/",
        {"action": "revert_auto_decisions", "_selected_action": [rows[3].pk]},
    )
    assert response.status_code == 302


def decided_pairs(
    reference: Reference,
    site: Site,
    *,
    scored: list[tuple[float, bool]],
    rejected_without_match: int = 0,
) -> None:
    """Synthetic human decisions: `scored` pairs get a stored match, the rest only a leak row."""
    total = len(scored) + rejected_without_match
    pages = Page.objects.bulk_create(
        Page(site=site, url=f"https://{site.domain}/label/{i}") for i in range(total)
    )
    ReferenceMatch.objects.bulk_create(
        ReferenceMatch(
            reference=reference,
            page=page,
            best_distance=10,
            matched_frames=2,
            page_frames=2,
            best_reference_timestamp_s=0,
            evidence_frames=2,
            confidence=confidence,
            band="low",
            status="confirmed" if is_leak else "rejected",
            decided_by="human",
        )
        for page, (confidence, is_leak) in zip(pages, scored, strict=False)
    )
    ReferenceLeak.objects.bulk_create(
        ReferenceLeak(reference=reference, page=page, status="rejected")
        for page in pages[len(scored) :]
    )


@pytest.mark.django_db
def test_the_2026_09_18_shape_refuses_instead_of_storing_high_0_00(
    reference: Reference, site: Site
) -> None:
    """95 confirmed matches, 1 rejected match, 160 rejections nothing ever scored (issue #90).

    The rejected leaks without a match row are the ones `suggest_leaks` proposed on duration
    alone. They are counted and excluded, never scored at 0: at 0 they would only move the chosen
    threshold from 0.00 to 0.05 and make the guard pass on 161 "negatives" the scorer never saw.
    """
    decided_pairs(
        reference,
        site,
        scored=[(0.5, True)] * 95 + [(0.45, False)],
        rejected_without_match=160,
    )
    output = StringIO()
    call_command("calibrate_confidence", "--dry-run", stdout=output)
    printed = output.getvalue()
    assert "high>=0.00" not in printed
    assert "Labeled pairs: 96 (95 leaks, 1 false matches)" in printed
    assert "  informative negatives (rejected, with a stored match): 1" in printed
    assert "  rejected without a stored match (excluded, no confidence to place): 160" in printed
    assert f"Guard {GUARD_INSUFFICIENT_NEGATIVES}" in printed
    assert "Thresholds unchanged: high>=0.75, review>=0.40" in printed

    call_command("calibrate_confidence", stdout=StringIO())
    assert not ConfidenceSettings.objects.exists()
    assert current_thresholds() == Thresholds()
    assert set(ReferenceMatch.objects.values_list("band", flat=True)) == {"low"}


@pytest.mark.django_db
def test_operator_override_is_kept_until_it_is_explicitly_replaced(
    reference: Reference, site: Site
) -> None:
    decided_pairs(reference, site, scored=HEALTHY)
    # The operator's 2026-09-17 row: thresholds set by hand, no calibration provenance.
    ConfidenceSettings.objects.create(
        pk=1, high_threshold=0.75, review_threshold=0.35, calibrated=True
    )

    output = StringIO()
    call_command("calibrate_confidence", stdout=output)
    printed = output.getvalue()
    assert f"Guard {GUARD_OPERATOR_OVERRIDE}" in printed
    assert "  derived but not stored: high>=0.80, review>=0.55" in printed
    assert "Nothing stored." in printed
    assert current_thresholds() == Thresholds(high=0.75, review=0.35)

    output = StringIO()
    call_command("calibrate_confidence", "--replace-override", stdout=output)
    printed = output.getvalue()
    assert "Thresholds: high 0.75 -> 0.80, review 0.35 -> 0.55" in printed
    assert "Stored thresholds; re-banded 225 matches." in printed
    stored = ConfidenceSettings.objects.get()
    assert (stored.high_threshold, stored.review_threshold) == (0.8, 0.55)
    assert (stored.calibrated_high, stored.calibrated_review) == (0.8, 0.55)
    assert (stored.labeled_positives, stored.labeled_negatives) == (200, 25)
    # Stored thresholds re-band, which is why storing one the labels cannot carry is the damage:
    # 190 rows at 0.9 with two evidence frames are notice-eligible from here on.
    assert ReferenceMatch.objects.filter(band="high").count() == 190

    # The row is calibration's own from here on, so the next run is not an override.
    output = StringIO()
    call_command("calibrate_confidence", stdout=output)
    assert "Thresholds: high 0.80 -> 0.80, review 0.55 -> 0.55" in output.getvalue()


@pytest.mark.django_db
def test_high_never_falls_unless_new_negatives_arrived(reference: Reference, site: Site) -> None:
    """More positives make a *lower* threshold provable; that is not a reason to lower it."""
    decided_pairs(reference, site, scored=HEALTHY)
    ConfidenceSettings.objects.create(
        pk=1,
        high_threshold=0.9,
        review_threshold=0.55,
        calibrated=True,
        calibrated_high=0.9,
        calibrated_review=0.55,
        labeled_positives=120,
        labeled_negatives=25,
    )
    output = StringIO()
    call_command("calibrate_confidence", stdout=output)
    assert f"Guard {GUARD_HIGH_WOULD_FALL}" in output.getvalue()
    assert "Nothing stored." in output.getvalue()
    assert current_thresholds() == Thresholds(high=0.9, review=0.55)

    # One more scored rejection and the same derivation is allowed through.
    extra = Page.objects.create(site=site, url=f"https://{site.domain}/label/extra")
    ReferenceMatch.objects.create(
        reference=reference,
        page=extra,
        best_distance=10,
        matched_frames=2,
        page_frames=2,
        best_reference_timestamp_s=0,
        evidence_frames=2,
        confidence=0.2,
        status="rejected",
        decided_by="human",
    )
    output = StringIO()
    call_command("calibrate_confidence", stdout=output)
    assert "Thresholds: high 0.90 -> 0.80, review 0.55 -> 0.55" in output.getvalue()
    assert current_thresholds() == Thresholds(high=0.8, review=0.55)
