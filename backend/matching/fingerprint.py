"""Fingerprint a local original into hashes + metadata; shared by server and local client.

No Django: this module must import with only numpy, pdqhash and Pillow installed, because
`client/fingerprint.py` runs it on the operator's machine. `matching.references.add_reference`
runs the same code on the server, so both sides produce identical rows for identical decoders.

`PIPELINE` names the recipe. It went to `ffmpeg-rgb512-pdq-v1` on 2026-09-16 (issue #70, ADR 0022)
because the old one sampled the picture at about k + 0.47 s and stored it under the label k; v1
samples the picture at k. `PIPELINE_V0` is still read and still imports — references on it keep
meaning what they meant — but only a re-run over the source file produces v1.

An original is either a **video** (frames along a timeline, `frame_index / fps` seconds) or a
**photo collection**: N unrelated images with no order (issue #69, ADR 0020). PDQ needs no change
— it is an image hash — but the two shapes must never be confused, so they carry different
pipeline tags and different JSON key sets, and `from_json` refuses a photo pipeline on a video
document and the other way round.

The JSON file format (`to_json` / `from_json`) holds sha256, size, title, creators and PDQ hashes
only: no video data, no image data, no path, no host or user names.
Version 1 stored `creator` as one optional name; version 2 (2026-09-16, issue #64) stores a list,
the primary creator first, because originals routinely show several performers. Version 3
(2026-09-16, issue #69) adds the required `kind` key (`video` / `photos`); version 1 and 2 files
are videos and are still read. Version 4 (2026-09-17, ADR 0037) adds `embedding_pipeline` and an
optional per-frame `embedding`: the second signal's 512-bit sketch, hex, on the unflipped
orientation only. Version 5 (2026-09-18, ADR 0039) adds `crop_grid` — the reference-side lattice
(null = none) — and, on the unflipped orientation of a gridded moment, that moment's whole blob as
hex. `to_json` always writes version 5.

A gridded document is **large**: 16 KB of hex per moment against 512 bytes of plain hashes, about
1.2 GB for the current library where the blobs themselves are 629 MB. Hex rather than base64 on
purpose — every other hash in this format is hex, and two encodings in one file is how a reader
ends up decoding the wrong thing.
"""

import hashlib
import math
import os
import platform
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pdqhash
from PIL import Image

from matching import crop_grid
from matching.embedding import EMBEDDING_PIPELINE, EMBEDDING_PIPELINES, SKETCH_BYTES
from matching.frames import (
    FFMPEG_TIMEOUT_S,
    FRAME_MAX_WIDTH,
    FULL_VIDEO_TIMEOUT_S,
    Embedder,
    _probe_duration,
    iter_video_frames,
)
from matching.pdq import (
    DIHEDRAL_ORIENTATIONS,
    MIN_QUALITY,
    PDQ_HASH_BYTES,
    MediaDecodeError,
    hamming_distance,
    hash_image,
    open_image,
)

# Decoder + scaler + hash recipe; references from different pipelines are not bit-comparable.
#
# v1 (issue #70, ADR 0022) fixed the sampling grid: `fps=F:round=up` hands back the last picture at
# or before second k, so slot k really is the picture at k and `timestamp_s = frame_index / fps` is
# the moment that was hashed. v0 used the filter's default rounding and sampled k + 0.46..0.48 s
# under the label k. Same decoder, same scaler, same hasher — a different *moment*, so v0 hashes
# and v1 hashes of one video are not comparable and the two tags must stay apart.
PIPELINE = "ffmpeg-rgb512-pdq-v1"
# References imported before 2026-09-16 carry this tag and keep meaning exactly what they meant:
# hashes of the picture at about k + 0.47 s, stored under the label k. They stay valid, matchable
# and untouched; only a re-run of `client/fingerprint.py` over the source file produces v1.
PIPELINE_V0 = "ffmpeg-rgb512-pdq-v0"
# The browser client (issue #29 stage B, ADR 0019) decodes with the platform decoder and scales on
# a canvas. The PDQ hasher is bit-identical (ADR 0017), but the frames reaching it are not the same
# frames. Measured in real headless Chromium 153 (`frontend/parity`, issue #28): hashing the *same*
# decoded picture agrees to a median of 2 bits, but max 22 at 1280x720, where the canvas downscale
# does the most work — too close to the match threshold of 31 to call the recipes equal. Since v1
# the browser and the CLI at least sample the *same moment* (`currentTime = k` is the same "last
# picture at or before k" rule as `round=up`), so the remaining gap is pixels, not frame choice.
# The tags still stay separate, and `Reference.pipeline` is what records which one produced a row.
# v1 (2026-09-17) sizes the canvas exactly like ffmpeg's `scale='min(512,iw)':-2`: height rounded
# to the *nearest* even number, and evened for frames narrower than 512 too. v0 floored it
# (1280x723 -> 512x288 where ffmpeg hashes 512x290) and kept odd heights of narrow frames, so a
# v0 and a v1 browser reference of such a video differ by a few bits and must stay apart.
BROWSER_PIPELINE = "browser-rgb512-pdq-v1"
BROWSER_PIPELINE_V0 = "browser-rgb512-pdq-v0"
# Photo collections (issue #69): Pillow decodes the image at its own resolution and PDQ hashes it.
# No ffmpeg, no 512-pixel scaler, no timeline - exactly what `matching.frames.hash_poster` does to
# a page poster, so both sides of a photo match go through the same recipe.
PHOTO_PIPELINE = "pillow-rgb-pdq-v0"
# The same recipe out of a browser (issue #116, ADR 0053): the image at its own resolution, RGB,
# PDQ, eight orientations, no scaler and no timeline — but decoded by the platform's image decoder
# rather than Pillow's. `createImageBitmap` is asked for no EXIF transform and no colour
# conversion, and both are hints an implementation may ignore; PNG should agree bit for bit, JPEG,
# WebP and AVIF are unmeasured. A tag is what records that (ADR 0022), so a browser-hashed set is
# never silently treated as bit-comparable with a Pillow-hashed one. `make browser-parity
# PARITY_ARGS="--photos"` measures the gap when the operator runs it.
BROWSER_PHOTO_PIPELINE = "browser-rgb-pdq-v0"
# The reference-side crop grid (ADR 0039, issue #80). The *sampling* grid is `PIPELINE`'s — slot k
# is still the picture at k, so this tag is aligned — but the pixel recipe is a different one:
# ffmpeg hands back the **master** frame, the 256 crops of `matching.crop_grid.grid_specs` are cut
# from it, and each crop is scaled to 512 px by Pillow before PDQ. Cropping the 512 px picture the
# plain recipe already emits instead costs a median of 8 bits of a 23-bit margin
# (`docs/matching/reference-crop-grid.md` §8.1), which is why the master is the source; and since
# the scaler is Pillow's rather than ffmpeg's, even this recipe's *uncropped* hash sits a few bits
# from the `PIPELINE` one. That is exactly what a pipeline tag is for (ADR 0022): a reference on
# this tag imports **alongside** the plain one, nothing is migrated in place, no stored hash
# changes, and the old row is retired by hand only once the pilot says so.
CROP_GRID_PIPELINE = "ffmpeg-master-pil512-pdq-grid256-v1"
VIDEO_PIPELINES = frozenset(
    {PIPELINE, PIPELINE_V0, BROWSER_PIPELINE, BROWSER_PIPELINE_V0, CROP_GRID_PIPELINE}
)
# Recipes that carry a crop grid. A blob on any other tag would claim a geometry nobody can
# reproduce, so `from_json` refuses one and the importer never writes one.
CROP_GRID_PIPELINES = frozenset({CROP_GRID_PIPELINE})
# Video recipes whose slot k is the picture at k. A pipeline outside this set stores hashes on a
# different time grid, so its seconds are not the seconds of another pipeline (ADR 0022).
ALIGNED_PIPELINES = frozenset({PIPELINE, BROWSER_PIPELINE, BROWSER_PIPELINE_V0, CROP_GRID_PIPELINE})
PHOTO_PIPELINES = frozenset({PHOTO_PIPELINE, BROWSER_PHOTO_PIPELINE})
PIPELINES = VIDEO_PIPELINES | PHOTO_PIPELINES

