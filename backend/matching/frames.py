"""Turn preview media into PDQ-hashed frames: posters, thumbnail sprite sheets, preview clips.

These are the entry points for the media worker. Input bytes come from hostile sites: decode
failures raise `MediaDecodeError` (ffmpeg problems as its subclass `FrameExtractionError`),
ffmpeg runs with a timeout and may only read local files.
"""

import subprocess
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from PIL import Image

from matching.black_bars import crop_bars as crop_bars_of
from matching.pdq import (
    MIN_QUALITY,
    HashedFrame,
    MediaDecodeError,
    hamming_distance,
    hash_image,
    open_image,
)

FFMPEG_TIMEOUT_S = 60.0
MAX_VIDEO_FRAMES = 64
# Watchdog for decoding a whole local original (iter_video_frames).
FULL_VIDEO_TIMEOUT_S = 3600.0
FRAME_MAX_WIDTH = 512
# Rounding of ffmpeg's `fps` filter, and the reason the reference grid is trustworthy (issue #70).
#
# The filter assigns every input picture to the output slot `round(t * fps)` and emits the LAST
# picture that fell into a slot. With the default `round=near` the bucket for slot k is
# [k-0.5, k+0.5), so slot k gets the picture just before k+0.5 and is then labelled `pts_time = k`.
# Measured over synthetic clips (`fps=1`, scratch harness of issue #70, confirmed by ffmpeg's own
# per-frame checksums): +466.7 ms at 30 fps, +458.3 ms at 24 fps, +480.0 ms at 25 fps,
# +471.6 ms at 30000/1001, +433 ms on a VFR clip. `timestamp_s = frame_index / fps` was therefore
# about half a second early: the stored moment was not the moment of the hashed picture.
#
# `round=up` makes the bucket (k-1, k], so slot k is the last picture at or before k — the rule
# `docs/research/fingerprinting-client.md` writes down, and the one a browser's `currentTime = k`
# seek follows. Measured residual: **exactly 0.0 ms** for 24, 25 and 30 fps and for the VFR clip,
# and 0.0 ms for all 120 slots of a 120 s clip (no drift). The only residue is a frame rate that
# puts no picture on the integer second: at 30000/1001 the chosen picture is the last one before
# k, a median of 27.4 ms and at worst 32.4 ms (one frame period) early.
FPS_ROUND = "up"

# The second signal, injected (ADR 0037): one picture in, its 512-bit sketch out. Typed as a
# callable so this module needs no onnxruntime and no model — see `matching.embedding`.
Embedder = Callable[[Image.Image], bytes]


class FrameExtractionError(MediaDecodeError):
    """ffmpeg could not sample frames from a video."""


def split_sprite(
    image: Image.Image,
    *,
    grid: tuple[int, int] | None = None,
    tile_size: tuple[int, int] | None = None,
) -> list[Image.Image]:
    """Cut a sprite sheet into tiles, row by row. Give either grid=(cols, rows) or
    tile_size=(width, height); incomplete tiles at the right/bottom edge are dropped."""
    if (grid is None) == (tile_size is None):
        raise ValueError("pass exactly one of grid or tile_size")
    if grid is not None:
        cols, rows = grid
        if cols < 1 or rows < 1:
            raise ValueError("grid must be at least 1x1")
        width, height = image.width // cols, image.height // rows
    else:
        assert tile_size is not None
        width, height = tile_size
        if width < 1 or height < 1:
            raise ValueError("tile_size must be positive")
        cols, rows = image.width // width, image.height // height
    if width < 1 or height < 1:
        raise ValueError("tiles would be empty")
    return [
        image.crop((col * width, row * height, (col + 1) * width, (row + 1) * height))
        for row in range(rows)
        for col in range(cols)
    ]


def _run(command: list[str], timeout_s: float) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise FrameExtractionError(f"{command[0]} timed out after {timeout_s}s") from exc
    if result.returncode != 0:
        raise FrameExtractionError(f"{command[0]} failed: {result.stderr.strip()[-500:]}")
    return result.stdout


def _probe_duration(path: Path, timeout_s: float) -> float:
    output = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout_s,
    ).strip()
    try:
        duration = float(output)
    except ValueError as exc:
        raise FrameExtractionError(f"unknown video duration: {output!r}") from exc
    if duration <= 0:
        raise FrameExtractionError(f"invalid video duration: {duration}")
    return duration


