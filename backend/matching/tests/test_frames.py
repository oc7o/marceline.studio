import io
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from matching.frames import (
    FRAME_MAX_WIDTH,
    FrameExtractionError,
    drop_near_duplicates,
    hash_poster,
    hash_sprite,
    hash_video,
    iter_video_frames,
    sample_video_frames,
    split_sprite,
)
from matching.pdq import HashedFrame, MediaDecodeError, hash_image
from matching.tests.hashes import flip_bits, random_hashes
from matching.tests.images import synthetic_image, to_bytes

TILE = (160, 90)


def make_sprite(
    tiles: list[Image.Image], cols: int, margin: tuple[int, int] = (0, 0)
) -> Image.Image:
    rows = -(-len(tiles) // cols)
    sheet = Image.new("RGB", (cols * TILE[0] + margin[0], rows * TILE[1] + margin[1]), "white")
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % cols) * TILE[0], (i // cols) * TILE[1]))
    return sheet


def make_video(tmp_path: Path, source: str) -> bytes:
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            *("ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", source),
            *("-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)),
        ],
        check=True,
        timeout=60,
    )
    return path.read_bytes()


def native_frame_hash(path: Path, index: int) -> bytes:
    """PDQ of decoded frame `index`, scaled exactly as the sampling pipeline scales it.

    The ruler for "which picture did slot k get": `select` by frame number picks one specific
    decoded picture, with no sampling grid of its own to be wrong about.
    """
    result = subprocess.run(
        [
            *("ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0"),
            *("-vf", f"select='eq(n\\,{index})',scale='min({FRAME_MAX_WIDTH},iw)':-2"),
            *("-frames:v", "1", "-c:v", "ppm", "-f", "image2pipe", "pipe:1"),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return hash_image(Image.open(io.BytesIO(result.stdout))).hash


@pytest.mark.parametrize("rate", (24, 25, 30))
def test_iter_video_frames_samples_the_picture_at_second_k(tmp_path: Path, rate: int) -> None:
    """Slot k is the picture at k seconds — the defect issue #70 fixed.

    Before `fps=…:round=up` the filter handed back the *last* picture of the [k-0.5, k+0.5) bucket
    and labelled it k, so `timestamp_s = frame_index / fps` described a moment about half a second
    earlier than the frame that was hashed. `testsrc2` changes a lot between frames, so every other
    picture of the same second hashes differently and distance 0 pins the choice exactly.
    """
    make_video(tmp_path, f"testsrc2=duration=5:size=320x240:rate={rate}")
    path = tmp_path / "clip.mp4"
    sampled = [hash_image(image).hash for _, image in iter_video_frames(path, fps=1.0)]
    assert len(sampled) >= 5
    for second, value in enumerate(sampled[:5]):
        assert value == native_frame_hash(path, second * rate), f"second {second}"
    # And it is genuinely not the frame the old recipe picked, ~0.47 s into the second.
    assert sampled[1] != native_frame_hash(path, rate + rate // 2 - 1)


@pytest.fixture
def tiles() -> list[Image.Image]:
    return [synthetic_image(seed, TILE) for seed in range(8)]


def test_split_sprite_by_grid(tiles: list[Image.Image]) -> None:
    parts = split_sprite(make_sprite(tiles, cols=4), grid=(4, 2))
    assert len(parts) == 8
    for part, tile in zip(parts, tiles, strict=True):
        assert np.array_equal(np.asarray(part), np.asarray(tile))


def test_split_sprite_by_tile_size_drops_partial_tiles(tiles: list[Image.Image]) -> None:
    sheet = make_sprite(tiles, cols=4, margin=(40, 10))
    parts = split_sprite(sheet, tile_size=TILE)
    assert len(parts) == 8
    assert np.array_equal(np.asarray(parts[5]), np.asarray(tiles[5]))


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"grid": (2, 2), "tile_size": TILE}, {"grid": (0, 1)}, {"tile_size": (0, 10)}],
)
def test_split_sprite_rejects_bad_arguments(kwargs: dict[str, tuple[int, int]]) -> None:
    with pytest.raises(ValueError):
        split_sprite(Image.new("RGB", (100, 100)), **kwargs)


