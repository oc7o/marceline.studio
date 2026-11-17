"""The claims of `matching.crop_grid`, as executable assertions.

Synthetic 1/f-noise pictures, because ffmpeg test patterns are spectrally degenerate for a DCT
hash (`docs/matching/0061-common-frames-review.md` §2.5). The recovery figures here are a sanity
floor, not the study's measurement: `docs/matching/reference-crop-grid.md` measured that on 160
real film frames.
"""

import numpy as np
import pytest
from PIL import Image

from matching import crop_grid
from matching.pdq import PdqResult, hamming_distance, hash_image


def noise_picture(seed: int, width: int = 1280, height: int = 534) -> Image.Image:
    """1/f noise: natural image statistics, which is what a DCT hash is calibrated for."""
    rng = np.random.default_rng(seed)
    fy = np.fft.fftfreq(height)[:, None]
    fx = np.fft.fftfreq(width)[None, :]
    radius = np.sqrt(fy**2 + fx**2)
    radius[0, 0] = 1.0
    spectrum = np.fft.fft2(rng.normal(size=(height, width))) / radius
    plane = np.real(np.fft.ifft2(spectrum))
    plane -= plane.min()
    plane /= max(plane.max(), 1e-9)
    grey = (plane * 235 + 10).astype(np.uint8)
    return Image.fromarray(np.stack([grey, np.roll(grey, 7, 1), np.roll(grey, 13, 0)], axis=2))


def page_copy(
    image: Image.Image,
    trims: tuple[float, float, float, float],
    width: int = 403,
    quality: int = 72,
) -> Image.Image:
    """The page transform: the site's re-frame, then 403 px (the median page width) and JPEG 72."""
    from io import BytesIO

    cropped = crop_grid.crop(image, crop_grid.Crop(*trims))
    height = max(2, round(cropped.height * width / cropped.width / 2) * 2)
    buffer = BytesIO()
    cropped.resize((width, height), Image.Resampling.BICUBIC).save(buffer, "JPEG", quality=quality)
    buffer.seek(0)
    out = Image.open(buffer)
    out.load()
    return out


def reference_picture(image: Image.Image, spec: crop_grid.Crop) -> Image.Image:
    """The reference recipe's geometry: crop the master, then scale='min(512,iw)':-2."""
    cropped = crop_grid.crop(image, spec)
    if cropped.width <= 512:
        return cropped
    height = max(2, round(cropped.height * 512 / cropped.width / 2) * 2)
    return cropped.resize((512, height), Image.Resampling.BICUBIC)


def test_lattice_sizes_are_the_documented_ones() -> None:
    assert len(crop_grid.lattice(0.06, 0.06)) == 16
    assert len(crop_grid.lattice(0.03, 0.06)) == 81
    assert len(crop_grid.lattice(0.02, 0.06)) == 256
    assert len(crop_grid.lattice(0.01, 0.06)) == 2401
    assert len(crop_grid.lattice()) == 256  # the module's default


def test_lattice_starts_at_the_zero_crop_and_stays_inside_the_box() -> None:
    grid = crop_grid.lattice()
    assert grid[0] == crop_grid.Crop(0.0, 0.0, 0.0, 0.0)
    assert len(set(grid)) == len(grid)
    for spec in grid:
        for value in spec.as_tuple():
            assert 0.0 <= value <= crop_grid.MAX_TRIM + 1e-12


def test_corner_lattice_is_the_sixteen_corners_and_a_subset_of_the_full_grid() -> None:
    corners = crop_grid.corner_lattice()
    assert len(corners) == 16
    assert set(corners) <= set(crop_grid.lattice())


def test_the_zero_crop_is_a_no_op_and_hashes_bit_identically() -> None:
    picture = noise_picture(1)
    assert crop_grid.crop(picture, crop_grid.Crop(0.0, 0.0, 0.0, 0.0)) is picture
    plain = hash_image(picture)
    first_spec, first_hash = next(iter(crop_grid.hash_grid(picture)))
    assert first_spec == crop_grid.Crop(0.0, 0.0, 0.0, 0.0)
    assert first_hash.hash == plain.hash