def sample_video_frames(
    data: bytes,
    *,
    fps: float | None = None,
    count: int | None = None,
    max_frames: int = MAX_VIDEO_FRAMES,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> list[bytes]:
    """PNG bytes of frames sampled from a video: `fps` frames per second, or `count` frames
    spread evenly over the duration. With `fps`, a video longer than `max_frames / fps` seconds
    gets `max_frames` frames spread over its whole duration instead of only its beginning.
    Frames are downscaled to at most FRAME_MAX_WIDTH wide.

    This is the *page media* path (preview clips found by the crawler), and it deliberately keeps
    the filter's default rounding, unlike `iter_video_frames`. A `HashedFrame` here carries a
    position and no timestamp: a leaked preview clip has its own, unknown start, so it shares no
    second-grid with anything and `FPS_ROUND` would buy nothing. Changing it would instead
    invalidate every stored `index.FrameHash` row and every `MediaReuse` link (ADR 0022)."""
    if (fps is None) == (count is None):
        raise ValueError("pass exactly one of fps or count")
    if count is not None and count < 1:
        raise ValueError("count must be positive")
    if fps is not None and fps <= 0:
        raise ValueError("fps must be positive")
    with tempfile.TemporaryDirectory(prefix="marceline-frames-") as tmp:
        workdir = Path(tmp)
        source = workdir / "input"
        source.write_bytes(data)
        duration = _probe_duration(source, timeout_s)
        if count is not None:
            rate = count / duration
            limit = min(count, max_frames)
        else:
            assert fps is not None
            rate, limit = min(fps, max_frames / duration), max_frames
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file",
                "-i",
                str(source),
                "-vf",
                f"fps={rate:.6f},scale='min({FRAME_MAX_WIDTH},iw)':-2",
                "-frames:v",
                str(limit),
                "-f",
                "image2",
                str(workdir / "frame_%04d.png"),
            ],
            timeout_s,
        )
        return [path.read_bytes() for path in sorted(workdir.glob("frame_*.png"))]


def hash_frames(
    images: Iterable[Image.Image],
    min_quality: int = MIN_QUALITY,
    *,
    crop_bars: bool = False,
    embed: Embedder | None = None,
) -> list[HashedFrame]:
    """Hash each image, keeping its position; frames below `min_quality` (blank, near-black,
    featureless) are dropped.

    With `crop_bars`, letterbox/pillarbox padding is trimmed before hashing
    (`matching.black_bars`, docs/matching/black-bars.md). A frame without bars is hashed
    unchanged, bit for bit. The caller decides: the media worker reads
    `settings.MATCHING_CROP_BLACK_BARS`, the reference pipeline never crops (that would be a new
    pipeline tag, ADR 0022).

    `embed` computes the second signal's sketch from the very same picture, before it is
    discarded (ADR 0037). It is injected rather than imported so this module keeps needing
    nothing but Pillow, numpy and pdqhash — `client/fingerprint.py` runs it on the operator's
    Mac. A frame dropped for quality is never embedded: our own index could not hold it."""
    frames = []
    for index, image in enumerate(images):
        frame = crop_bars_of(image)[0] if crop_bars else image
        result = hash_image(frame)
        if result.quality >= min_quality:
            sketch = embed(frame) if embed is not None else None
            frames.append(HashedFrame(index, result.hash, result.quality, sketch))
    return frames


def hash_poster(
    data: bytes,
    min_quality: int = MIN_QUALITY,
    *,
    crop_bars: bool = False,
    embed: Embedder | None = None,
) -> list[HashedFrame]:
    """A single image; empty list if it is too featureless to match on."""
    return hash_frames([open_image(data)], min_quality, crop_bars=crop_bars, embed=embed)


def hash_sprite(
    data: bytes,
    *,
    grid: tuple[int, int] | None = None,
    tile_size: tuple[int, int] | None = None,
    min_quality: int = MIN_QUALITY,
    crop_bars: bool = False,
    embed: Embedder | None = None,
) -> list[HashedFrame]:
    """Every usable tile of a sprite sheet; frame_index is the tile position."""
    tiles = split_sprite(open_image(data), grid=grid, tile_size=tile_size)
    return hash_frames(tiles, min_quality, crop_bars=crop_bars, embed=embed)