# What each tag promises about the sampling grid, in words an operator can act on. Used by the
# admin column and `reference_report`, so both say the same thing about the same row.
PIPELINE_LABELS = {
    PIPELINE: "current grid: frame k is the picture at k s",
    PIPELINE_V0: "superseded grid: frame k is the picture at about k + 0.47 s — re-fingerprint",
    BROWSER_PIPELINE: "browser client: frame k is the picture at k s, platform decoder and canvas",
    BROWSER_PIPELINE_V0: (
        "browser client, superseded frame size: frame k is the picture at k s, canvas height "
        "floored instead of ffmpeg's rounding"
    ),
    PHOTO_PIPELINE: "photo collection: image positions, no timeline",
    BROWSER_PHOTO_PIPELINE: (
        "photo collection from a browser: image positions, no timeline, platform image decoder"
    ),
    CROP_GRID_PIPELINE: (
        "crop grid: frame k is the picture at k s, plus 256 crops of the master frame per moment "
        "(ADR 0039) — imports alongside the plain recipe, never instead of it"
    ),
}

KIND_VIDEO = "video"
KIND_PHOTOS = "photos"
KINDS = frozenset({KIND_VIDEO, KIND_PHOTOS})
PIPELINES_BY_KIND = {KIND_VIDEO: VIDEO_PIPELINES, KIND_PHOTOS: PHOTO_PIPELINES}

FORMAT = "marceline-fingerprint"
FORMAT_VERSION = 5
# v1: "creator" is a name or null. v2: "creator" is a list of names, the primary creator first.
# v3: "kind" says video or photos; v1 and v2 files are videos.
# v4: "embedding_pipeline" names the second signal's recipe ("" = none) and each unflipped frame
# may carry its 64-byte sketch as hex (ADR 0037).
# v5: "crop_grid" describes the reference-side lattice (null = none) and each unflipped frame of a
# gridded document carries its whole blob as hex (ADR 0039). Videos only: the grid answers a site's
# re-frame of a video moment, and a photo collection is a different shape of original.
SUPPORTED_VERSIONS = frozenset({1, 2, 3, 4, 5})
KIND_VERSION = 3
EMBEDDING_VERSION = 4
CROP_GRID_VERSION = 5
TOOL_VERSION = "0.4.0"
# What Pillow 12.3 (backend/uv.lock) decodes and the operator is likely to have. Checked with
# `PIL.features.check`: avif and webp are compiled in, heif is not.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".avif")
# A photo set is a shoot, not an archive; anything larger is a mistake or a malformed file.
MAX_COLLECTION_IMAGES = 20_000
# Domain separation: a collection id can never equal the sha256 of a file.
COLLECTION_DIGEST_PREFIX = b"marceline-photo-collection-v1\n"
# The one orientation that carries a sketch: an embedding is roughly flip-invariant, so the
# dihedral expansion PDQ needs does not apply to it (ADR 0037).
EMBEDDING_ORIENTATION = 0
# The one orientation that carries a crop grid, for the opposite reason: PDQ is *not* flip
# invariant, but mirroring stays the job of the eight uncropped orientations. The grid answers a
# re-frame, the dihedral expansion answers a flip, and the two must not multiply — 256 crops x 8
# orientations per moment would be 2,048 hashes for no measured gain (ADR 0039 §3).
CROP_GRID_ORIENTATION = 0
MAX_TITLE_LENGTH = 255
# A video lists a handful of creators; anything longer is a malformed file.
MAX_CREATORS = 20
MAX_CLIENT_ENTRIES = 16
MAX_CLIENT_VALUE_LENGTH = 200
# Accept rounding at the end of the sampling grid (ffmpeg's fps filter may emit one extra frame).
FRAME_INDEX_SLACK = 2
TIMESTAMP_TOLERANCE = 1e-6

