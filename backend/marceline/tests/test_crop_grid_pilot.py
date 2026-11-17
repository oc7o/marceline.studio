"""The crop grid pilot's pre-registered arithmetic and its refusal to declare success (ADR 0039).

The important assertions here are the negative ones. A pilot that says ADOPT because nothing was
measured is worse than no pilot, so every gap — no gridded original, no near-miss page, no human
verdict on the new proposals — has to read as NOT MEASURED.
"""

import hashlib
import re
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from marceline import crop_grid_pilot as pilot
from matching import crop_grid
from matching.fingerprint import CROP_GRID_PIPELINE, PIPELINE
from matching.models import Reference, ReferenceFrameHash, ReferenceMatch
from matching.pdq import hash_image
from matching.references import STRICT_MAX_DISTANCE
from matching.tests.test_crop_grid import noise_picture, page_copy

pytestmark = pytest.mark.django_db

REFRAME = (0.045, 0.01, 0.015, 0.055)
SECONDS = (0, 1, 2)
DIGEST = hashlib.sha256(b"pilot-original").hexdigest()


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="pilot.test", enabled=True)


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Pilot Creator", needs_review=False)


def master(second: int) -> Any:
    return noise_picture(2000 + second)


def make_pair(creator: Creator, grid_fps: float = 1) -> tuple[Reference, Reference]:
    """The shape the pilot reads: one file, a plain reference and a gridded one beside it."""
    plain = Reference.objects.create(
        creator=creator, title="a", sha256=DIGEST, frame_fps=1, duration_s=60, pipeline=PIPELINE
    )
    gridded = Reference.objects.create(
        creator=creator,
        title="a",
        sha256=DIGEST,
        frame_fps=grid_fps,
        duration_s=60,
        pipeline=CROP_GRID_PIPELINE,
    )
    for second in SECONDS:
        grid = crop_grid.build(master(second))
        assert grid is not None
        ReferenceFrameHash.objects.create(
            reference=plain,
            frame_index=second,
            timestamp_s=float(second),
            pdq=grid.base.hash,
            quality=grid.base.quality,
        )
        ReferenceFrameHash.objects.create(
            reference=gridded,
            frame_index=second,
            timestamp_s=float(second),
            pdq=grid.base.hash,
            quality=grid.base.quality,
            crop_grid=grid.blob,
        )
    return plain, gridded


def confirm(
    reference: Reference, page: Page, status: str = ReferenceMatch.Status.CONFIRMED
) -> ReferenceMatch:
    """A human verdict on a match, which is what criterion 3 counts."""
    return ReferenceMatch.objects.create(
        reference=reference,
        page=page,
        status=status,
        decided_by="human",
        best_distance=8,
        best_reference_timestamp_s=0.0,
        matched_frames=2,
        evidence_frames=2,
        page_frames=3,
    )


def reframed_page(site: Site, slug: str) -> Page:
    page = Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")
    for second in SECONDS:
        media = Media.objects.create(
            page=page,
            url=f"https://{site.domain}/{slug}_{second}.jpg",
            kind=Media.Kind.FRAME,
            status=Media.Status.DONE,
        )
        hashed = hash_image(page_copy(master(second), REFRAME))
        FrameHash.objects.create(
            media=media, frame_index=second, pdq=hashed.hash, quality=hashed.quality
        )
    return page


def test_an_empty_library_is_not_measured_and_is_never_a_pass() -> None:
    measurement = pilot.measure()
    assert measurement.files == 0
    assert not measurement.recall_passes
    assert measurement.verdict().startswith("NOT MEASURED")
    assert "ADOPT" not in measurement.verdict()


def test_a_gridded_original_without_near_miss_pages_is_not_measured(
    creator: Creator, site: Site
) -> None:
    make_pair(creator)
    measurement = pilot.measure()
    assert measurement.files == 1
    assert measurement.near_miss == 0
    assert "NOT MEASURED" in measurement.verdict()