def test_crop_trims_the_named_edges() -> None:
    picture = noise_picture(2, width=1000, height=500)
    cropped = crop_grid.crop(picture, crop_grid.Crop(0.10, 0.20, 0.30, 0.05))
    assert cropped.width == 1000 - 100 - 300
    assert cropped.height == 500 - 100 - 25


def test_crop_refuses_to_leave_nothing() -> None:
    with pytest.raises(ValueError, match="less than 8 px"):
        crop_grid.crop(noise_picture(3, width=40, height=40), crop_grid.Crop(0.5, 0.0, 0.5, 0.0))


def test_lattice_refuses_a_step_larger_than_the_box() -> None:
    with pytest.raises(ValueError):
        crop_grid.lattice(0.10, 0.06)


def test_blob_round_trip_and_size() -> None:
    results = [PdqResult(bytes([i % 256]) * 32, 100) for i in range(5)]
    blob = crop_grid.pack(results)
    assert len(blob) == 5 * 32
    assert crop_grid.unpack(blob).shape == (5, 4)
    assert crop_grid.best_distance(results[3].hash, blob) == 0
    assert crop_grid.blob_bytes() == 256 * 32 == 8192
    assert crop_grid.blob_bytes(0.06, 0.06) == 512


def test_pack_drops_crops_below_the_quality_floor() -> None:
    results = [PdqResult(b"\x00" * 32, 100), PdqResult(b"\xff" * 32, 10)]
    assert len(crop_grid.pack(results)) == 32
    assert crop_grid.pack([PdqResult(b"\xff" * 32, 10)]) == b""


def test_prune_drops_near_duplicates_and_keeps_the_first_n() -> None:
    identical = [PdqResult(b"\x00" * 32, 100) for _ in range(5)]
    assert len(crop_grid.prune(identical)) == 1
    # keep_first survives even though the entries are identical
    assert len(crop_grid.prune(identical, keep_first=3)) == 3
    far = [PdqResult(b"\x00" * 32, 100), PdqResult(b"\xff" * 32, 100)]
    assert len(crop_grid.prune(far)) == 2


def test_prune_keeps_the_corner_prefix_of_a_real_grid() -> None:
    picture = noise_picture(4)
    corners = crop_grid.corner_lattice()
    rest = [spec for spec in crop_grid.lattice() if spec not in set(corners)]
    results = [hash_image(reference_picture(picture, spec)) for spec in corners + rest]
    kept = crop_grid.prune(results, keep_first=len(corners))
    assert kept[: len(corners)] == results[: len(corners)]
    assert len(corners) < len(kept) < len(results)


def test_stage1_intact_catches_a_low_quality_corner_shifting_the_prefix() -> None:
    good = [PdqResult(bytes([i]) * 32, 100) for i in range(4)]
    assert crop_grid.stage1_intact(good, keep_first=2)
    spoilt = [good[0], PdqResult(b"\x07" * 32, 10), good[2], good[3]]
    assert not crop_grid.stage1_intact(spoilt, keep_first=2)
    # and the reason it matters: the blob no longer begins with the two stage-1 crops
    assert crop_grid.unpack(crop_grid.pack(spoilt))[1].tobytes() != spoilt[1].hash


def test_best_distance_of_an_empty_blob_is_none_not_zero() -> None:
    assert crop_grid.best_distance(b"\x00" * 32, b"") is None


def test_unpack_refuses_a_truncated_blob() -> None:
    with pytest.raises(ValueError, match="whole number"):
        crop_grid.unpack(b"\x00" * 33)