_SKETCH_HEX = re.compile(r"[0-9a-f]{128}")
# A crop grid is a whole number of 32-byte hashes: 64 hex characters each, at least one.
_GRID_HEX = re.compile(r"(?:[0-9a-f]{64})+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]{64}")
_CLIENT_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")


class FingerprintFormatError(ValueError):
    """A fingerprint file is malformed or inconsistent."""


@dataclass(frozen=True)
class FingerprintFrame:
    """One hashed position of an original, in one orientation.

    `frame_index` is the sample position of a video frame, or the ordinal of an image inside a
    photo collection. `timestamp_s` is seconds into the video; for a photo collection it repeats
    the ordinal, because a collection has no time (ADR 0020) — the JSON file omits it there.
    """

    frame_index: int
    timestamp_s: float
    dihedral: int
    quality: int
    pdq: bytes
    # The second signal's 512-bit sign sketch of this picture (ADR 0037), on the unflipped
    # orientation only; None where the fingerprint run computed none.
    embedding: bytes | None = None
    # This moment's whole crop lattice (ADR 0039): N x 32 bytes in `crop_grid.grid_specs` order,
    # unflipped orientation only, None where the run computed none. `pdq` above is entry 0 of it.
    crop_grid: bytes | None = None


@dataclass(frozen=True)
class CropGrid:
    """What recipe made a document's crop grids, so a reader never has to guess (ADR 0022's rule).

    `stage1` is how many entries at the head of every blob are the corner crops the searchable
    index takes; `dedup_bits` is the pruning radius the run used, or None for a whole lattice.
    """

    step: float
    max_trim: float
    stage1: int
    dedup_bits: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "max_trim": self.max_trim,
            "stage1": self.stage1,
            "dedup_bits": self.dedup_bits,
        }


@dataclass(frozen=True)
class Fingerprint:
    """Hashes and metadata of one original: a video, or a whole photo collection.

    `sha256` is the file digest for a video and the collection digest (`collection_sha256`) for a
    photo collection. `duration_s` and `fps` are a video's; a collection has neither and counts
    its images in `image_count` instead.
    """

    sha256: str
    file_size: int
    duration_s: float | None
    fps: float | None
    dihedral: bool
    pipeline: str
    title: str
    creators: tuple[str, ...]
    client: dict[str, str]
    frames: tuple[FingerprintFrame, ...]
    kind: str = KIND_VIDEO
    image_count: int | None = None
    # Recipe that produced the frames' sketches; "" when this original has none (ADR 0037).
    embedding_pipeline: str = ""
    # Lattice the frames' crop grids were cut on; None when this original has none (ADR 0039).
    crop_grid: CropGrid | None = None

    @property
    def creator(self) -> str | None:
        """The primary creator (rights holder), or None when the file names nobody."""
        return self.creators[0] if self.creators else None

    @property
    def frame_count(self) -> int:
        """Distinct sampled positions (video frames, or collection images) with stored hashes."""
        return len({frame.frame_index for frame in self.frames})

    @property
    def is_photos(self) -> bool:
        return self.kind == KIND_PHOTOS

    @property
    def unit(self) -> str:
        """What `frame_count` counts, for human-readable output: images or frames."""
        return "images" if self.is_photos else "frames"


def pipeline_label(tag: str) -> str:
    """`ffmpeg-rgb512-pdq-v0 (superseded grid: ...)` — the tag plus what it means for the grid.

    An unknown tag is returned unchanged rather than guessed at: it was written by something this
    build does not know about, and inventing a meaning for it is exactly what the tag prevents.
    """
    note = PIPELINE_LABELS.get(tag)
    return f"{tag} ({note})" if note else tag


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def collection_sha256(digests: Iterable[str]) -> str:
    """Identity of a photo collection: one digest over its images' sha256 digests, sorted.

    Order-independent (the same folder listed in any order gives the same id), duplicate-sensitive
    (two copies of a photo are two images) and domain-separated, so a collection id can never
    collide with a file's sha256. Adding or removing an image makes a *different* collection,
    which `import_fingerprint` then imports as a second reference — see ADR 0020.
    """
    values = sorted(digests)
    if not values:
        raise ValueError("a photo collection needs at least one image")
    for digest in values:
        if not _SHA256.fullmatch(digest):
            raise ValueError("image digests must be 64 lowercase hex characters")
    hasher = hashlib.sha256(COLLECTION_DIGEST_PREFIX)
    hasher.update("\n".join(values).encode())
    return hasher.hexdigest()


def collection_digest(paths: Sequence[Path]) -> str:
    """`collection_sha256` of the files themselves; the client uses it to skip finished work."""
    return collection_sha256(file_sha256(path) for path in paths)


def _ffmpeg_version() -> str:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    lines = result.stdout.splitlines()
    return lines[0].strip() if result.returncode == 0 and lines else "unknown"


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def client_info(*, ffmpeg: bool = True) -> dict[str, str]:
    """Software that computed the hashes. Deliberately no hostname, user name or paths.

    `ffmpeg=False` for a photo collection: Pillow decodes it and ffmpeg never runs, so the
    Pillow version is what a later parity check needs instead.
    """
    decoder = {"ffmpeg": _ffmpeg_version()} if ffmpeg else {"pillow": _package_version("pillow")}
    info = {
        "tool": f"marceline-fingerprint {TOOL_VERSION}",
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        **decoder,
        "pdqhash": _package_version("pdqhash"),
    }
    return {key: value[:MAX_CLIENT_VALUE_LENGTH] for key, value in info.items()}


def _embedding_tag(embed: Embedder | None, pipeline: str) -> str:
    """The embedding tag a run may record: a sketch without its recipe is unusable (ADR 0022).

    Embedding and tag come as a pair — `matching.embedding.EMBEDDING_PIPELINE` by default — and a
    tag this build does not know is refused rather than written into a reference that would then
    claim a grid nobody can reproduce.
    """
    if embed is None:
        if pipeline:
            raise ValueError("embedding_pipeline was given but nothing computes an embedding")
        return ""
    tag = pipeline or EMBEDDING_PIPELINE
    if tag not in EMBEDDING_PIPELINES:
        raise ValueError(f"unknown embedding pipeline {tag!r}")
    return tag


