"""The two-stage crop-grid search against the database (ADR 0039 §8.2).

Stage 1 is the 16 corner crops in the exact FAISS/numpy index at radius 64; stage 2 reads the
candidate moment's blob and takes the exact minimum over all 256 crops. The claims kept here are
the ones a refactor could quietly break: the prefix is really what goes into the index, the blob is
really what decides, an empty grid reads as "no information" rather than distance 0, and a moment
stage 1 never reaches is never read.
"""

from collections.abc import Callable

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from PIL import Image

from catalog.models import Creator
from matching import crop_grid, crop_index
from matching.fingerprint import CROP_GRID_PIPELINE
from matching.hamming import to_words
from matching.models import Reference, ReferenceFrameHash
from matching.pdq import hamming_distance, hash_image
from matching.tests.test_crop_grid import noise_picture, page_copy

pytestmark = pytest.mark.django_db

Moment = tuple[Reference, ReferenceFrameHash, Image.Image]
MakeMoment = Callable[..., Moment]

REFRAME = (0.045, 0.01, 0.015, 0.055)


@pytest.fixture
def make_moment() -> MakeMoment:
    creator = Creator.objects.create(name="Grid Original", needs_review=False)

    def build(seed: int, *, frame_index: int = 0, reference: Reference | None = None) -> Moment:
        if reference is None:
            reference = Reference.objects.create(
                creator=creator,
                title=f"original {seed}",
                duration_s=60.0,
                sha256=f"{seed:064x}",
                frame_fps=1.0,
                pipeline=CROP_GRID_PIPELINE,
            )
        master = noise_picture(seed)
        grid = crop_grid.build(master)
        assert grid is not None
        row = ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=frame_index,
            timestamp_s=float(frame_index),
            pdq=grid.base.hash,
            quality=grid.base.quality,
            crop_grid=grid.blob,
        )
        return reference, row, master

    return build


def test_stage_one_loads_only_the_corner_prefix(make_moment: MakeMoment) -> None:
    reference, row, _ = make_moment(41)
    stage1 = crop_index.load_stage1()

    assert stage1.moments == 1
    assert len(stage1) == crop_grid.STAGE1_CROPS
    assert stage1.words.tobytes() == b"".join(crop_grid.corner_hashes(bytes(row.crop_grid or b"")))
    # Side columns are one row per moment, not one per corner hash (29 MB saved at scale).
    assert stage1.reference_id.tolist() == [reference.pk]
    assert stage1.row_pk.tolist() == [row.pk]
    assert [stage1.moment_of(i) for i in range(crop_grid.STAGE1_CROPS)] == [0] * 16
    assert stage1.ram_bytes() > 0


def test_a_reframed_page_frame_is_found_by_the_corners_and_decided_by_the_blob(
    make_moment: MakeMoment,
) -> None:
    reference, row, master = make_moment(42)
    page = hash_image(page_copy(master, REFRAME))
    words = to_words([page.hash])

    # Today: the stored (uncropped) hash is outside the threshold.
    assert hamming_distance(page.hash, bytes(row.pdq)) > 31

    hits = crop_index.search(crop_index.load_stage1(), words, radius=31)
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.position, hit.row_pk, hit.reference_id) == (0, row.pk, reference.pk)
    assert hit.distance <= 31
    assert hit.timestamp_s == 0.0 and hit.shared is False
    # And the distance really is the blob's minimum, not something stage 1 reported.
    assert hit.distance == crop_grid.best_distance(page.hash, bytes(row.crop_grid or b""))


def test_an_unrelated_picture_produces_no_hit(make_moment: MakeMoment) -> None:
    make_moment(43)
    other = hash_image(page_copy(noise_picture(943), (0.0, 0.0, 0.0, 0.0)))
    assert crop_index.search(crop_index.load_stage1(), to_words([other.hash]), radius=31) == []


def test_the_decision_radius_is_the_matchers_not_stage_ones(
    make_moment: MakeMoment,
) -> None:
    """Stage 1 searches at 64; nothing beyond `radius` may come back as a hit."""
    _, _, master = make_moment(44)
    page = hash_image(page_copy(master, REFRAME))
    stage1 = crop_index.load_stage1()
    wide = crop_index.search(stage1, to_words([page.hash]), radius=64)
    assert wide and wide[0].distance <= 64
    tight = crop_index.search(stage1, to_words([page.hash]), radius=0)
    assert tight == []


def test_a_moment_whose_grid_is_empty_is_no_information_not_distance_zero(
    make_moment: MakeMoment,
) -> None:
    _, row, _ = make_moment(45)
    # A blob of the right shape whose crops are all below the floor cannot exist through `build`,
    # so this is the defensive path: `pack` dropped everything and left nothing to compare.
    ReferenceFrameHash.objects.filter(pk=row.pk).update(crop_grid=None)
    assert crop_index.load_stage1().moments == 0
    assert crop_index.search(crop_index.load_stage1(), to_words([bytes(32)]), radius=64) == []


def test_only_moments_stage_one_named_have_their_blobs_read(make_moment: MakeMoment) -> None:
    """The cost model of §8.2: stage 2 is a few thousand blob reads, not the whole library."""
    _, hit_row, master = make_moment(46)
    for seed in range(47, 52):
        make_moment(seed)
    page = hash_image(page_copy(master, REFRAME))
    stage1 = crop_index.load_stage1()
    assert stage1.moments == 6

    with CaptureQueriesContext(connection) as queries:
        hits = crop_index.search(stage1, to_words([page.hash]), radius=31)
    assert len(queries) == 1
    assert [hit.row_pk for hit in hits] == [hit_row.pk]


def test_references_can_be_narrowed_and_coverage_reports_what_is_there(
    make_moment: MakeMoment,
) -> None:
    first, _, _ = make_moment(53)
    second, _, _ = make_moment(54)
    make_moment(55, frame_index=1, reference=second)

    assert crop_index.coverage() == (3, 2)
    assert crop_index.coverage([first.pk]) == (1, 1)
    assert crop_index.load_stage1([second.pk]).moments == 2


def test_the_setting_is_off_by_default() -> None:
    assert crop_index.enabled() is False