def test_best_distance_matches_the_slow_definition() -> None:
    hashes = [PdqResult(bytes([i, i + 1]) * 16, 100) for i in range(7)]
    blob = crop_grid.pack(hashes)
    query = bytes([3, 9]) * 16
    slow = min(hamming_distance(query, h.hash) for h in hashes)
    assert crop_grid.best_distance(query, blob) == slow


def test_an_asymmetric_reframe_is_lost_by_one_hash_and_found_by_the_grid() -> None:
    """The whole point, on synthetic material: 3 % off the edges asymmetrically."""
    trims = (0.045, 0.01, 0.015, 0.055)
    lost = recovered = 0
    for seed in range(6):
        master = noise_picture(100 + seed)
        page = hash_image(page_copy(master, trims))
        plain = reference_picture(master, crop_grid.Crop(0.0, 0.0, 0.0, 0.0))
        alone = hamming_distance(page.hash, hash_image(plain).hash)
        blob = crop_grid.pack(
            [hash_image(reference_picture(master, spec)) for spec in crop_grid.lattice()]
        )
        best = crop_grid.best_distance(page.hash, blob)
        assert best is not None
        lost += alone > 31
        recovered += best <= 31
    assert lost == 6, "the re-frame must be outside the threshold for one hash"
    assert recovered == 6, "the grid must bring every one of them back inside"


def grid_zero() -> crop_grid.Crop:
    return crop_grid.Crop(0.0, 0.0, 0.0, 0.0)


def test_grid_specs_put_the_corners_first_and_cover_the_whole_lattice() -> None:
    specs = crop_grid.grid_specs()
    corners = crop_grid.corner_lattice()
    assert specs[: crop_grid.STAGE1_CROPS] == corners
    assert len(specs) == 256 == len(set(specs))
    assert set(specs) == set(crop_grid.lattice())
    assert specs[0] == grid_zero()
    # step == max_trim collapses to the corners alone, and the prefix still holds.
    assert crop_grid.grid_specs(0.06, 0.06) == corners


def test_scale_to_output_reproduces_ffmpegs_min_512_even_height_rule() -> None:
    # The odd-height case `matching.fingerprint`'s browser pipeline records: 1280x723 -> 512x290,
    # nearest even, where flooring would give 288 and a few bits of difference.
    assert crop_grid.scale_to_output(noise_picture(11, 1280, 723)).size == (512, 290)
    assert crop_grid.scale_to_output(noise_picture(12, 1280, 534)).size == (512, 214)
    # Narrower than the output width: the picture keeps its width, the height is evened.
    assert crop_grid.scale_to_output(noise_picture(13, 400, 226)).size == (400, 226)


def test_render_crops_before_scaling() -> None:
    master = noise_picture(14)
    spec = crop_grid.Crop(0.06, 0.0, 0.0, 0.0)
    assert crop_grid.render(master, spec).width == crop_grid.OUTPUT_WIDTH
    # §8.1: cropping the 512 px picture instead of the master is a different, worse hash.
    after = crop_grid.crop(crop_grid.scale_to_output(master), spec)
    assert hash_image(crop_grid.render(master, spec)).hash != hash_image(after).hash


def test_build_produces_the_documented_blob_and_the_zero_crop_as_its_base() -> None:
    master = noise_picture(15)
    grid = crop_grid.build(master)
    assert grid is not None
    assert grid.generated == grid.kept == len(grid) == 256
    assert len(grid.blob) == 256 * 32 == 8192 == crop_grid.blob_bytes()
    assert grid.dedup_bits is None
    # The base hash is entry 0 of the blob, so a stored row and its grid can never disagree.
    assert grid.base.hash == grid.blob[:32]
    assert grid.base.hash == hash_image(crop_grid.render(master, grid_zero())).hash
    assert crop_grid.best_distance(grid.base.hash, grid.blob) == 0


def test_build_refuses_a_source_below_the_measured_minimum_width() -> None:
    with pytest.raises(ValueError, match="1024 px wide"):
        crop_grid.build(noise_picture(16, width=800, height=450))