def test_hash_sprite_skips_blank_tiles(tiles: list[Image.Image]) -> None:
    layout = [tiles[0], Image.new("RGB", TILE, "black"), tiles[1], tiles[2]]
    frames = hash_sprite(to_bytes(make_sprite(layout, cols=2)), grid=(2, 2))
    assert [frame.frame_index for frame in frames] == [0, 2, 3]
    assert frames[1].hash == hash_image(tiles[1]).hash


def test_hash_poster() -> None:
    [frame] = hash_poster(to_bytes(synthetic_image(1), "JPEG"))
    assert frame.frame_index == 0
    assert len(frame.hash) == 32
    assert hash_poster(to_bytes(Image.new("RGB", (64, 64), "black"))) == []
    with pytest.raises(MediaDecodeError):
        hash_poster(b"garbage")


def test_sample_video_frames(tmp_path: Path) -> None:
    video = make_video(tmp_path, "testsrc=duration=4:size=320x240:rate=10")
    assert len(sample_video_frames(video, count=4)) == 4
    assert len(sample_video_frames(video, fps=10, max_frames=5)) == 5
    assert 7 <= len(sample_video_frames(video, fps=2)) <= 9
    first = Image.open(io.BytesIO(sample_video_frames(video, count=1)[0]))
    assert first.size == (320, 240)


def test_fps_sampling_spreads_capped_frames_over_the_clip(tmp_path: Path) -> None:
    video = make_video(tmp_path, "testsrc=duration=20:size=320x240:rate=10")
    spread = sample_video_frames(video, fps=2, max_frames=32)
    assert len(spread) == 32
    short = make_video(tmp_path, "testsrc=duration=4:size=320x240:rate=10")
    assert sample_video_frames(short, fps=10, max_frames=4) == sample_video_frames(short, count=4)


def test_drop_near_duplicates_compares_with_the_previous_kept_frame() -> None:
    first, second = random_hashes(2, seed=3)
    frames = [
        HashedFrame(0, first, 90),
        HashedFrame(1, flip_bits(first, 8), 90),
        HashedFrame(2, flip_bits(flip_bits(first, 8), 8, seed=1), 90),
        HashedFrame(3, second, 90),
        HashedFrame(4, second, 90),
    ]
    kept = drop_near_duplicates(frames, max_distance=8)
    # frame 2 is within 8 bits of dropped frame 1 but not of kept frame 0
    assert [frame.frame_index for frame in kept] == [0, 2, 3]


def test_hash_video_drops_near_duplicate_frames_of_still_shots(tmp_path: Path) -> None:
    inputs = []
    for seed in (1, 2):
        still = tmp_path / f"still-{seed}.png"
        still.write_bytes(to_bytes(synthetic_image(seed)))
        inputs += ["-loop", "1", "-framerate", "10", "-t", "3", "-i", str(still)]
    clip = tmp_path / "stills.mp4"
    subprocess.run(
        [
            *("ffmpeg", "-nostdin", "-loglevel", "error", *inputs),
            *("-filter_complex", "[0:v][1:v]concat=n=2:v=1,format=yuv420p"),
            *("-c:v", "libx264", str(clip)),
        ],
        check=True,
        timeout=60,
    )
    data = clip.read_bytes()

    every_frame = hash_video(data, fps=2, max_frames=32)
    distinct = hash_video(data, fps=2, max_frames=32, max_duplicate_distance=8)

    assert len(every_frame) >= 10
    assert len(distinct) == 2
    assert distinct[0].frame_index == 0 and distinct[1].frame_index >= 5


def test_hash_video(tmp_path: Path) -> None:
    frames = hash_video(make_video(tmp_path, "testsrc=duration=3:size=640x360:rate=10"), count=3)
    assert [frame.frame_index for frame in frames] == [0, 1, 2]


def test_hash_video_skips_black_frames(tmp_path: Path) -> None:
    black = make_video(tmp_path, "color=c=black:size=320x240:duration=2")
    assert hash_video(black, count=4) == []


def test_undecodable_video() -> None:
    with pytest.raises(FrameExtractionError):
        sample_video_frames(b"definitely not a video", count=3)
    with pytest.raises(FrameExtractionError):
        sample_video_frames(b"definitely not a video", fps=1.0)
    assert issubclass(FrameExtractionError, MediaDecodeError)


def test_sample_video_frames_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        sample_video_frames(b"", fps=1, count=1)
    with pytest.raises(ValueError):
        sample_video_frames(b"", count=0)