def test_recall_is_measured_against_the_plain_reference_of_the_same_file(
    creator: Creator, site: Site
) -> None:
    """The comparison needs no snapshot: both rows of the file are live at the same time."""
    plain, gridded = make_pair(creator)
    page = reframed_page(site, "a")

    today = pilot.plain_distances([plain.pk])
    withgrid = pilot.grid_distances(gridded.pk)
    assert pilot.NEAR_MISS_MIN <= today[page.pk] <= pilot.NEAR_MISS_MAX
    assert withgrid[page.pk] <= pilot.RECOVERY_DISTANCE

    measurement = pilot.measure()
    assert (measurement.near_miss, measurement.near_miss_recovered) == (1, 1)
    assert measurement.recall_passes
    # Criterion 3 has no human verdict yet, so this is still not a pass.
    assert measurement.verdict().startswith("NOT MEASURED")
    assert measurement.decidable is False


def test_every_criterion_met_reads_adopt_and_demands_recalibration(
    creator: Creator, site: Site
) -> None:
    _, gridded = make_pair(creator)
    page = reframed_page(site, "a")
    confirm(gridded, page)

    measurement = pilot.measure()

    assert measurement.recall_passes and measurement.harm_passes and measurement.precision_passes
    assert measurement.decidable
    verdict = measurement.verdict()
    assert verdict.startswith("ADOPT")
    assert "calibrate_confidence" in verdict


def test_a_rejected_majority_of_new_proposals_withdraws_the_grid(
    creator: Creator, site: Site
) -> None:
    _, gridded = make_pair(creator)
    reframed_page(site, "a")
    for slug in ("b", "c"):
        other = Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")
        confirm(gridded, other, status=ReferenceMatch.Status.REJECTED)
    confirm(gridded, Page.objects.get(url=f"https://{site.domain}/a"))

    measurement = pilot.measure()

    assert measurement.recall_passes
    assert not measurement.precision_passes
    assert measurement.verdict().startswith("WITHDRAW")


def test_failing_recall_says_so_and_stops_rather_than_widening_anything(
    creator: Creator, site: Site
) -> None:
    """ADR 0039: fail criterion 1 and the reference-side case is unproven — say so and stop."""
    plain, _ = make_pair(creator)
    # A near-miss page the grid does not reach: a different picture, re-framed.
    page = Page.objects.create(site=site, title="far", url=f"https://{site.domain}/far")
    media = Media.objects.create(
        page=page,
        url=f"https://{site.domain}/far_0.jpg",
        kind=Media.Kind.FRAME,
        status=Media.Status.DONE,
    )
    # Flip enough bits of the moment's own hash to land in the near-miss band and stay there.
    moment = ReferenceFrameHash.objects.get(reference=plain, frame_index=0)
    base = bytes(moment.pdq)
    far = bytes(value ^ (0xFF if index < 5 else 0) for index, value in enumerate(base))
    FrameHash.objects.create(media=media, frame_index=0, pdq=far, quality=90)

    measurement = pilot.measure()

    assert measurement.near_miss == 1
    assert measurement.near_miss_recovered == 0
    verdict = measurement.verdict()
    assert verdict.startswith("UNPROVEN — STOP")
    assert "Do not widen" in verdict


def test_the_selection_prints_identifiers_and_counts_only(creator: Creator, site: Site) -> None:
    """Staff output about originals: reference ids, digests and counts — never a title."""
    for index in range(3):
        reference = Reference.objects.create(
            creator=creator,
            title=f"A Very Recognisable Title {index}",
            sha256=hashlib.sha256(str(index).encode()).hexdigest(),
            frame_fps=1,
            duration_s=60,
            pipeline=PIPELINE,
        )
        ReferenceFrameHash.objects.create(
            reference=reference, frame_index=0, timestamp_s=0.0, pdq=bytes([index]) * 32, quality=90
        )

    lines = pilot.selection_lines(limit=2)
    text = "\n".join(lines)

    assert "A Very Recognisable Title" not in text
    chosen, cost, whole = pilot.recommend(limit=2)
    assert len(chosen) == 2
    assert cost.originals == 2 and cost.moments == 2
    assert whole.originals == 3
    assert cost.stage1_hashes == 2 * crop_grid.STAGE1_CROPS
    assert cost.blob_bytes == 2 * crop_grid.blob_bytes()
    assert "Whole library" in text