def test_build_with_dedup_keeps_the_corner_prefix_and_shrinks_the_blob() -> None:
    master = noise_picture(17)
    whole = crop_grid.build(master)
    pruned = crop_grid.build(master, dedup_bits=crop_grid.DEDUP_BITS)
    assert whole is not None and pruned is not None
    assert pruned.dedup_bits == 24
    assert crop_grid.STAGE1_CROPS < pruned.kept < whole.kept
    assert len(pruned.blob) == pruned.kept * 32
    assert pruned.blob[: crop_grid.STAGE1_CROPS * 32] == whole.blob[: crop_grid.STAGE1_CROPS * 32]


def test_build_returns_no_grid_at_all_when_the_quality_floor_would_shift_the_prefix() -> None:
    """A near-blank master: every crop is below the floor, so there is no honest prefix."""
    assert crop_grid.build(Image.new("RGB", (1280, 534), (7, 7, 7))) is None


def test_corner_hashes_are_the_stage_one_prefix() -> None:
    master = noise_picture(18)
    grid = crop_grid.build(master)
    assert grid is not None
    corners = crop_grid.corner_hashes(grid.blob)
    assert corners[0] == grid.base.hash
    assert corners == [
        hash_image(crop_grid.render(master, spec)).hash for spec in crop_grid.corner_lattice()
    ]


def test_corner_hashes_refuses_a_blob_too_short_to_hold_the_prefix() -> None:
    with pytest.raises(ValueError, match="stage-1 crops"):
        crop_grid.corner_hashes(b"\x00" * 32)


def test_best_distances_matches_best_distance_per_query() -> None:
    hashes = [PdqResult(bytes([i, 255 - i]) * 16, 100) for i in range(9)]
    blob = crop_grid.pack(hashes)
    queries = [bytes([3, 9]) * 16, bytes([200, 1]) * 16]
    batched = crop_grid.best_distances(
        np.frombuffer(b"".join(queries), dtype=np.uint64).reshape(-1, 4), blob
    )
    assert batched is not None
    assert batched.tolist() == [crop_grid.best_distance(value, blob) for value in queries]
    empty = crop_grid.best_distances(np.empty((0, 4), dtype=np.uint64), blob)
    assert empty is not None and empty.tolist() == []
    assert crop_grid.best_distances(np.zeros((1, 4), dtype=np.uint64), b"") is None


def test_the_stage_one_corners_find_a_reframe_the_full_grid_then_decides() -> None:
    """The two-stage split of §8.2, in miniature: corners at radius 64 find it, the blob decides."""
    trims = (0.045, 0.01, 0.015, 0.055)
    found = decided = 0
    for seed in range(6):
        master = noise_picture(300 + seed)
        page = hash_image(page_copy(master, trims))
        grid = crop_grid.build(master)
        assert grid is not None
        corners = crop_grid.pack(
            [PdqResult(value, 100) for value in crop_grid.corner_hashes(grid.blob)]
        )
        stage1 = crop_grid.best_distance(page.hash, corners)
        stage2 = crop_grid.best_distance(page.hash, grid.blob)
        assert stage1 is not None and stage2 is not None
        found += stage1 <= crop_grid.STAGE1_RADIUS
        decided += stage2 <= 31
    assert found == 6, "stage 1 must reach every re-frame the full grid can decide"
    assert decided == 6


def test_the_grid_does_not_pull_an_unrelated_picture_closer_to_the_threshold() -> None:
    """The precision claim, in miniature: a grid of 256 chances must not reach an unrelated page."""
    for seed in range(4):
        master = noise_picture(200 + seed)
        other = hash_image(page_copy(noise_picture(900 + seed), (0.0, 0.0, 0.0, 0.0)))
        blob = crop_grid.pack(
            [hash_image(reference_picture(master, spec)) for spec in crop_grid.lattice()]
        )
        best = crop_grid.best_distance(other.hash, blob)
        assert best is not None and best > 64, f"unrelated picture at {best} bits"
