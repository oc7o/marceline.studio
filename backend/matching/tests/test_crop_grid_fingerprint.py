"""Producing crop grids: the client recipe, format version 5 and the import (ADR 0039, 0022).

The claims worth keeping executable here are the ones that would be silent if they broke: the
crops come from the **master** frame and not from the 512 px output (§8.1 measured 8 bits median
lost the other way), the stored hash is entry 0 of the blob, the file carries its own pipeline tag
so it imports *alongside* the plain reference rather than replacing it, and the strict reader
refuses every blob it cannot read by position.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from catalog.models import Creator
from matching import crop_grid
from matching.fingerprint import (
    CROP_GRID_ORIENTATION,
    CROP_GRID_PIPELINE,
    FORMAT_VERSION,
    PIPELINE,
    CropGrid,
    FingerprintFormatError,
    fingerprint_video,
    from_json,
    to_json,
)
from matching.frames import iter_video_frames
from matching.models import Reference, ReferenceFrameHash
from matching.references import import_fingerprint
from matching.tests.test_frames import make_video

CLIENT = Path(__file__).resolve().parents[3] / "client" / "fingerprint.py"


def load_client() -> ModuleType:
    spec = importlib.util.spec_from_file_location("marceline_fingerprint_client", CLIENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Wide enough for the grid (>= MIN_SOURCE_WIDTH) and short enough to hash in a test.
WIDE = "testsrc2=duration=2:size=1280x534:rate=10"
NARROW = "testsrc2=duration=2:size=640x360:rate=10"
#: 256 crops of every sampled second is seconds of CPU per frame, so the tests use the 16-crop
#: lattice (step == box): same code path, same layout, one fifteenth of the work.
TINY = CropGrid(crop_grid.MAX_TRIM, crop_grid.MAX_TRIM, crop_grid.STAGE1_CROPS)


@pytest.fixture
def wide_original(tmp_path: Path) -> Path:
    make_video(tmp_path, WIDE)
    return tmp_path / "clip.mp4"


def test_a_grid_run_carries_its_own_pipeline_tag_and_lattice(wide_original: Path) -> None:
    fingerprint = fingerprint_video(wide_original, fps=1.0, dihedral=False, grid=TINY)

    assert fingerprint.pipeline == CROP_GRID_PIPELINE != PIPELINE
    assert fingerprint.crop_grid == TINY
    blobs = [frame.crop_grid for frame in fingerprint.frames if frame.crop_grid]
    assert len(blobs) == len(fingerprint.frames)
    for blob in blobs:
        assert len(blob) == crop_grid.STAGE1_CROPS * 32


def test_the_stored_hash_is_entry_zero_of_the_blob(wide_original: Path) -> None:
    fingerprint = fingerprint_video(wide_original, fps=1.0, dihedral=False, grid=TINY)
    for frame in fingerprint.frames:
        assert frame.crop_grid is not None
        assert frame.crop_grid[:32] == frame.pdq


def test_only_the_unflipped_orientation_carries_a_grid(wide_original: Path) -> None:
    """The dihedral expansion answers a flip, the lattice a re-frame: they do not multiply."""
    fingerprint = fingerprint_video(wide_original, fps=1.0, dihedral=True, grid=TINY)
    with_grid = [frame for frame in fingerprint.frames if frame.crop_grid is not None]
    assert with_grid
    assert {frame.dihedral for frame in with_grid} == {CROP_GRID_ORIENTATION}
    assert len(fingerprint.frames) == 8 * len(with_grid)


def test_the_crops_come_from_the_master_frame_not_from_the_512_px_output(
    wide_original: Path,
) -> None:
    """§8.1: cropping the output instead of the master costs a median of 8 bits, p90 12, max 34."""
    fingerprint = fingerprint_video(wide_original, fps=1.0, dihedral=False, grid=TINY)
    masters = dict(iter_video_frames(wide_original, fps=1.0, max_width=None))
    outputs = dict(iter_video_frames(wide_original, fps=1.0))
    assert masters[0].width == 1280 and outputs[0].width == 512

    spec = crop_grid.Crop(crop_grid.MAX_TRIM, 0.0, 0.0, 0.0)
    position = crop_grid.grid_specs(TINY.step, TINY.max_trim).index(spec)
    from matching.pdq import hash_image

    stored = fingerprint.frames[0].crop_grid
    assert stored is not None
    assert (
        stored[position * 32 : (position + 1) * 32]
        == hash_image(crop_grid.render(masters[0], spec)).hash
    )
    # The cheap way round really is a different hash, which is why the detour exists.
    assert (
        stored[position * 32 : (position + 1) * 32]
        != hash_image(crop_grid.crop(outputs[0], spec)).hash
    )


def test_a_master_below_the_measured_minimum_width_fails_the_run(tmp_path: Path) -> None:
    make_video(tmp_path, NARROW)
    with pytest.raises(ValueError, match="1024 px wide"):
        fingerprint_video(tmp_path / "clip.mp4", fps=1.0, dihedral=False, grid=TINY)


def test_a_plain_run_is_untouched(wide_original: Path) -> None:
    """The grid is opt-in end to end: without it, the same file hashes exactly as it did."""
    fingerprint = fingerprint_video(wide_original, fps=1.0, dihedral=False)
    assert fingerprint.pipeline == PIPELINE
    assert fingerprint.crop_grid is None
    assert all(frame.crop_grid is None for frame in fingerprint.frames)


def test_dedup_is_recorded_in_the_file_and_survives_the_round_trip(
    wide_original: Path,
) -> None:
    """§3.3, optional and off by default: it halves the blob for 0.03 of recall at 31 bits."""
    pruned = CropGrid(crop_grid.STEP, crop_grid.MAX_TRIM, crop_grid.STAGE1_CROPS, dedup_bits=24)
    fingerprint = fingerprint_video(wide_original, fps=1.0, dihedral=False, grid=pruned)

    blobs = [frame.crop_grid for frame in fingerprint.frames if frame.crop_grid]
    assert blobs
    for blob in blobs:
        # The prefix always survives pruning; the tail does not.
        assert crop_grid.STAGE1_CROPS * 32 <= len(blob) < 256 * 32
    assert from_json(to_json(fingerprint)).crop_grid == pruned


def test_the_json_round_trip_keeps_every_blob(wide_original: Path) -> None:
    fingerprint = fingerprint_video(wide_original, fps=1.0, dihedral=False, grid=TINY)
    document = to_json(fingerprint)

    assert document["version"] == FORMAT_VERSION == 5
    assert document["crop_grid"] == TINY.as_json()
    back = from_json(json.loads(json.dumps(document)))
    assert back.crop_grid == TINY
    assert [frame.crop_grid for frame in back.frames] == [
        frame.crop_grid for frame in fingerprint.frames
    ]


def gridded_document(wide_original: Path) -> dict[str, object]:
    return to_json(fingerprint_video(wide_original, fps=1.0, dihedral=False, grid=TINY))


def test_a_blob_without_its_lattice_is_refused(wide_original: Path) -> None:
    document = gridded_document(wide_original)
    document["crop_grid"] = None
    document["pipeline"] = PIPELINE
    with pytest.raises(FingerprintFormatError, match="names no lattice"):
        from_json(document)


def test_a_lattice_on_a_pipeline_that_does_not_produce_one_is_refused(
    wide_original: Path,
) -> None:
    document = gridded_document(wide_original)
    document["pipeline"] = PIPELINE
    with pytest.raises(FingerprintFormatError, match="does not produce crop grids"):
        from_json(document)


def test_a_blob_that_is_not_whole_hashes_is_refused(wide_original: Path) -> None:
    document = gridded_document(wide_original)
    frames = document["frames"]
    assert isinstance(frames, list)
    frames[0]["crop_grid"] = frames[0]["crop_grid"][:-2]
    with pytest.raises(FingerprintFormatError, match="whole number"):
        from_json(document)


def test_a_blob_shorter_than_the_stage_one_prefix_is_refused(wide_original: Path) -> None:
    document = gridded_document(wide_original)
    frames = document["frames"]
    assert isinstance(frames, list)
    frames[0]["crop_grid"] = frames[0]["crop_grid"][: 64 * (crop_grid.STAGE1_CROPS - 1)]
    with pytest.raises(FingerprintFormatError, match="stage-1 crops"):
        from_json(document)


def test_a_blob_whose_first_entry_is_not_the_stored_hash_is_refused(
    wide_original: Path,
) -> None:
    """Stage 2 reads a blob for the moment the row names; the two may never disagree."""
    document = gridded_document(wide_original)
    frames = document["frames"]
    assert isinstance(frames, list)
    blob = frames[0]["crop_grid"]
    frames[0]["crop_grid"] = "0" * 64 + blob[64:]
    with pytest.raises(FingerprintFormatError, match="does not begin with"):
        from_json(document)


def test_a_photo_document_may_not_carry_a_lattice(wide_original: Path) -> None:
    document = gridded_document(wide_original)
    document["kind"] = "photos"
    with pytest.raises(FingerprintFormatError):
        from_json(document)


@pytest.mark.django_db
def test_importing_a_grid_file_stores_the_blobs_beside_the_plain_reference(
    wide_original: Path,
) -> None:
    """ADR 0022: a reference's identity is (sha256, pipeline, fps), so both rows coexist."""
    creator = Creator.objects.create(name="Grid Original", needs_review=False)
    plain = fingerprint_video(wide_original, fps=1.0, dihedral=False)
    gridded = fingerprint_video(wide_original, fps=1.0, dihedral=False, grid=TINY)

    first = import_fingerprint(creator=creator, fingerprint=plain, mark_shared=False)
    second = import_fingerprint(creator=creator, fingerprint=gridded, mark_shared=False)

    assert first.pk != second.pk
    assert first.sha256 == second.sha256
    assert (first.pipeline, second.pipeline) == (PIPELINE, CROP_GRID_PIPELINE)
    assert Reference.objects.filter(sha256=plain.sha256).count() == 2
    # The plain reference keeps meaning exactly what it meant: no blob, no change.
    assert not ReferenceFrameHash.objects.filter(reference=first, crop_grid__isnull=False).exists()
    stored = ReferenceFrameHash.objects.filter(reference=second, crop_grid__isnull=False)
    assert stored.count() == len(gridded.frames)
    row = stored.order_by("frame_index").first()
    assert row is not None
    assert bytes(row.crop_grid or b"")[:32] == bytes(row.pdq)


def test_the_client_refuses_the_flag_for_photos_and_a_lone_dedup_radius(
    tmp_path: Path,
) -> None:
    """The lattice answers a re-framed video moment; a photo collection is a different shape."""
    client = load_client()
    with pytest.raises(SystemExit):
        client.main(["--crop-grid", "--photos", str(tmp_path)])
    with pytest.raises(SystemExit):
        client.main(["--crop-grid-dedup", "24", str(tmp_path)])
    with pytest.raises(SystemExit):
        client.main(["--crop-grid", "--crop-grid-dedup", "0", str(tmp_path)])


def test_the_client_names_a_gridded_run_differently_on_disk() -> None:
    """A gridded file is a different recipe, so it is not skipped as work already done."""
    client = load_client()
    assert client.crop_grid_suffix(True) == ".grid"
    assert client.crop_grid_suffix(False) == ""
    options = client.crop_grid_options(True, None)
    assert options == CropGrid(crop_grid.STEP, crop_grid.MAX_TRIM, crop_grid.STAGE1_CROPS, None)
    assert client.crop_grid_options(False, None) is None