def test_selection_ranks_by_confirmed_copies_plus_near_miss_pages(
    creator: Creator, site: Site
) -> None:
    plain, gridded = make_pair(creator)
    reframed_page(site, "a")
    quiet = Reference.objects.create(
        creator=creator,
        title="quiet",
        sha256=hashlib.sha256(b"quiet").hexdigest(),
        frame_fps=1,
        duration_s=60,
        pipeline=PIPELINE,
    )
    ReferenceFrameHash.objects.create(
        reference=quiet, frame_index=0, timestamp_s=0.0, pdq=b"\x11" * 32, quality=90
    )

    ranked = pilot.candidates()

    # One entry per FILE, and the gridded file is not a candidate at all: it already has what
    # the pilot would produce, on every row of it.
    assert {item.sha256 for item in ranked} == {quiet.sha256}
    assert len(ranked) == 1
    assert ranked[0].reference_id == quiet.pk
    assert ranked[0].score == 0

    # Without the grid, that file would rank first on its near-miss page.
    ReferenceFrameHash.objects.filter(reference=gridded).update(crop_grid=None)
    ranked = pilot.candidates()
    assert [item.sha256 for item in ranked] == [plain.sha256, quiet.sha256]
    # The newest row of the file is the one named, and both rows' counts are merged into it.
    assert ranked[0].reference_id == gridded.pk
    assert ranked[0].near_miss_pages == 1


def hashed_page(site: Site, slug: str, pdq: bytes) -> Page:
    """A page carrying exactly one frame hash of our choosing, so its distance is arithmetic."""
    page = Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")
    media = Media.objects.create(
        page=page,
        url=f"https://{site.domain}/{slug}_0.jpg",
        kind=Media.Kind.FRAME,
        status=Media.Status.DONE,
    )
    FrameHash.objects.create(media=media, frame_index=0, pdq=pdq, quality=90)
    return page