def _hash_position(
    image: Image.Image,
    index: int,
    timestamp_s: float,
    dihedral: bool,
    embed: Embedder | None = None,
    grid: bytes | None = None,
) -> list[FingerprintFrame]:
    """PDQ hashes of one position, in all eight orientations or only the original.

    Returns nothing for an image below MIN_QUALITY (blank, near-black, featureless).
    With `embed`, the unflipped orientation also carries this picture's sketch (ADR 0037): one
    per moment, because an embedding is roughly flip-invariant where PDQ is not. With `grid`, it
    also carries this moment's crop lattice (ADR 0039) — one per moment for the opposite reason:
    PDQ is not flip invariant, but mirroring stays the job of the eight orientations.
    """
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    if dihedral:
        variants, quality = pdqhash.compute_dihedral(
            np.ascontiguousarray(np.asarray(rgb, dtype=np.uint8))
        )
        hashes = [np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes() for bits in variants]
    else:
        result = hash_image(rgb)
        hashes, quality = [result.hash], result.quality
    if quality < MIN_QUALITY:
        return []
    sketch = embed(rgb) if embed is not None else None
    return [
        FingerprintFrame(
            frame_index=index,
            timestamp_s=timestamp_s,
            dihedral=orientation,
            quality=int(quality),
            pdq=value,
            embedding=sketch if orientation == EMBEDDING_ORIENTATION else None,
            crop_grid=grid if orientation == CROP_GRID_ORIENTATION else None,
        )
        for orientation, value in enumerate(hashes)
    ]


def _crop_grid_options(options: CropGrid | None) -> tuple[CropGrid, list[crop_grid.Crop]] | None:
    """Validate a run's lattice and hand back its crop specs in stored order, or None."""
    if options is None:
        return None
    specs = crop_grid.grid_specs(options.step, options.max_trim)
    if options.stage1 != crop_grid.STAGE1_CROPS:
        raise ValueError(
            f"a crop grid's stage-1 prefix is {crop_grid.STAGE1_CROPS} crops, got {options.stage1}"
        )
    if len(specs) < options.stage1:
        raise ValueError(f"a lattice of {len(specs)} crops cannot hold a {options.stage1} prefix")
    return options, specs


def fingerprint_video(
    path: Path,
    *,
    fps: float = 1.0,
    dihedral: bool = True,
    title: str | None = None,
    creators: Sequence[str] = (),
    sha256: str | None = None,
    timeout_s: float = FULL_VIDEO_TIMEOUT_S,
    progress: Callable[[int], None] | None = None,
    file_stat: os.stat_result | None = None,
    embed: Embedder | None = None,
    embedding_pipeline: str = "",
    grid: CropGrid | None = None,
) -> Fingerprint:
    """Hash a whole local video at `fps` samples per second.

    `creators` names the performers, the primary creator (rights holder) first; the server resolves
    the names at import time, not here.
    `sha256` is an already computed digest of the file (skips the first read). A file changed
    while hashing raises ValueError. With `file_stat` — `path.stat()` taken *before* that digest
    was computed — the check compares size and modification time instead of reading the whole
    file a second time (a multi-GB original on a laptop); without it the digest is re-read.
    `progress` is called with the number of sampled frames so far.
    `embed` adds the second signal's sketch to every sampled moment and `embedding_pipeline`
    records which recipe made them; both or neither (ADR 0037).

    `grid` switches the whole recipe to the reference-side crop grid (ADR 0039) and the result
    carries `CROP_GRID_PIPELINE` rather than `PIPELINE`, because it really is a different recipe:
    ffmpeg hands back the **master** frame instead of the 512 px one, and every crop — the
    uncropped one included — is scaled by Pillow before PDQ. Cutting the crops from the 512 px
    picture would cost a median of 8 bits (§8.1), which is the whole reason for the detour. A
    moment whose master is narrower than `crop_grid.MIN_SOURCE_WIDTH`, or whose corner crops fall
    below the quality floor, gets **no grid** rather than a worse one; it still gets its hashes.

    Decode failures raise `matching.pdq.MediaDecodeError`.
    """
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be finite and positive")
    embedding_pipeline = _embedding_tag(embed, embedding_pipeline)
    lattice = _crop_grid_options(grid)
    names = validate_creators(tuple(creators))
    if title is not None and not 0 < len(title) <= MAX_TITLE_LENGTH:
        raise ValueError(f"title must be 1..{MAX_TITLE_LENGTH} characters")
    trusted_stat = file_stat if sha256 is not None else None
    before = trusted_stat or path.stat()
    digest = sha256 or file_sha256(path)
    file_size = before.st_size
    duration = _probe_duration(path.resolve(), FFMPEG_TIMEOUT_S)
    if not math.isfinite(duration):
        raise ValueError("video duration must be finite")
    frames: list[FingerprintFrame] = []
    # The grid needs the master frame; everything else is hashed from ffmpeg's 512 px output.
    width = None if lattice is not None else FRAME_MAX_WIDTH
    for index, image in iter_video_frames(path, fps=fps, timeout_s=timeout_s, max_width=width):
        if progress is not None:
            progress(index + 1)
        picture, blob = image, None
        if lattice is not None:
            options, specs = lattice
            if image.width < crop_grid.MIN_SOURCE_WIDTH:
                # A property of the file, not of this moment, so it fails the run rather than
                # producing an original that claims a grid recipe and carries almost no grids.
                raise ValueError(
                    f"a crop grid needs frames at least {crop_grid.MIN_SOURCE_WIDTH} px wide and "
                    f"this original is {image.width} px "
                    f"(docs/matching/reference-crop-grid.md §8.1); fingerprint it without the grid"
                )
            built = crop_grid.build(image, specs=specs, dedup_bits=options.dedup_bits)
            # None means the quality floor would shift the blob's prefix — a near-blank moment.
            # Unlike a missing embedding, a missing grid is harmless: that moment simply keeps
            # matching on its plain hash, exactly as it does today.
            blob = built.blob if built is not None else None
            # Hash the moment itself the way the grid's crops are hashed, grid or no grid: one
            # recipe per pipeline tag, always, so the stored hash is entry 0 of the blob.
            picture = crop_grid.scale_to_output(image)
        frames.extend(_hash_position(picture, index, index / fps, dihedral, embed, blob))
    # Refuse a file modified while hashing rather than associate hashes with the wrong digest.
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ValueError("File changed during import")
    if trusted_stat is None and file_sha256(path) != digest:
        raise ValueError("File changed during import")
    return Fingerprint(
        kind=KIND_VIDEO,
        sha256=digest,
        file_size=file_size,
        duration_s=duration,
        fps=fps,
        dihedral=dihedral,
        pipeline=CROP_GRID_PIPELINE if lattice is not None else PIPELINE,
        title=title or path.stem[:MAX_TITLE_LENGTH],
        creators=names,
        client=client_info(),
        frames=tuple(frames),
        embedding_pipeline=embedding_pipeline,
        crop_grid=lattice[0] if lattice is not None else None,
    )


