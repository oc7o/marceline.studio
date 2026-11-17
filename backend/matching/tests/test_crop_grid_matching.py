"""`match_references` with the crop grid: off changes nothing, on turns a near miss into evidence.

ADR 0039 §7. The grid is the only signal of the three that is **evidence** rather than a candidate
generator — a crop of a moment is a PDQ hash of the original at that moment, in PDQ's own units —
so the thing worth pinning down is that it enters through the ordinary evidence path and that a
run with `MATCHING_REFERENCE_CROP_GRID` off is bit-for-bit today's run.

The re-frame here is synthetic (`matching/tests/test_crop_grid.py` builds the pictures); the real
measurement is `docs/matching/reference-crop-grid.md` on 160 film frames.
"""

import hashlib

import pytest
from PIL import Image
from pytest_django.fixtures import Settings

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching import crop_grid
from matching.fingerprint import CROP_GRID_PIPELINE
from matching.models import (
    MatchRunState,
    Reference,
    ReferenceCandidate,
    ReferenceFrameHash,
    ReferenceMatch,
)
from matching.pdq import hamming_distance, hash_image
from matching.references import match_references
from matching.tests.test_crop_grid import noise_picture, page_copy

pytestmark = pytest.mark.django_db

#: The asymmetric 1-3 % trim the study measured on real sites, as a fixed synthetic transform.
REFRAME = (0.045, 0.01, 0.015, 0.055)
SECONDS = (0, 1, 2)


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="grid.test", enabled=True)


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Grid Creator", needs_review=False)


def master(second: int) -> Image.Image:
    return noise_picture(1000 + second)


def gridded_reference(creator: Creator, *, with_grid: bool) -> Reference:
    """One original, three moments, hashed by the crop-grid recipe.

    `with_grid=False` stores the same PDQ hashes and no blobs: the "before" state, and the proof
    that what changes below is the grid and not the pictures.
    """
    reference = Reference.objects.create(
        creator=creator,
        title="original",
        sha256=hashlib.sha256(b"grid-original").hexdigest(),
        frame_fps=1,
        duration_s=60,
        pipeline=CROP_GRID_PIPELINE,
    )
    for second in SECONDS:
        grid = crop_grid.build(master(second))
        assert grid is not None
        ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=second,
            timestamp_s=float(second),
            pdq=grid.base.hash,
            quality=grid.base.quality,
            crop_grid=grid.blob if with_grid else None,
        )
    return reference


def reframed_page(site: Site, slug: str) -> Page:
    """A page whose hover frames are the same moments, re-framed the way a site re-frames them."""
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


def test_the_reframe_is_outside_the_threshold_without_the_grid(
    site: Site, creator: Creator
) -> None:
    """The premise: these pages are exactly the near misses the study is about."""
    reference = gridded_reference(creator, with_grid=True)
    page = reframed_page(site, "a")
    stored = {row.frame_index: bytes(row.pdq) for row in reference.frames.all()}
    for frame in FrameHash.objects.filter(media__page=page):
        assert hamming_distance(bytes(frame.pdq), stored[frame.frame_index]) > 31


def test_with_the_setting_off_the_grid_changes_nothing(site: Site, creator: Creator) -> None:
    reference = gridded_reference(creator, with_grid=True)
    reframed_page(site, "a")

    match_references([reference], full=True)

    assert not ReferenceMatch.objects.filter(reference=reference).exists()
    assert not ReferenceCandidate.objects.filter(reference=reference).exists()


def test_with_the_setting_on_a_reframed_page_becomes_ordinary_pdq_evidence(
    site: Site, creator: Creator, settings: Settings
) -> None:
    reference = gridded_reference(creator, with_grid=True)
    page = reframed_page(site, "a")
    settings.MATCHING_REFERENCE_CROP_GRID = True

    match_references([reference], full=True)

    match = ReferenceMatch.objects.get(reference=reference, page=page)
    assert match.best_distance <= 31
    # Evidence, not a candidate: a real strength, real evidence frames, the PDQ source.
    assert match.evidence_frames >= 2
    assert match.strength == "strong"
    assert match.candidate_source == ReferenceMatch.CandidateSource.PDQ
    # Every crop of a moment carries that moment's own second, so the timeline still works.
    assert match.matched_seconds == [float(second) for second in SECONDS]
    assert match.timeline_score == 1.0


def test_a_reference_without_a_grid_is_untouched_even_with_the_setting_on(
    site: Site, creator: Creator, settings: Settings
) -> None:
    """A library with no grids behaves exactly as today, switch or no switch."""
    reference = gridded_reference(creator, with_grid=False)
    reframed_page(site, "a")
    settings.MATCHING_REFERENCE_CROP_GRID = True

    match_references([reference], full=True)

    assert not ReferenceMatch.objects.filter(reference=reference).exists()


def test_an_unrelated_page_is_not_matched_by_the_grid(
    site: Site, creator: Creator, settings: Settings
) -> None:
    """256 chances per moment must not reach a picture that is not the original."""
    reference = gridded_reference(creator, with_grid=True)
    page = Page.objects.create(site=site, title="other", url=f"https://{site.domain}/other")
    for second in SECONDS:
        media = Media.objects.create(
            page=page,
            url=f"https://{site.domain}/other_{second}.jpg",
            kind=Media.Kind.FRAME,
            status=Media.Status.DONE,
        )
        hashed = hash_image(page_copy(noise_picture(5000 + second), REFRAME))
        FrameHash.objects.create(
            media=media, frame_index=second, pdq=hashed.hash, quality=hashed.quality
        )
    settings.MATCHING_REFERENCE_CROP_GRID = True

    match_references([reference], full=True)

    assert not ReferenceMatch.objects.filter(reference=reference).exists()


def test_switching_the_grid_on_forces_one_full_rescan(
    site: Site, creator: Creator, settings: Settings
) -> None:
    """ADR 0039's stated cost: the scan fingerprint changes, so the cache is rebuilt once."""
    reference = gridded_reference(creator, with_grid=True)
    reframed_page(site, "a")

    match_references([reference], full=True)
    before = MatchRunState.objects.get(pk=1)
    assert before.settings["crop_grid"] is False

    settings.MATCHING_REFERENCE_CROP_GRID = True
    output = match_references([reference])

    after = MatchRunState.objects.get(pk=1)
    assert after.settings["crop_grid"] is True
    assert after.fingerprint != before.fingerprint
    assert after.last_mode == "full"
    assert any("Scan: full" in line and "crop grid:" in line for line in output)