def flipped(value: bytes, bits: int) -> bytes:
    """`value` with exactly `bits` of its bits inverted, so the PDQ distance is known."""
    out = bytearray(value)
    for position in range(bits):
        out[position // 8] ^= 1 << (position % 8)
    return bytes(out)


def moment_hash(reference: Reference) -> bytes:
    return bytes(ReferenceFrameHash.objects.get(reference=reference, frame_index=0).pdq)


# --- ADR 0039 amendment 2026-09-18: the instrument reads the matcher's own threshold ---


def test_both_yardsticks_are_the_matchers_threshold_and_not_a_literal() -> None:
    """The fault the amendment fixes: a local 31 while the matcher had moved to 36.

    The first run measured criterion 2 against a threshold the matcher had not used for a day and
    reported 7 confirmed pages lost where the real number was 0. This fails the moment the two
    diverge again, exactly as `matching/tests/test_browser_parity_thresholds.py` does for the
    browser parity harness.
    """
    assert pilot.MATCH_DISTANCE == STRICT_MAX_DISTANCE
    assert pilot.RECOVERY_DISTANCE == STRICT_MAX_DISTANCE
    assert pilot.NEAR_MISS_MIN == STRICT_MAX_DISTANCE + 1

    source = Path(pilot.__file__).read_text(encoding="utf-8")
    for name in ("MATCH_DISTANCE", "RECOVERY_DISTANCE", "NEAR_MISS_MIN"):
        assert re.search(rf"^{name}\s*=\s*\d", source, re.MULTILINE) is None, (
            f"{name} must derive from matching.references.STRICT_MAX_DISTANCE, never a literal"
        )
    # The bars themselves are pre-registered and stay literals: this test must not hide a change.
    assert (pilot.MIN_NEAR_MISS_RECOVERED, pilot.MAX_OUTWARD_DRIFT, pilot.MAX_NEW_REJECTED) == (
        0.50,
        4,
        0.20,
    )


def test_a_page_just_inside_the_threshold_is_not_a_near_miss(creator: Creator, site: Site) -> None:
    """The band starts above what the matcher takes: it cannot 'recover' an already-matched page.

    As printed, 4 of the first run's 55 near-miss pages were already inside 36 bits today and all
    four were counted as recovered.
    """
    plain, _ = make_pair(creator)
    distance = STRICT_MAX_DISTANCE - 2
    page = hashed_page(site, "inside", flipped(moment_hash(plain), distance))

    assert pilot.plain_distances([plain.pk])[page.pk] == distance

    measurement = pilot.measure()
    assert measurement.near_miss == 0
    assert measurement.matched_today == 1
    assert "NOT MEASURED" in measurement.verdict()


def test_a_confirmed_page_that_stays_matched_is_not_lost(creator: Creator, site: Site) -> None:
    """Criterion 2 is 'becomes unmatched', and the grid keeps the plain hashes as blob entry 0."""
    plain, _ = make_pair(creator)
    page = hashed_page(site, "matched", moment_hash(plain))
    confirm(plain, page)

    measurement = pilot.measure()

    assert measurement.confirmed_today == 1
    assert measurement.confirmed_lost == []
    assert measurement.confirmed_no_hash == 0
    assert measurement.harm_passes
    assert "WITHDRAW" not in measurement.verdict()


def test_a_confirmed_page_with_no_frame_hash_is_excluded_and_counted_on_its_own_line(
    creator: Creator, site: Site
) -> None:
    """3 of the first run's 7 'lost' pages had no page hash at all: unmatched before and after."""
    plain, _ = make_pair(creator)
    reframed_page(site, "near")  # keeps criterion 1 measurable
    blind = Page.objects.create(site=site, title="blind", url=f"https://{site.domain}/blind")
    confirm(plain, blind)

    measurement = pilot.measure()

    assert measurement.confirmed_no_hash == 1
    assert measurement.confirmed_today == 0
    assert measurement.confirmed_lost == []
    assert measurement.harm_passes
    text = "\n".join(measurement.lines())
    assert "no page hash: 1 — no signal can reach these" in text
    assert "WITHDRAW" not in text


def test_the_measurement_states_its_confounds(creator: Creator, site: Site) -> None:
    """A gain at a different sampling rate, and a ranking the pilot did not run on."""
    make_pair(creator, grid_fps=2)
    reframed_page(site, "a")
    other = Reference.objects.create(
        creator=creator,
        title="other",
        sha256=hashlib.sha256(b"other").hexdigest(),
        frame_fps=1,
        duration_s=60,
        pipeline=PIPELINE,
    )
    ReferenceFrameHash.objects.create(
        reference=other, frame_index=0, timestamp_s=0.0, pdq=b"\x22" * 32, quality=90
    )

    text = "\n".join(pilot.measure().lines())

    assert "2 fps" in text and "1 fps" in text
    assert "sampled at different rates" in text
    assert f"references {other.pk}" in text
    assert "carry no grid yet" in text


def test_the_command_runs_every_stage_read_only(creator: Creator, site: Site) -> None:
    make_pair(creator)
    reframed_page(site, "a")
    for stage in ("select", "plan", "measure"):
        out = StringIO()
        call_command("crop_grid_pilot", "--stage", stage, stdout=out)
        assert out.getvalue()
    assert ReferenceMatch.objects.count() == 0