def fingerprint_photos(
    paths: Sequence[Path],
    *,
    dihedral: bool = True,
    title: str | None = None,
    creators: Sequence[str] = (),
    progress: Callable[[int], None] | None = None,
    embed: Embedder | None = None,
    embedding_pipeline: str = "",
) -> Fingerprint:
    """Hash a whole photo collection: N unrelated images, one `Fingerprint` (issue #69, ADR 0020).

    One collection is one original, because a leak reposts the set, not a single picture. Images
    are hashed in sorted path order; `frame_index` is that ordinal and there is no timeline.
    Each image is read once: the same bytes produce its sha256 (which the collection digest is
    built from) and its PDQ hashes, so the digest can never describe bytes that were not hashed.
    The files are read in place and never copied; only hashes are kept.

    `title` defaults to the name of the folder holding the first image (a name, never a path).
    An image below MIN_QUALITY leaves a gap in the ordinals rather than shifting the rest.
    Decode failures raise `matching.pdq.MediaDecodeError`, naming the file (never its path).
    """
    names = validate_creators(tuple(creators))
    if title is not None and not 0 < len(title) <= MAX_TITLE_LENGTH:
        raise ValueError(f"title must be 1..{MAX_TITLE_LENGTH} characters")
    embedding_pipeline = _embedding_tag(embed, embedding_pipeline)
    ordered = sorted(paths)
    if not ordered:
        raise ValueError("a photo collection needs at least one image")
    if len(ordered) > MAX_COLLECTION_IMAGES:
        raise ValueError(f"at most {MAX_COLLECTION_IMAGES} images per collection")
    digests: list[str] = []
    frames: list[FingerprintFrame] = []
    file_size = 0
    for index, path in enumerate(ordered):
        data = path.read_bytes()
        digests.append(hashlib.sha256(data).hexdigest())
        file_size += len(data)
        if progress is not None:
            progress(index + 1)
        try:
            image = open_image(data)
        except MediaDecodeError as exc:
            raise MediaDecodeError(f"{path.name}: {exc}") from exc
        frames.extend(_hash_position(image, index, float(index), dihedral, embed))
    folder = ordered[0].parent.name
    return Fingerprint(
        kind=KIND_PHOTOS,
        sha256=collection_sha256(digests),
        file_size=file_size,
        duration_s=None,
        fps=None,
        dihedral=dihedral,
        pipeline=PHOTO_PIPELINE,
        title=(title or folder)[:MAX_TITLE_LENGTH] or "photos",
        creators=names,
        client=client_info(ffmpeg=False),
        frames=tuple(frames),
        image_count=len(ordered),
        embedding_pipeline=embedding_pipeline,
    )


def to_json(fingerprint: Fingerprint) -> dict[str, Any]:
    """The hashes-only document, always in the current format version.

    A video carries `duration_s` and `fps` and a `timestamp_s` per frame; a photo collection
    carries `image_count` and no time anywhere, because it has none.
    """
    shape: dict[str, Any] = (
        {"image_count": fingerprint.image_count}
        if fingerprint.is_photos
        else {"duration_s": fingerprint.duration_s, "fps": fingerprint.fps}
    )
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "kind": fingerprint.kind,
        "pipeline": fingerprint.pipeline,
        "embedding_pipeline": fingerprint.embedding_pipeline,
        # null for every original without a lattice, which is all of them until ADR 0039 is adopted.
        "crop_grid": None if fingerprint.crop_grid is None else fingerprint.crop_grid.as_json(),
        "sha256": fingerprint.sha256,
        "file_size": fingerprint.file_size,
        **shape,
        "dihedral": fingerprint.dihedral,
        "title": fingerprint.title,
        "creator": list(fingerprint.creators),
        "client": dict(fingerprint.client),
        "frames": [
            {
                "frame_index": frame.frame_index,
                **({} if fingerprint.is_photos else {"timestamp_s": frame.timestamp_s}),
                "dihedral": frame.dihedral,
                "quality": frame.quality,
                "pdq": frame.pdq.hex(),
                # Only the unflipped orientation carries one, and only when the run computed it.
                **({} if frame.embedding is None else {"embedding": frame.embedding.hex()}),
                # The whole lattice, hex, on the unflipped orientation of a gridded moment. This
                # is what makes a gridded file large — 16 KB of hex per moment against 512 B of
                # hashes — and it is the 629 MB ADR 0039 budgets for the transfer, doubled by hex.
                # Hex on purpose: every other hash in this format is hex, and a second encoding in
                # one file is how a reader ends up decoding the wrong thing.
                **({} if frame.crop_grid is None else {"crop_grid": frame.crop_grid.hex()}),
            }
            for frame in fingerprint.frames
        ],
    }


