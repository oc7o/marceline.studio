# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "numpy>=2.3",
#     "pdqhash>=0.2.8,<0.3",
#     "pillow>=12.3,<13",
# ]
# ///
"""Browser parity harness: is a browser-hashed original comparable with a CLI-hashed one?

Generates synthetic ffmpeg clips (`testsrc`/`testsrc2` - never real or pirated media) and hashes
each one twice:

  * the server/CLI path, `matching.fingerprint.fingerprint_video` (`PIPELINE`);
  * the browser path, the app's own `src/lib/fingerprintClient.ts` driven by Playwright in a real
    headless browser (`BROWSER_PIPELINE`), via `browser.mjs`.

Both tags are imported from `matching.fingerprint`, so the report always names the recipe that
actually ran rather than a tag someone typed into this file.

Two numbers come out, and both matter:

  * **as stored** - the distance between the two sides' hashes for the same `frame_index`. This is
    what matching actually compares, so it is the headline.
  * **aligned** - the distance to the nearest natively decoded frame, with the time offset that
    frame sits at. This says whether a large "as stored" distance comes from the decoders
    disagreeing about *which picture* second k is, or from the same picture hashing differently.

Usage (from the repo root):
    make browser-parity
or  uv run frontend/parity/parity.py [--browsers chromium,firefox,webkit] [--duration 10]
                                     [--fps 1] [--keep-clips] [--fail-over 36]

This is NOT part of `make check`: it needs ffmpeg, a browser download and a minute of decoding per
browser. Run it on demand, and whenever the decode path or the hasher changes.
"""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from matching.fingerprint import (  # noqa: E402
    BROWSER_PHOTO_PIPELINE,
    BROWSER_PIPELINE,
    PHOTO_PIPELINE,
    PIPELINE,
    file_sha256,
    fingerprint_photos,
    fingerprint_video,
)
from matching.frames import iter_video_frames  # noqa: E402
from matching.pdq import hamming_distance, hash_image  # noqa: E402


def _thresholds() -> tuple[int, int]:
    """`STRICT_MAX_DISTANCE` and `LOOSE_MAX_DISTANCE` as `matching/references.py` declares them."""
    source = (BACKEND / "matching" / "references.py").read_text(encoding="utf-8")
    found: dict[str, int] = {}
    for name in ("STRICT_MAX_DISTANCE", "LOOSE_MAX_DISTANCE"):
        match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", source, re.MULTILINE)
        if match is None:
            raise SystemExit(f"{name} not found in matching/references.py")
        found[name] = int(match.group(1))
    return found["STRICT_MAX_DISTANCE"], found["LOOSE_MAX_DISTANCE"]


# The thresholds `match_references` uses. Distances above these stop being evidence. They are the
# real constants, not a copy typed here: `matching/references.py` raised the strict one from 31 to
# 36 (ADR 0015 amendment) and a harness measuring against a stale number would answer the wrong
# question. `matching.references` imports Django models, which this standalone script does not
# have, so the two lines are read out of the module's source instead of imported, and
# `matching/tests/test_browser_parity_thresholds.py` fails if that ever stops working.
MATCH_THRESHOLD, LOOSE_THRESHOLD = _thresholds()
# How far from the nominal second the alignment scan looks for the same picture.
ALIGN_WINDOW_S = 1.0


@dataclass(frozen=True)
class Clip:
    """One synthetic test clip. `source` is an ffmpeg lavfi generator, never a file."""

    name: str
    source: str
    width: int
    height: int
    rate: int
    codec: str
    container: str

    @property
    def file_name(self) -> str:
        return f"{self.name}.{self.container}"


# Resolutions straddle the pipeline's 512 px downscale (480 wide is hashed as decoded; 640, 854 and
# 1280 are scaled down), frame rates straddle the 1 fps sampling grid, and both lavfi patterns are
# used: testsrc moves a bar over a mostly static colour grid, testsrc2 changes far more between
# frames, which makes it the sensitive probe for frame-choice differences.
CLIPS: tuple[Clip, ...] = (
    Clip("h264_testsrc_480x270_25", "testsrc", 480, 270, 25, "h264", "mp4"),
    Clip("h264_testsrc2_640x360_30", "testsrc2", 640, 360, 30, "h264", "mp4"),
    Clip("h264_testsrc_854x480_24", "testsrc", 854, 480, 24, "h264", "mp4"),
    Clip("h264_testsrc2_1280x720_30", "testsrc2", 1280, 720, 30, "h264", "mp4"),
    Clip("vp9_testsrc2_640x360_30", "testsrc2", 640, 360, 30, "vp9", "webm"),
    Clip("vp8_testsrc_640x360_25", "testsrc", 640, 360, 25, "vp8", "webm"),
)