def drop_near_duplicates(frames: Iterable[HashedFrame], max_distance: int) -> list[HashedFrame]:
    """Drop each frame within `max_distance` Hamming bits of the previous kept frame (a still
    shot sampled several times); kept frames keep their original frame_index."""
    kept: list[HashedFrame] = []
    for frame in frames:
        if kept and hamming_distance(frame.hash, kept[-1].hash) <= max_distance:
            continue
        kept.append(frame)
    return kept


def hash_video(
    data: bytes,
    *,
    fps: float | None = None,
    count: int | None = None,
    max_frames: int = MAX_VIDEO_FRAMES,
    min_quality: int = MIN_QUALITY,
    max_duplicate_distance: int | None = None,
    timeout_s: float = FFMPEG_TIMEOUT_S,
    crop_bars: bool = False,
    embed: Embedder | None = None,
) -> list[HashedFrame]:
    """Usable frames of a preview clip; frame_index is the sample position. With
    `max_duplicate_distance`, consecutive near-identical frames are dropped."""
    frames = sample_video_frames(
        data, fps=fps, count=count, max_frames=max_frames, timeout_s=timeout_s
    )
    hashed = hash_frames(
        (open_image(frame) for frame in frames), min_quality, crop_bars=crop_bars, embed=embed
    )
    if max_duplicate_distance is None:
        return hashed
    return drop_near_duplicates(hashed, max_duplicate_distance)


def iter_video_frames(
    path: Path,
    *,
    fps: float,
    timeout_s: float = FULL_VIDEO_TIMEOUT_S,
    max_width: int | None = FRAME_MAX_WIDTH,
) -> Iterable[tuple[int, Image.Image]]:
    """Stream whole-video RGB frames from a local file, without copies or a frame cap.

    Slot k really is the picture at k seconds: `fps=…:round={FPS_ROUND}` hands back the last
    decoded picture at or before k, relative to video start (see `FPS_ROUND` for the measurement).
    So `timestamp_s = index / fps`, which `fingerprint_video` stores, is the moment of the picture
    that was hashed — that was not true before issue #70, which is why this recipe is tagged
    `matching.fingerprint.PIPELINE = ffmpeg-rgb512-pdq-v1` and the old one kept its own tag.
    A watchdog kills ffmpeg after `timeout_s` seconds; closing the iterator also reaps ffmpeg.

    `max_width=None` drops the scaler and streams the **master** frame. That is what the crop grid
    of ADR 0039 needs and the one thing it cannot do without: cutting its crops from the 512 px
    picture instead costs a median of 8 bits (p90 12, max 34) of a 23-bit margin
    (`docs/matching/reference-crop-grid.md` §8.1). It also costs memory and pipe bandwidth — a
    4K frame is 24 MB where a 512 px one is 0.8 MB — so only the grid recipe asks for it, and it
    carries its own pipeline tag because the scaler then runs in Pillow rather than in ffmpeg.
    """
    import math
    import threading

    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be finite and positive")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and positive")
    if max_width is not None and max_width < 1:
        raise ValueError("max_width must be positive, or None for the master frame")
    scale = "" if max_width is None else f",scale='min({max_width},iw)':-2"
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-i",
        str(path.resolve()),
        "-map",
        "0:v:0",
        "-vf",
        f"fps={fps}:round={FPS_ROUND}{scale}",
        "-c:v",
        "ppm",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        assert process.stdout is not None
        watchdog = threading.Timer(timeout_s, process.kill)
        watchdog.start()
        try:
            index = 0
            while magic := process.stdout.readline():
                if magic != b"P6\n":
                    raise FrameExtractionError("invalid ffmpeg frame header")
                width, height = map(int, process.stdout.readline().split())
                if process.stdout.readline() != b"255\n":
                    raise FrameExtractionError("invalid ffmpeg pixel depth")
                data = process.stdout.read(width * height * 3)
                if len(data) != width * height * 3:
                    raise FrameExtractionError("truncated ffmpeg frame")
                yield index, Image.frombytes("RGB", (width, height), data)
                index += 1
            if process.wait() != 0:
                raise FrameExtractionError(f"ffmpeg failed or decoding exceeded {timeout_s:g} s")
            if index == 0:
                raise FrameExtractionError("video contains no sampled frames")
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