_COMMON_KEYS = frozenset(
    {
        "format",
        "version",
        "pipeline",
        "sha256",
        "file_size",
        "dihedral",
        "title",
        "creator",
        "client",
        "frames",
    }
)
# Version 1 and 2 files are videos and have no `kind`; version 3 requires one.
_VIDEO_KEYS = _COMMON_KEYS | {"duration_s", "fps"}
_VIDEO_KEYS_V3 = _VIDEO_KEYS | {"kind"}
_PHOTO_KEYS = _COMMON_KEYS | {"kind", "image_count"}
# Version 4 names the second signal's recipe, always ("" = this original has none).
_EMBEDDING_KEYS = frozenset({"embedding_pipeline"})
# Version 5 describes the crop lattice, always (null = this original has none).
_CROP_GRID_KEYS = frozenset({"crop_grid"})
_CROP_GRID_FIELDS = frozenset({"step", "max_trim", "stage1", "dedup_bits"})
_FRAME_KEYS = frozenset({"frame_index", "timestamp_s", "dihedral", "quality", "pdq"})
# A collection image has no timestamp: the position is the ordinal in the collection.
_IMAGE_KEYS = frozenset({"frame_index", "dihedral", "quality", "pdq"})
# `embedding` and `crop_grid` are optional per frame even in the versions that introduce them:
# only the unflipped orientation carries either.
_OPTIONAL_FRAME_KEYS = frozenset({"embedding", "crop_grid"})


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise FingerprintFormatError(f"{name} must be an integer")
    return value


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FingerprintFormatError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise FingerprintFormatError(f"{name} must be finite and positive")
    return number