ENCODERS = {
    "h264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
    "vp9": ["-c:v", "libvpx-vp9", "-b:v", "2M", "-deadline", "good", "-cpu-used", "4"],
    "vp8": ["-c:v", "libvpx", "-b:v", "2M", "-deadline", "good", "-cpu-used", "4"],
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def generate_clip(clip: Clip, path: Path, duration: int) -> None:
    """Synthesize one clip with ffmpeg. Deterministic input, so a rerun measures the same pixels."""
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"{clip.source}=size={clip.width}x{clip.height}:rate={clip.rate}:duration={duration}",
        "-pix_fmt",
        "yuv420p",
        *ENCODERS[clip.codec],
        # A keyframe every second keeps the browser's per-second seek cheap and exact.
        "-g",
        str(clip.rate),
    ]
    if clip.container == "mp4":
        command += ["-movflags", "+faststart"]
    subprocess.run([*command, str(path)], check=True, capture_output=True)


def ensure_clips(directory: Path, duration: int, regenerate: bool) -> list[Clip]:
    directory.mkdir(parents=True, exist_ok=True)
    for clip in CLIPS:
        path = directory / clip.file_name
        if regenerate or not path.exists():
            log(f"generating {clip.file_name} ({duration} s)")
            generate_clip(clip, path, duration)
    manifest = {
        "duration_s": duration,
        "clips": [
            {
                "name": clip.name,
                "file": clip.file_name,
                "codec": clip.codec,
                "container": clip.container,
                "width": clip.width,
                "height": clip.height,
                "rate": clip.rate,
                "source": clip.source,
            }
            for clip in CLIPS
        ],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return list(CLIPS)


def cli_hashes(path: Path, fps: float) -> dict[tuple[int, int], bytes]:
    """The server's own recipe: `ffmpeg -vf fps=F,scale='min(512,iw)':-2` -> RGB -> pdqhash."""
    fingerprint = fingerprint_video(path, fps=fps, dihedral=True, title=path.stem)
    return {(frame.frame_index, frame.dihedral): frame.pdq for frame in fingerprint.frames}


def native_hashes(path: Path, rate: int) -> list[bytes]:
    """Every decoded frame, scaled and hashed exactly as the CLI pipeline would.

    This is the common ruler: it lets the report say which *moment* each side actually hashed,
    independently of either sampling grid.
    """
    return [hash_image(image).hash for _, image in iter_video_frames(path, fps=float(rate))]


def browser_hashes(result: dict[str, object]) -> dict[tuple[int, int], bytes]:
    fingerprint = result.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return {}
    frames = fingerprint.get("frames", [])
    assert isinstance(frames, list)
    return {
        (int(frame["frame_index"]), int(frame["dihedral"])): bytes.fromhex(frame["pdq"])
        for frame in frames
    }


def run_browser(
    clips_dir: Path,
    out_path: Path,
    browsers: Sequence[str],
    fps: float,
    *,
    photos: bool = False,
) -> dict[str, object]:
    """Drive the real browsers through browser.mjs (vite build + Playwright)."""
    if not (HERE / "node_modules" / "playwright").exists():
        raise SystemExit(
            "Playwright is not installed for the harness. Once, from frontend/parity:\n"
            "  npm install && npx playwright install chromium\n"
            "(the browser lands in ~/.cache/ms-playwright, shared with other checkouts)"
        )
    command = [
        "node",
        str(HERE / "browser.mjs"),
        "--clips",
        str(clips_dir / "manifest.json"),
        "--out",
        str(out_path),
        "--browsers",
        ",".join(browsers),
        "--fps",
        str(fps),
    ]
    if photos:
        command.append("--photos")
    subprocess.run(command, check=True, cwd=HERE)
    return json.loads(out_path.read_text())


def percentile(values: Sequence[int], q: float) -> int:
    """Same convention as `matching.fingerprint.compare_frames`: never interpolate downwards."""
    if not values:
        return 0
    return int(np.percentile(np.asarray(values, dtype=np.int64), q, method="higher"))


@dataclass(frozen=True)
class Alignment:
    """Which decoded moment each side actually hashed, measured against the native frames."""

    browser_offset_ms: list[int] = field(default_factory=list)
    browser_distance: list[int] = field(default_factory=list)
    cli_offset_ms: list[int] = field(default_factory=list)
    cli_distance: list[int] = field(default_factory=list)


def nearest_native(
    value: bytes, native: Sequence[bytes], index: int, rate: int, fps: float
) -> tuple[int, int]:
    """(offset in ms from the nominal second, distance) of the closest natively decoded frame."""
    nominal = int(round(index / fps * rate))
    window = int(round(ALIGN_WINDOW_S * rate))
    low = max(0, nominal - window)
    high = min(len(native), nominal + window + 1)
    best = min(range(low, high), key=lambda i: hamming_distance(value, native[i]))
    return int(round((best - nominal) / rate * 1000)), hamming_distance(value, native[best])


def align(
    cli: dict[tuple[int, int], bytes],
    browser: dict[tuple[int, int], bytes],
    native: Sequence[bytes],
    rate: int,
    fps: float,
) -> Alignment:
    alignment = Alignment()
    for index in sorted({key[0] for key in browser if key[1] == 0}):
        offset, distance = nearest_native(browser[(index, 0)], native, index, rate, fps)
        alignment.browser_offset_ms.append(offset)
        alignment.browser_distance.append(distance)
    for index in sorted({key[0] for key in cli if key[1] == 0}):
        offset, distance = nearest_native(cli[(index, 0)], native, index, rate, fps)
        alignment.cli_offset_ms.append(offset)
        alignment.cli_distance.append(distance)
    return alignment


@dataclass(frozen=True)
class ClipComparison:
    clip: str
    codec: str
    ok: bool
    note: str
    compared: int
    only_cli: int
    only_browser: int
    identical: int
    median: int
    p90: int
    maximum: int
    over_match: int
    over_loose: int
    native: str
    hashed: str
    alignment: Alignment
    distances: list[int]

    @property
    def identical_share(self) -> float:
        return self.identical / self.compared if self.compared else 0.0


def compare(
    clip: Clip,
    cli: dict[tuple[int, int], bytes],
    result: dict[str, object],
    native: Sequence[bytes] | None,
    fps: float,
) -> ClipComparison:
    meta = result.get("meta", {})
    assert isinstance(meta, dict)
    native_size = f"{meta.get('nativeWidth', 0)}x{meta.get('nativeHeight', 0)}"
    hashed_size = f"{meta.get('frameWidth', 0)}x{meta.get('frameHeight', 0)}"
    if not result.get("ok"):
        error = result.get("error") or {}
        assert isinstance(error, dict)
        note = f"{error.get('name', 'Error')}: {error.get('message', '')}"[:120]
        return ClipComparison(
            clip.name, clip.codec, False, note, 0, len(cli), 0, 0, 0, 0, 0, 0, 0,
            native_size, hashed_size, Alignment(), [],
        )
    browser = browser_hashes(result)
    common = sorted(cli.keys() & browser.keys())
    distances = [hamming_distance(cli[key], browser[key]) for key in common]
    alignment = align(cli, browser, native, clip.rate, fps) if native else Alignment()
    return ClipComparison(
        clip=clip.name,
        codec=clip.codec,
        ok=True,
        note="",
        compared=len(common),
        only_cli=len(cli.keys() - browser.keys()),
        only_browser=len(browser.keys() - cli.keys()),
        identical=sum(1 for distance in distances if distance == 0),
        median=percentile(distances, 50),
        p90=percentile(distances, 90),
        maximum=max(distances) if distances else 0,
        over_match=sum(1 for distance in distances if distance > MATCH_THRESHOLD),
        over_loose=sum(1 for distance in distances if distance > LOOSE_THRESHOLD),
        native=native_size,
        hashed=hashed_size,
        alignment=alignment,
        distances=distances,
    )


def pool(rows: Sequence[ClipComparison]) -> ClipComparison:
    distances = [distance for row in rows if row.ok for distance in row.distances]
    merged = Alignment()
    for row in rows:
        merged.browser_offset_ms.extend(row.alignment.browser_offset_ms)
        merged.browser_distance.extend(row.alignment.browser_distance)
        merged.cli_offset_ms.extend(row.alignment.cli_offset_ms)
        merged.cli_distance.extend(row.alignment.cli_distance)
    return ClipComparison(
        clip="pooled",
        codec="",
        ok=True,
        note="",
        compared=len(distances),
        only_cli=sum(row.only_cli for row in rows),
        only_browser=sum(row.only_browser for row in rows),
        identical=sum(1 for distance in distances if distance == 0),
        median=percentile(distances, 50),
        p90=percentile(distances, 90),
        maximum=max(distances) if distances else 0,
        over_match=sum(1 for distance in distances if distance > MATCH_THRESHOLD),
        over_loose=sum(1 for distance in distances if distance > LOOSE_THRESHOLD),
        native="",
        hashed="",
        alignment=merged,
        distances=distances,
    )


def stored_table(rows: Iterable[ClipComparison], pooled: ClipComparison | None) -> str:
    lines = [
        f"| clip | codec | rows | identical | median | p90 | max | > {MATCH_THRESHOLD} | > {LOOSE_THRESHOLD} |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not row.ok:
            lines.append(f"| {row.clip} | {row.codec} | — | not decoded: {row.note} | | | | | |")
            continue
        lines.append(
            f"| {row.clip} | {row.codec} | {row.compared} | {row.identical_share:.0%} | "
            f"{row.median} | {row.p90} | {row.maximum} | {row.over_match} | {row.over_loose} |"
        )
    if pooled is not None and pooled.compared:
        lines.append(
            f"| **all clips pooled** | | **{pooled.compared}** | "
            f"**{pooled.identical_share:.0%}** | "
            f"**{pooled.median}** | **{pooled.p90}** | **{pooled.maximum}** | "
            f"**{pooled.over_match}** | **{pooled.over_loose}** |"
        )
    return "\n".join(lines)


def aligned_table(rows: Iterable[ClipComparison]) -> str:
    lines = [
        "| clip | side | offset from t=k (median / max ms) | distance to that frame "
        "(median / p90 / max) | identical |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        if not row.ok or not row.alignment.browser_distance:
            continue
        for side, offsets, distances in (
            ("browser", row.alignment.browser_offset_ms, row.alignment.browser_distance),
            ("CLI", row.alignment.cli_offset_ms, row.alignment.cli_distance),
        ):
            identical = sum(1 for distance in distances if distance == 0)
            share = identical / len(distances) if distances else 0.0
            lines.append(
                f"| {row.clip} | {side} | {int(np.median(offsets)):+d} / "
                f"{max(offsets, key=abs):+d} | {percentile(distances, 50)} / "
                f"{percentile(distances, 90)} / {max(distances)} | {share:.0%} |"
            )
    return "\n".join(lines)


def report(document: dict[str, object], comparisons: dict[str, list[ClipComparison]]) -> str:
    browsers = document.get("browsers", [])
    assert isinstance(browsers, list)
    parts: list[str] = ["# Browser parity report", ""]
    parts.append(
        f"Generated {document.get('generated_at')} on {document.get('platform')}, "
        f"node {document.get('node')}, sampling {document.get('fps')} fps."
    )
    parts.append("")
    parts.append("Synthetic ffmpeg `testsrc`/`testsrc2` clips only - no real or pirated media.")
    parts.append(
        f"Distances are PDQ Hamming bits between the CLI hash (`{PIPELINE}`) and the "
        f"browser hash (`{BROWSER_PIPELINE}`), over all eight dihedral orientations."
    )
    for entry in browsers:
        assert isinstance(entry, dict)
        name = str(entry.get("name"))
        parts.append("")
        if not entry.get("launched"):
            error = str(entry.get("error", "")).splitlines()[0]
            parts.append(f"## {name}: not run ({error})")
            continue
        rows = comparisons.get(name, [])
        pooled = pool(rows)
        parts.append(f"## {name} {entry.get('version')}")
        parts.append("")
        results = entry.get("results", [])
        assert isinstance(results, list)
        metas = [r.get("meta", {}) for r in results if isinstance(r, dict)]
        agents = {str(meta.get("userAgent", "")) for meta in metas if isinstance(meta, dict)}
        for agent in sorted(a for a in agents if a):
            parts.append(f"User agent: `{agent}`")
        support = next((meta.get("canPlay", {}) for meta in metas if meta.get("canPlay")), {})
        if support:
            parts.append("")
            parts.append(
                "Codec support reported by the browser (`canPlayType`): "
                + ", ".join(f"{codec} {verdict}" for codec, verdict in sorted(support.items()))
            )
        parts.append("")
        parts.append("### As stored: same `frame_index` on both sides")
        parts.append("")
        parts.append("This is what matching compares.")
        parts.append("")
        parts.append(stored_table(rows, pooled))
        for row in rows:
            if row.ok and (row.only_cli or row.only_browser):
                parts.append("")
                parts.append(
                    f"- `{row.clip}`: {row.only_cli} hash rows only the CLI produced, "
                    f"{row.only_browser} only the browser (sampling grid or quality gate)."
                )
        if pooled.alignment.browser_distance:
            parts.append("")
            parts.append("### Aligned: which moment each side actually hashed")
            parts.append("")
            parts.append(
                "Every natively decoded frame is hashed with the CLI recipe and used as a common "
                "ruler. `offset` is how far the nearest matching frame sits from the nominal "
                "second; `distance` is how far the hash is from that frame. A near-zero distance "
                "at a non-zero offset means the pipelines hashed *different pictures*, not the "
                "same picture differently."
            )
            parts.append("")
            parts.append(aligned_table(rows))
            parts.append("")
            browser_offset = int(np.median(pooled.alignment.browser_offset_ms))
            parts.append(
                f"Pooled: browser offset median {browser_offset:+d} ms "
                f"(distance median {percentile(pooled.alignment.browser_distance, 50)}, "
                f"max {max(pooled.alignment.browser_distance)}); "
                f"CLI offset median {int(np.median(pooled.alignment.cli_offset_ms)):+d} ms "
                f"(distance median {percentile(pooled.alignment.cli_distance, 50)}, "
                f"max {max(pooled.alignment.cli_distance)})."
            )
        parts.append("")
        for row in rows:
            if row.ok:
                parts.append(f"- `{row.clip}`: decoded {row.native}, hashed {row.hashed}.")
    return "\n".join(parts) + "\n"


def as_json(row: ClipComparison) -> dict[str, object]:
    histogram: dict[int, int] = {}
    for distance in row.distances:
        histogram[distance] = histogram.get(distance, 0) + 1
    return {
        "clip": row.clip,
        "codec": row.codec,
        "ok": row.ok,
        "note": row.note,
        "compared": row.compared,
        "only_cli": row.only_cli,
        "only_browser": row.only_browser,
        "identical": row.identical,
        "identical_share": row.identical_share,
        "median": row.median,
        "p90": row.p90,
        "max": row.maximum,
        "over_match_threshold": row.over_match,
        "over_loose_threshold": row.over_loose,
        "native_size": row.native,
        "hashed_size": row.hashed,
        "alignment": {
            "browser_offset_ms": row.alignment.browser_offset_ms,
            "browser_distance": row.alignment.browser_distance,
            "cli_offset_ms": row.alignment.cli_offset_ms,
            "cli_distance": row.alignment.cli_distance,
        },
        "distance_histogram": dict(sorted(histogram.items())),
    }


# --- Photo collections (issue #116, ADR 0053) ---------------------------------------------------
#
# The same question as the video harness asks, for the other shape of original: the CLI hashes a
# folder of images with `fingerprint_photos` (Pillow -> RGB -> PDQ, no scaler), the browser hashes
# the very same files with the app's own `fingerprintPhotos` (platform decoder -> OffscreenCanvas
# -> the same WASM hasher), and this reports the per-image Hamming distance **by image format**,
# because the format is what decides how far two decoders can drift. PNG is lossless and should
# agree bit for bit; JPEG, WebP and AVIF are the open question that keeps the browser recipe on a
# tag of its own.
#
# Synthetic pictures only, generated here and deleted afterwards unless `--keep-clips`.

#: One set per container, each holding the same synthetic pictures, so a difference is the codec's.
PHOTO_FORMATS: tuple[tuple[str, str], ...] = (
    ("png", "PNG"),
    ("jpeg", "JPEG"),
    ("webp", "WEBP"),
)


def photo_pictures() -> list[tuple[str, "object"]]:
    """Deterministic synthetic images: a gradient, seeded noise, a checkerboard, an odd size."""
    from PIL import Image  # imported here: the video path needs no Pillow

    rows, columns = np.mgrid[0:240, 0:320]
    gradient = np.stack(
        [(rows * 3) % 256, (columns * 2) % 256, ((rows + columns) * 5) % 256], axis=-1
    ).astype(np.uint8)
    generator = np.random.default_rng(20260918)
    noise = generator.integers(0, 256, size=(200, 300, 3), dtype=np.uint8)
    checker = np.zeros((256, 256, 3), dtype=np.uint8)
    checker[::16, :, :] = 240
    checker[:, ::16, :] = 180
    stripes = np.zeros((181, 97, 3), dtype=np.uint8)  # odd, non-square
    stripes[:, ::3, :] = 220
    return [
        ("01-gradient", Image.fromarray(gradient, "RGB")),
        ("02-noise", Image.fromarray(noise, "RGB")),
        ("03-checker", Image.fromarray(checker, "RGB")),
        ("04-stripes", Image.fromarray(stripes, "RGB")),
    ]


def ensure_photos(directory: Path, regenerate: bool) -> list[dict[str, object]]:
    """Write one set per format and the manifest `browser.mjs --photos` reads."""
    directory.mkdir(parents=True, exist_ok=True)
    pictures = photo_pictures()
    sets: list[dict[str, object]] = []
    for suffix, pillow_format in PHOTO_FORMATS:
        folder = directory / suffix
        folder.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        for name, image in pictures:
            path = folder / f"{name}.{suffix}"
            if regenerate or not path.exists():
                # Lossless where the format allows it, so only the decoder differs; JPEG's own
                # quantisation is part of what is being measured and 92 is a realistic export.
                options = {"quality": 92} if pillow_format == "JPEG" else {"lossless": True}
                image.save(path, format=pillow_format, **options)  # type: ignore[arg-type]
            files.append(str(path.relative_to(directory)))
        sets.append({"name": suffix, "format": pillow_format, "files": files})
    (directory / "manifest.json").write_text(
        json.dumps({"kind": "photos", "clips": sets}, indent=2) + "\n"
    )
    return sets


@dataclass(frozen=True)
class PhotoComparison:
    """One set, as stored: the CLI's hashes against the browser's, per image and orientation."""

    name: str
    pillow_format: str
    ok: bool
    error: str
    images: int
    #: The collection digest each side computed. Equal unless the browser left an image out.
    cli_digest: str
    browser_digest: str
    skipped: int
    compared: int
    identical: int
    distances: list[int] = field(default_factory=list)

    @property
    def median(self) -> int:
        return percentile(self.distances, 0.5)

    @property
    def p90(self) -> int:
        return percentile(self.distances, 0.9)

    @property
    def maximum(self) -> int:
        return max(self.distances, default=0)

    @property
    def over_strict(self) -> int:
        return sum(1 for value in self.distances if value > MATCH_THRESHOLD)

    @property
    def over_loose(self) -> int:
        return sum(1 for value in self.distances if value > LOOSE_THRESHOLD)


def cli_photo_hashes(paths: Sequence[Path]) -> tuple[str, dict[tuple[int, int], bytes]]:
    """The operator's own recipe, keyed by **digest ordinal** so the browser's keys line up.

    `fingerprint_photos` numbers images in sorted *path* order; the browser has no paths and
    numbers them in sorted *digest* order (ADR 0053). The identity is order-free either way, so
    the two are compared after remapping one onto the other.
    """
    fingerprint = fingerprint_photos(list(paths), title="parity set")
    by_path = sorted(paths)
    digests = {path: file_sha256(path) for path in by_path}
    by_digest = sorted(by_path, key=lambda path: digests[path])
    remap = {path_index: by_digest.index(path) for path_index, path in enumerate(by_path)}
    hashes = {
        (remap[frame.frame_index], frame.dihedral): frame.pdq for frame in fingerprint.frames
    }
    return fingerprint.sha256, hashes


def compare_photos(
    entry: dict[str, object], directory: Path, result: dict[str, object]
) -> PhotoComparison:
    name = str(entry["name"])
    pillow_format = str(entry["format"])
    files = [directory / str(file) for file in entry["files"]]  # type: ignore[union-attr]
    cli_digest, cli = cli_photo_hashes(files)
    if not result.get("ok"):
        error = result.get("error") or {}
        assert isinstance(error, dict)
        return PhotoComparison(
            name=name,
            pillow_format=pillow_format,
            ok=False,
            error=f"{error.get('name', 'Error')}: {error.get('message', '')}",
            images=len(files),
            cli_digest=cli_digest,
            browser_digest="",
            skipped=len(result.get("skipped") or []),
            compared=0,
            identical=0,
        )
    fingerprint = result.get("fingerprint")
    assert isinstance(fingerprint, dict)
    browser = {
        (int(frame["frame_index"]), int(frame["dihedral"])): bytes.fromhex(frame["pdq"])
        for frame in fingerprint["frames"]
    }
    shared = sorted(set(cli) & set(browser))
    distances = [hamming_distance(cli[key], browser[key]) for key in shared]
    return PhotoComparison(
        name=name,
        pillow_format=pillow_format,
        ok=True,
        error="",
        images=len(files),
        cli_digest=cli_digest,
        browser_digest=str(fingerprint["sha256"]),
        skipped=len(result.get("skipped") or []),
        compared=len(shared),
        identical=sum(1 for value in distances if value == 0),
        distances=distances,
    )


def photo_report(document: dict[str, object], rows: dict[str, list[PhotoComparison]]) -> str:
    lines = [
        "# Browser parity: photo collections",
        "",
        f"Generated {document.get('generated_at')} · node {document.get('node')} · "
        f"{document.get('platform')}",
        f"CLI recipe `{PHOTO_PIPELINE}` vs browser recipe `{BROWSER_PHOTO_PIPELINE}`.",
        f"Match threshold {MATCH_THRESHOLD}, loose threshold {LOOSE_THRESHOLD} "
        "(read from matching/references.py).",
        "",
        "Synthetic pictures only. Each set holds the same images in one container, so a difference",
        "between the rows is the image codec and the decoder, not the picture.",
        "",
    ]
    for browser_name, comparisons in rows.items():
        lines.append(f"## {browser_name}")
        lines.append("")
        header = "| set | images | rows | identical | median | p90 | max | > strict | id equal |"
        lines.append(header)
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in comparisons:
            if not row.ok:
                lines.append(f"| {row.name} | {row.images} | FAILED: {row.error} | | | | | | |")
                continue
            share = f"{100 * row.identical / row.compared:.0f} %" if row.compared else "-"
            equal = "yes" if row.cli_digest == row.browser_digest else f"NO ({row.skipped} skipped)"
            lines.append(
                f"| {row.name} | {row.images} | {row.compared} | {share} | {row.median} | "
                f"{row.p90} | {row.maximum} | {row.over_strict} | {equal} |"
            )
        lines.append("")
        pooled = [value for row in comparisons for value in row.distances]
        if pooled:
            lines.append(
                f"Pooled: {len(pooled)} rows, median {percentile(pooled, 0.5)}, "
                f"p90 {percentile(pooled, 0.9)}, max {max(pooled)}, "
                f"{sum(1 for v in pooled if v > MATCH_THRESHOLD)} past the strict threshold."
            )
            lines.append("")
    return "\n".join(lines)


def run_photo_parity(args: argparse.Namespace) -> int:
    """The `--photos` mode, end to end. Returns the process exit code."""
    sets = ensure_photos(args.clips_dir, args.regenerate)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    browser_json = args.out_dir / "browser-photos.json"
    if args.browser_json is not None:
        document = json.loads(Path(args.browser_json).read_text())
    else:
        browsers = [name.strip() for name in args.browsers.split(",") if name.strip()]
        log(f"hashing {len(sets)} photo sets in {', '.join(browsers)}")
        document = run_browser(
            args.clips_dir, browser_json, browsers, args.fps, photos=True
        )
    by_name = {str(entry["name"]): entry for entry in sets}
    rows: dict[str, list[PhotoComparison]] = {}
    entries = document.get("browsers", [])
    assert isinstance(entries, list)
    for entry in entries:
        comparisons = []
        for result in entry.get("results", []):
            declared = by_name.get(str(result.get("clip")))
            if declared is None:
                continue
            comparisons.append(compare_photos(declared, args.clips_dir, result))
        rows[str(entry.get("name"))] = comparisons

    text = photo_report(document, rows)
    print(text)
    (args.out_dir / "report-photos.md").write_text(text)
    (args.out_dir / "report-photos.json").write_text(
        json.dumps(
            {
                "generated_at": document.get("generated_at"),
                "cli_pipeline": PHOTO_PIPELINE,
                "browser_pipeline": BROWSER_PHOTO_PIPELINE,
                "match_threshold": MATCH_THRESHOLD,
                "loose_threshold": LOOSE_THRESHOLD,
                "browsers": {
                    name: [
                        {
                            "set": row.name,
                            "format": row.pillow_format,
                            "ok": row.ok,
                            "error": row.error,
                            "images": row.images,
                            "compared": row.compared,
                            "identical": row.identical,
                            "median": row.median,
                            "p90": row.p90,
                            "max": row.maximum,
                            "over_strict": row.over_strict,
                            "over_loose": row.over_loose,
                            "cli_digest": row.cli_digest,
                            "browser_digest": row.browser_digest,
                            "skipped": row.skipped,
                            "digests_equal": row.cli_digest == row.browser_digest,
                        }
                        for row in comparisons
                    ]
                    for name, comparisons in rows.items()
                },
            },
            indent=2,
        )
        + "\n"
    )
    log(f"wrote {args.out_dir / 'report-photos.md'}")
    if not args.keep_clips:
        shutil.rmtree(args.clips_dir, ignore_errors=True)

    measured = [row for comparisons in rows.values() for row in comparisons if row.ok]
    if not measured:
        log("no browser hashed any photo set")
        return 1
    if args.fail_over is not None:
        worst = max(row.maximum for row in measured)
        if worst > args.fail_over:
            log(f"maximum distance {worst} exceeds --fail-over {args.fail_over}")
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="parity.py", description="Compare browser and CLI fingerprints of synthetic clips."
    )
    parser.add_argument("--browsers", default="chromium",
                        help="comma-separated: chromium (default), firefox, webkit")
    parser.add_argument("--duration", type=int, default=10, help="clip length in seconds")
    parser.add_argument("--fps", type=float, default=1.0, help="sampling rate (default 1)")
    parser.add_argument("--clips-dir", type=Path, default=HERE / "clips")
    parser.add_argument("--out-dir", type=Path, default=HERE / "out")
    parser.add_argument("--regenerate", action="store_true", help="re-encode the clips")
    parser.add_argument("--keep-clips", action="store_true", help="keep the generated videos")
    parser.add_argument("--skip-alignment", action="store_true",
                        help="skip the native-frame scan (faster, no frame-choice diagnosis)")
    parser.add_argument("--browser-json", type=Path,
                        help="reuse an existing browser.mjs result instead of running browsers")
    parser.add_argument("--fail-over", type=int, default=None, metavar="BITS",
                        help="exit non-zero if any distance exceeds BITS (e.g. the strict threshold)")
    parser.add_argument(
        "--photos",
        action="store_true",
        help="measure the photo recipe instead of the video one (issue #116, ADR 0053)",
    )
    args = parser.parse_args(argv)

    if args.photos:
        # No ffmpeg in this mode: Pillow writes the pictures and there is no timeline.
        if args.clips_dir == HERE / "clips":
            args.clips_dir = HERE / "photos"
        return run_photo_parity(args)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is not on PATH")
    if args.duration < 2 or not math.isfinite(args.fps) or args.fps <= 0:
        raise SystemExit("--duration must be >= 2 and --fps positive")

    browsers = [name.strip() for name in args.browsers.split(",") if name.strip()]
    clips = ensure_clips(args.clips_dir, args.duration, args.regenerate)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    log("hashing the clips with the CLI/server pipeline")
    cli = {clip.name: cli_hashes(args.clips_dir / clip.file_name, args.fps) for clip in clips}

    browser_json = args.out_dir / "browser.json"
    if args.browser_json is not None:
        document = json.loads(Path(args.browser_json).read_text())
    else:
        log(f"hashing the clips in {', '.join(browsers)}")
        document = run_browser(args.clips_dir, browser_json, browsers, args.fps)

    native: dict[str, list[bytes]] = {}
    if not args.skip_alignment:
        log("hashing every native frame as the common ruler (frame-choice diagnosis)")
        native = {
            clip.name: native_hashes(args.clips_dir / clip.file_name, clip.rate) for clip in clips
        }

    by_name = {clip.name: clip for clip in clips}
    comparisons: dict[str, list[ClipComparison]] = {}
    entries = document.get("browsers", [])
    assert isinstance(entries, list)
    for entry in entries:
        rows = []
        for result in entry.get("results", []):
            clip = by_name.get(str(result.get("clip")))
            if clip is None:
                continue
            rows.append(compare(clip, cli[clip.name], result, native.get(clip.name), args.fps))
        comparisons[str(entry.get("name"))] = rows

    text = report(document, comparisons)
    print(text)
    (args.out_dir / "report.md").write_text(text)
    (args.out_dir / "report.json").write_text(
        json.dumps(
            {
                "generated_at": document.get("generated_at"),
                "fps": document.get("fps"),
                "match_threshold": MATCH_THRESHOLD,
                "loose_threshold": LOOSE_THRESHOLD,
                "browsers": {
                    name: [as_json(row) for row in rows] for name, rows in comparisons.items()
                },
            },
            indent=2,
        )
        + "\n"
    )
    log(f"wrote {args.out_dir / 'report.md'} and {args.out_dir / 'report.json'}")

    if not args.keep_clips:
        shutil.rmtree(args.clips_dir, ignore_errors=True)

    failures = [row for rows in comparisons.values() for row in rows if not row.ok]
    decoded = [row for rows in comparisons.values() for row in rows if row.ok and row.compared]
    if not decoded:
        log("no browser produced any hashes")
        return 1
    if args.fail_over is not None:
        worst = max(row.maximum for row in decoded)
        if worst > args.fail_over:
            log(f"maximum distance {worst} exceeds --fail-over {args.fail_over}")
            return 1
    if failures:
        log(f"{len(failures)} clip(s) the browser refused to decode - see the report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