def _keys(
    data: Mapping[str, object],
    expected: frozenset[str],
    name: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    present = set(data)
    if not expected <= present or not present <= expected | optional:
        missing = sorted(expected - present)
        extra = sorted(present - expected - optional)
        raise FingerprintFormatError(f"{name}: missing keys {missing}, unknown keys {extra}")


def _timestamp(data: Mapping[str, object], index: int, fps: float) -> float:
    timestamp = data["timestamp_s"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
        raise FingerprintFormatError("timestamp_s must be a number")
    if not math.isclose(float(timestamp), index / fps, rel_tol=0, abs_tol=TIMESTAMP_TOLERANCE):
        raise FingerprintFormatError(f"timestamp_s of frame {index} is not frame_index / fps")
    return float(timestamp)


def _embedding(data: Mapping[str, object], orientation: int, pipeline: str) -> bytes | None:
    """One frame's sketch, or None. Refuses the combinations that could not have been produced.

    A sketch without a document-level recipe could never be compared with anything, and a sketch
    on a flipped orientation was not produced by this pipeline at all (ADR 0037).
    """
    value = data.get("embedding")
    if value is None:
        return None
    if not pipeline:
        raise FingerprintFormatError("a frame carries an embedding but the file names no recipe")
    if orientation != EMBEDDING_ORIENTATION:
        raise FingerprintFormatError(
            f"only dihedral {EMBEDDING_ORIENTATION} may carry an embedding, got {orientation}"
        )
    if not isinstance(value, str) or not _SKETCH_HEX.fullmatch(value):
        raise FingerprintFormatError(
            f"embedding must be {SKETCH_BYTES * 2} lowercase hex characters"
        )
    return bytes.fromhex(value)


def _crop_grid_blob(
    data: Mapping[str, object], orientation: int, grid: CropGrid | None
) -> bytes | None:
    """One moment's lattice, or None. Refuses everything that could not have been produced.

    Strict on purpose: a blob is read by position — stage 1 takes its first `stage1` entries — so a
    blob of the wrong length, on a flipped orientation, or in a document that names no lattice is a
    blob whose entries mean something other than what the reader will assume.
    """
    value = data.get("crop_grid")
    if value is None:
        return None
    if grid is None:
        raise FingerprintFormatError("a frame carries a crop grid but the file names no lattice")
    if orientation != CROP_GRID_ORIENTATION:
        raise FingerprintFormatError(
            f"only dihedral {CROP_GRID_ORIENTATION} may carry a crop grid, got {orientation}"
        )
    if not isinstance(value, str) or not _GRID_HEX.fullmatch(value):
        raise FingerprintFormatError(
            f"crop_grid must be a whole number of {PDQ_HASH_BYTES}-byte hashes in lowercase hex"
        )
    blob = bytes.fromhex(value)
    crops = len(blob) // PDQ_HASH_BYTES
    if crops < grid.stage1:
        raise FingerprintFormatError(
            f"crop_grid holds {crops} crops, fewer than the {grid.stage1} stage-1 crops it must "
            "begin with"
        )
    # Pruning only ever removes crops from the tail, so the whole lattice is the upper bound.
    largest = len(crop_grid.grid_specs(grid.step, grid.max_trim))
    if crops > largest:
        raise FingerprintFormatError(
            f"crop_grid holds {crops} crops, more than the {largest} of its own lattice"
        )
    return blob


def _frame(
    data: object,
    kind: str,
    fps: float | None,
    max_index: float,
    dihedral: bool,
    pipeline: str = "",
    grid: CropGrid | None = None,
) -> FingerprintFrame:
    photos = kind == KIND_PHOTOS
    if not isinstance(data, dict):
        raise FingerprintFormatError("frame must be an object")
    _keys(
        data,
        _IMAGE_KEYS if photos else _FRAME_KEYS,
        "image" if photos else "frame",
        _OPTIONAL_FRAME_KEYS,
    )
    index = _int(data["frame_index"], "frame_index")
    if not 0 <= index <= max_index:
        raise FingerprintFormatError(
            f"frame_index {index} outside the " + ("collection" if photos else "video duration")
        )
    # A collection has no time: the position of an image is its ordinal, and that is all.
    timestamp = float(index) if photos or fps is None else _timestamp(data, index, fps)
    orientation = _int(data["dihedral"], "dihedral")
    if not 0 <= orientation < (DIHEDRAL_ORIENTATIONS if dihedral else 1):
        raise FingerprintFormatError(f"dihedral {orientation} out of range")
    quality = _int(data["quality"], "quality")
    if not MIN_QUALITY <= quality <= 100:
        raise FingerprintFormatError(f"quality {quality} outside {MIN_QUALITY}..100")
    value = data["pdq"]
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise FingerprintFormatError(f"pdq must be {PDQ_HASH_BYTES * 2} lowercase hex characters")
    sketch = _embedding(data, orientation, pipeline)
    blob = _crop_grid_blob(data, orientation, grid)
    pdq = bytes.fromhex(value)
    # The stored hash is entry 0 of the blob by construction (`crop_grid.build`), so a file where
    # it is not was assembled by something other than this pipeline and would send stage 2 looking
    # for a moment the row does not describe.
    if blob is not None and blob[:PDQ_HASH_BYTES] != pdq:
        raise FingerprintFormatError(
            f"frame {index}: the crop grid does not begin with this position's own hash"
        )
    return FingerprintFrame(index, timestamp, orientation, quality, pdq, sketch, blob)


def _client(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > MAX_CLIENT_ENTRIES:
        raise FingerprintFormatError(f"client must be an object with <= {MAX_CLIENT_ENTRIES} keys")
    for key, text in value.items():
        if not _CLIENT_KEY.fullmatch(key):
            raise FingerprintFormatError(f"invalid client key {key!r}")
        if not isinstance(text, str) or len(text) > MAX_CLIENT_VALUE_LENGTH:
            raise FingerprintFormatError(f"client {key} must be a short string")
    return dict(value)


def _title(value: object, name: str) -> str:
    if not isinstance(value, str) or not 0 < len(value.strip()) <= MAX_TITLE_LENGTH:
        raise FingerprintFormatError(f"{name} must be 1..{MAX_TITLE_LENGTH} characters")
    return value


def validate_creators(values: Sequence[str]) -> tuple[str, ...]:
    """Performer names as a fingerprint stores them: 1..255 characters, unique, at most 20.

    Order is meaningful — the first name is the primary creator. Duplicates are compared
    case-insensitively, like `matching.references.resolve_creator` resolves them.
    """
    if len(values) > MAX_CREATORS:
        raise FingerprintFormatError(f"at most {MAX_CREATORS} creators per fingerprint")
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _title(value, "creator")
        if name.casefold() in seen:
            raise FingerprintFormatError(f"duplicate creator {name!r}")
        seen.add(name.casefold())
        names.append(name)
    return tuple(names)


def _creators(value: object, version: int) -> tuple[str, ...]:
    """`creator` holds one optional name in version 1 and a list of names in version 2."""
    if version == 1:
        return () if value is None else (_title(value, "creator"),)
    if not isinstance(value, list):
        raise FingerprintFormatError("creator must be a list of names in format version 2")
    return validate_creators(value)


def _kind(data: Mapping[str, object], version: int) -> str:
    """Version 3 names the kind; version 1 and 2 files are always videos."""
    if version < KIND_VERSION:
        return KIND_VIDEO
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise FingerprintFormatError(f"unknown kind {kind!r}")
    return kind


def _embedding_pipeline(data: Mapping[str, object]) -> str:
    """The document's embedding recipe: "", or a tag this build knows (ADR 0022's rule, 0037's tag).

    An unknown tag is refused rather than stored: the whole point of a tag is that nobody has to
    guess what the bits mean, and a reference carrying a recipe we cannot reproduce would be
    compared with page sketches it was never comparable to.
    """
    value = data.get("embedding_pipeline", "")
    if not isinstance(value, str):
        raise FingerprintFormatError("embedding_pipeline must be a string")
    if value and value not in EMBEDDING_PIPELINES:
        raise FingerprintFormatError(f"unknown embedding pipeline {value!r}")
    return value


def _crop_grid(data: Mapping[str, object], kind: str, pipeline: str) -> CropGrid | None:
    """The document's lattice, or None. Strict, because a blob is read by position (ADR 0039).

    The step and the box are what a reader needs to know how many crops a whole blob holds, and
    `stage1` is how many of them the searchable index takes. A lattice this build cannot reproduce
    is refused rather than guessed at, exactly as an unknown pipeline tag is.
    """
    value = data.get("crop_grid")
    if value is None:
        return None
    if kind != KIND_VIDEO:
        raise FingerprintFormatError("only a video may carry a crop grid")
    if pipeline not in CROP_GRID_PIPELINES:
        raise FingerprintFormatError(f"pipeline {pipeline!r} does not produce crop grids")
    if not isinstance(value, dict):
        raise FingerprintFormatError("crop_grid must be an object or null")
    _keys(value, _CROP_GRID_FIELDS, "crop_grid")
    step = _positive_float(value["step"], "crop_grid.step")
    max_trim = _positive_float(value["max_trim"], "crop_grid.max_trim")
    stage1 = _int(value["stage1"], "crop_grid.stage1")
    dedup = value["dedup_bits"]
    if dedup is not None:
        dedup = _int(dedup, "crop_grid.dedup_bits")
        if not 0 < dedup <= 256:
            raise FingerprintFormatError("crop_grid.dedup_bits must be 1..256 or null")
    try:
        crop_grid.grid_specs(step, max_trim)
    except ValueError as exc:
        raise FingerprintFormatError(f"crop_grid: {exc}") from exc
    if stage1 != crop_grid.STAGE1_CROPS:
        raise FingerprintFormatError(
            f"crop_grid.stage1 must be {crop_grid.STAGE1_CROPS}, got {stage1}"
        )
    return CropGrid(step=step, max_trim=max_trim, stage1=stage1, dedup_bits=dedup)


def _image_count(value: object) -> int:
    count = _int(value, "image_count")
    if not 0 < count <= MAX_COLLECTION_IMAGES:
        raise FingerprintFormatError(f"image_count must be 1..{MAX_COLLECTION_IMAGES}")
    return count


def from_json(data: object) -> Fingerprint:
    """Validate a decoded fingerprint file strictly; raises FingerprintFormatError."""
    if not isinstance(data, dict):
        raise FingerprintFormatError("fingerprint must be a JSON object")
    if data.get("format") != FORMAT:
        raise FingerprintFormatError(f"not a {FORMAT} file")
    version = data.get("version")
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise FingerprintFormatError(f"unsupported format version {version!r}")
    kind = _kind(data, version)
    photos = kind == KIND_PHOTOS
    video_keys = _VIDEO_KEYS_V3 if version >= KIND_VERSION else _VIDEO_KEYS
    expected = _PHOTO_KEYS if photos else video_keys
    if version >= EMBEDDING_VERSION:
        expected = expected | _EMBEDDING_KEYS
    if version >= CROP_GRID_VERSION:
        expected = expected | _CROP_GRID_KEYS
    _keys(data, expected, "fingerprint")
    embedding_pipeline = _embedding_pipeline(data)
    # A pipeline tag is a promise about how the bits were made; a photo recipe can never have
    # produced a video's frames, so the tags may not cross kinds (ADR 0020).
    if data["pipeline"] not in PIPELINES_BY_KIND[kind]:
        raise FingerprintFormatError(f"pipeline {data['pipeline']!r} is not a {kind} pipeline")
    grid = _crop_grid(data, kind, str(data["pipeline"]))
    sha256 = data["sha256"]
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise FingerprintFormatError("sha256 must be 64 lowercase hex characters")
    file_size = _int(data["file_size"], "file_size")
    if file_size <= 0:
        raise FingerprintFormatError("file_size must be positive")
    duration = None if photos else _positive_float(data["duration_s"], "duration_s")
    fps = None if photos else _positive_float(data["fps"], "fps")
    image_count = _image_count(data["image_count"]) if photos else None
    dihedral = data["dihedral"]
    if not isinstance(dihedral, bool):
        raise FingerprintFormatError("dihedral must be true or false")
    title = _title(data["title"], "title")
    creators = _creators(data["creator"], version)
    raw_frames = data["frames"]
    if not isinstance(raw_frames, list):
        raise FingerprintFormatError("frames must be a list")
    max_index = (
        image_count - 1
        if image_count is not None
        else (duration or 0) * (fps or 1) + FRAME_INDEX_SLACK
    )
    frames = tuple(
        _frame(item, kind, fps, max_index, dihedral, embedding_pipeline, grid)
        for item in raw_frames
    )
    orientations: dict[int, set[int]] = {}
    qualities: dict[int, int] = {}
    for frame in frames:
        seen = orientations.setdefault(frame.frame_index, set())
        if frame.dihedral in seen:
            raise FingerprintFormatError(
                f"duplicate frame {frame.frame_index} orientation {frame.dihedral}"
            )
        seen.add(frame.dihedral)
        if qualities.setdefault(frame.frame_index, frame.quality) != frame.quality:
            raise FingerprintFormatError(f"frame {frame.frame_index} has mixed qualities")
    wanted = set(range(DIHEDRAL_ORIENTATIONS if dihedral else 1))
    for index, seen in orientations.items():
        if seen != wanted:
            raise FingerprintFormatError(f"frame {index} does not have orientations {wanted}")
    # A file that names a recipe must carry a sketch for every position it hashed: half a second
    # signal is worse than none, because nothing downstream could tell the gap from "not similar".
    if embedding_pipeline:
        without = sorted(
            frame.frame_index
            for frame in frames
            if frame.dihedral == EMBEDDING_ORIENTATION and frame.embedding is None
        )
        if without:
            raise FingerprintFormatError(
                f"embedding_pipeline {embedding_pipeline!r} but {len(without)} position(s) carry "
                f"no embedding (first: {without[0]})"
            )
    # A crop grid is deliberately NOT all-or-nothing the way a sketch is. A moment whose corner
    # crops fall below the quality floor gets no grid, and that is harmless: the moment keeps
    # matching on its plain hash exactly as it does today, where a missing sketch would be
    # indistinguishable from "not similar". A file that names a lattice and carries not one blob
    # is still a mistake, so that one is refused.
    if grid is not None and not any(frame.crop_grid for frame in frames):
        raise FingerprintFormatError("the file names a crop lattice but carries no crop grid")
    return Fingerprint(
        kind=kind,
        sha256=sha256,
        file_size=file_size,
        duration_s=duration,
        fps=fps,
        dihedral=dihedral,
        pipeline=str(data["pipeline"]),
        title=title,
        creators=creators,
        client=_client(data["client"]),
        frames=frames,
        image_count=image_count,
        embedding_pipeline=embedding_pipeline,
        crop_grid=grid,
    )


@dataclass(frozen=True)
class Parity:
    """Hash agreement between two fingerprints of the same file, per (frame_index, dihedral)."""

    both: int
    only_first: int
    only_second: int
    identical_share: float
    p50: int
    p90: int
    p99: int
    max: int


def compare_frames(
    first: Iterable[tuple[int, int, bytes]], second: Iterable[tuple[int, int, bytes]]
) -> Parity:
    """Compare (frame_index, dihedral, pdq) rows; distance stats are 0 without common pairs."""
    left = {(index, orientation): value for index, orientation, value in first}
    right = {(index, orientation): value for index, orientation, value in second}
    common = sorted(left.keys() & right.keys())
    distances = np.array(
        [hamming_distance(left[key], right[key]) for key in common], dtype=np.int64
    )

    def percentile(q: float) -> int:
        return int(np.percentile(distances, q, method="higher")) if len(distances) else 0

    return Parity(
        both=len(common),
        only_first=len(left.keys() - right.keys()),
        only_second=len(right.keys() - left.keys()),
        identical_share=float(np.mean(distances == 0)) if len(distances) else 0.0,
        p50=percentile(50),
        p90=percentile(90),
        p99=percentile(99),
        max=int(distances.max()) if len(distances) else 0,
    )
