# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "numpy>=2.3",
#     "onnxruntime>=1.30,<1.31",
#     "pdqhash>=0.2.8,<0.3",
#     "pillow>=12.3,<13",
# ]
# ///
"""Fingerprint originals locally; write hashes-only JSON files for `import_fingerprints`.

Usage: uv run client/fingerprint.py PATH [PATH ...] [--photos] [--out DIR] [--creator NAME ...]
       uv run client/fingerprint.py PATH --embeddings   # + the second signal (ADR 0037)
       uv run client/fingerprint.py PATH --crop-grid    # + the crop grid (ADR 0039)

Videos (default): one JSON file per video. Photo collections (`--photos`): one JSON file per
folder of images — a whole set is one original, because a leak reposts the set (issue #69).

`--creator` is repeatable: the first name is the primary creator (rights holder), the rest are
co-creators, and all of them end up in the JSON file.

`--crop-grid` adds the reference-side crop lattice (ADR 0039): 256 crops of every sampled
moment, hashed and stored as one blob, which is what recovers the 1-3 % asymmetric re-frame the
sites apply. It is a different recipe — the frames come out of ffmpeg at the **master** width and
are scaled in Pillow — so the result carries its own pipeline tag and imports *alongside* an
existing reference of the same file instead of replacing it. It costs about 1.9 s of CPU per
sampled second of video and 8.2 KB per moment, so plan for hours, not minutes: about 5 h on 8
cores for all 44 originals. The server only consults the grids once the operator switches
`MATCHING_REFERENCE_CROP_GRID` on, and adopting them means re-running `calibrate_confidence`.

`--embeddings` adds the second content signal (ADR 0037): one 512-bit sign sketch per moment,
computed by the same pinned ONNX image encoder the server uses. It needs the model file, which is
downloaded once with `--fetch-model` (45 MB, checked against its digest) and is never in the
repository. A file re-fingerprinted with embeddings is imported *onto the existing reference* —
the frame grid is unchanged, so only the sketches are added.

Runs the server's own hashing code (`backend/matching/fingerprint.py`) without Django.
The only network access is that one model download, and only when you ask for it. Output files
hold sha256, size, duration or image count, title, PDQ hashes and (with `--embeddings`) the
sketches, never video or image data, paths, host or user names. Re-running skips work already
written.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from matching import (
    crop_grid,
    embedding,
)
from matching.fingerprint import (
    IMAGE_SUFFIXES,
    CropGrid,
    collection_digest,
    file_sha256,
    fingerprint_photos,
    fingerprint_video,
    to_json,
)
from matching.pdq import MediaDecodeError

VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".mpg", ".mpeg", ".ts"}
)
# What the server's Pillow decodes; the backend owns the list so both sides always agree.
IMAGE_EXTENSIONS = frozenset(IMAGE_SUFFIXES)
# Whole originals can be long 4K files on a laptop; the server's one-hour watchdog is too tight.
TIMEOUT_S = 12 * 3600.0
PROGRESS_EVERY_FRAMES = 600
PROGRESS_EVERY_IMAGES = 50
REMOTE_HOST = "devbox"
REMOTE_DIR = "marceline-fingerprints"


@dataclass(frozen=True)
class Outcome:
    path: Path
    status: str  # done | skipped | failed
    detail: str


@dataclass(frozen=True)
class Collection:
    """One photo collection: the images of one folder, and the title they are stored under."""

    title: str
    folder: Path
    images: tuple[Path, ...]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def is_hidden(name: str) -> bool:
    return name.startswith(".")  # includes macOS AppleDouble "._*" files


def find_files(
    paths: list[Path], extensions: frozenset[str]
) -> tuple[list[Path], list[Path]]:
    """(files, missing paths). Directories are walked recursively, skipping hidden entries.

    A file named on the command line is taken as given; only walked directories are filtered by
    extension.
    """
    found: list[Path] = []
    missing: list[Path] = []
    for path in paths:
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(name for name in dirs if not is_hidden(name))
                found.extend(
                    Path(root) / name
                    for name in sorted(files)
                    if not is_hidden(name) and Path(name).suffix.lower() in extensions
                )
        else:
            missing.append(path)
    unique = {item.resolve(): item for item in found}
    return list(unique.values()), missing


def find_collections(paths: list[Path]) -> tuple[list[Collection], list[Path]]:
    """One collection per folder on the command line, plus one for loose image files.

    The second list holds paths that hold nothing to hash: a missing path, or a folder without
    images. A collection is titled after its folder, never after its path.
    """
    collections: list[Collection] = []
    problems: list[Path] = []
    loose: list[Path] = []
    for path in paths:
        if path.is_dir():
            images, _ = find_files([path], IMAGE_EXTENSIONS)
            if images:
                collections.append(
                    Collection(path.name or "photos", path, tuple(sorted(images)))
                )
            else:
                problems.append(path)
        elif path.is_file():
            loose.append(path)
        else:
            problems.append(path)
    if loose:
        unique = sorted({item.resolve(): item for item in loose}.values())
        folder = unique[0].parent
        collections.append(Collection(folder.name or "photos", folder, tuple(unique)))
    return collections, problems


def safe_stem(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80] or "video"


def rate_suffix(fps: float) -> str:
    """File name part for the sampling rate: none at the default 1 fps, `.2fps` otherwise.

    A 2 fps run of an already fingerprinted video is a different, denser grid (ADR 0022
    amendment), so it gets its own file instead of being skipped as finished work.
    """
    return "" if fps == 1 else f".{fps:g}fps"


def existing_output(out: Path, digest: str, suffix: str = "") -> Path | None:
    return next(iter(sorted(out.glob(f"*.{digest[:12]}{suffix}.json"))), None)


def embedding_suffix(embeddings: bool) -> str:
    """File name part for an embedded run, so it is not skipped as finished work (ADR 0037).

    A video already fingerprinted without the second signal must be fingerprinted again *with*
    it, and the result is a different document: it gets its own file, and the import attaches its
    sketches to the existing reference.
    """
    return ".emb" if embeddings else ""


def crop_grid_suffix(grid: bool) -> str:
    """File name part for a gridded run (ADR 0039), so it is not skipped as finished work.

    A gridded file is a different recipe under a different pipeline tag, so it imports beside the
    plain reference rather than replacing it (ADR 0022) and must not be mistaken for it on disk.
    """
    return ".grid" if grid else ""


def crop_grid_options(grid: bool, dedup_bits: int | None) -> CropGrid | None:
    """The lattice a run cuts, or None. 256 crops (step 2 %, max 6 % per edge) is the measurement.

    `dedup_bits` is the optional corner-seeded pruning of `reference-crop-grid.md` §3.3: at 24 bits
    a median of 139 of 256 crops survive (4.3 KB per moment instead of 8.2) for 0.03 of recall at a
    threshold of 31 and no precision at all. Not recommended at 44 originals; worth it before the
    library reaches a few hundred.
    """
    if not grid:
        return None
    return CropGrid(
        step=crop_grid.STEP,
        max_trim=crop_grid.MAX_TRIM,
        stage1=crop_grid.STAGE1_CROPS,
        dedup_bits=dedup_bits,
    )


def sketch_function(embeddings: bool) -> object:
    """The encoder's sketch function, or None. Built per worker process (the model is 45 MB)."""
    if not embeddings:
        return None
    return embedding.encoder().sketch


def write_atomic(path: Path, data: object) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
        handle.write("\n")
    tmp.replace(path)


def process(
    path: Path,
    out: Path,
    fps: float,
    dihedral: bool,
    title: str | None,
    creators: Sequence[str],
    embeddings: bool = False,
    grid: bool = False,
    dedup_bits: int | None = None,
) -> Outcome:
    """Fingerprint one video (runs in a worker process)."""
    started = time.monotonic()
    try:
        # Taken before the digest: fingerprint_video then checks size and mtime against it instead
        # of reading the whole file a second time after decoding.
        before = path.stat()
        digest = file_sha256(path)
        suffix = (
            rate_suffix(fps) + embedding_suffix(embeddings) + crop_grid_suffix(grid)
        )
        if (done := existing_output(out, digest, suffix)) is not None:
            return Outcome(path, "skipped", f"already fingerprinted ({done.name})")
        log(f"start {path.name}")

        def progress(frames: int) -> None:
            if frames % PROGRESS_EVERY_FRAMES == 0:
                log(f"  {path.name}: {frames} frames sampled")

        fingerprint = fingerprint_video(
            path,
            fps=fps,
            dihedral=dihedral,
            title=title,
            creators=creators,
            sha256=digest,
            timeout_s=TIMEOUT_S,
            progress=progress,
            file_stat=before,
            embed=sketch_function(embeddings),  # type: ignore[arg-type]
            grid=crop_grid_options(grid, dedup_bits),
        )
        target = out / f"{safe_stem(path.stem)}.{digest[:12]}{suffix}.json"
        write_atomic(target, to_json(fingerprint))
    except (OSError, ValueError, MediaDecodeError) as exc:
        return Outcome(path, "failed", str(exc))
    elapsed = time.monotonic() - started
    # A gridded run costs hours, so the line says what it cost per sampled second: that is the
    # number the operator plans the other 43 originals with (ADR 0039 measured ~1.9 s at 256 crops).
    rate = (
        ""
        if not fingerprint.frame_count
        else f", {elapsed / fingerprint.frame_count:.2f} s/frame"
    )
    grids = sum(1 for frame in fingerprint.frames if frame.crop_grid)
    blobs = "" if not grids else f", {grids} crop grids"
    return Outcome(
        path,
        "done",
        f"{fingerprint.frame_count} frames, {fingerprint.duration_s or 0:.0f} s video"
        f"{blobs}, {elapsed:.0f} s elapsed{rate} -> {target.name}",
    )


def process_collection(
    collection: Collection,
    out: Path,
    dihedral: bool,
    creators: Sequence[str],
    embeddings: bool = False,
) -> Outcome:
    """Fingerprint one photo collection into a single file (runs in a worker process)."""
    started = time.monotonic()
    try:
        digest = collection_digest(collection.images)
        if (
            done := existing_output(out, digest, embedding_suffix(embeddings))
        ) is not None:
            return Outcome(
                collection.folder, "skipped", f"already fingerprinted ({done.name})"
            )
        log(f"start {collection.title}: {len(collection.images)} images")

        def progress(images: int) -> None:
            if images % PROGRESS_EVERY_IMAGES == 0:
                log(f"  {collection.title}: {images} images hashed")

        fingerprint = fingerprint_photos(
            collection.images,
            dihedral=dihedral,
            title=collection.title,
            creators=creators,
            progress=progress,
            embed=sketch_function(embeddings),  # type: ignore[arg-type]
        )
        target = (
            out / f"{safe_stem(collection.title)}.{fingerprint.sha256[:12]}"
            f"{embedding_suffix(embeddings)}.json"
        )
        write_atomic(target, to_json(fingerprint))
    except (OSError, ValueError, MediaDecodeError) as exc:
        return Outcome(collection.folder, "failed", str(exc))
    elapsed = time.monotonic() - started
    return Outcome(
        collection.folder,
        "done",
        f"{fingerprint.frame_count} of {fingerprint.image_count} images hashed, "
        f"{elapsed:.0f} s elapsed -> {target.name}",
    )


def run_all(
    videos: list[Path], args: argparse.Namespace, title: str | None
) -> Iterator[Outcome]:
    jobs = (
        args.out,
        args.fps,
        not args.no_dihedral,
        title,
        tuple(args.creators or ()),
        args.embeddings,
        args.crop_grid,
        args.crop_grid_dedup,
    )
    if args.jobs == 1:
        for video in videos:
            yield process(video, *jobs)
        return
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(process, video, *jobs) for video in videos]
        for future in as_completed(futures):
            yield future.result()


def run_all_collections(
    collections: list[Collection], args: argparse.Namespace
) -> Iterator[Outcome]:
    jobs = (args.out, not args.no_dihedral, tuple(args.creators or ()), args.embeddings)
    if args.jobs == 1:
        for collection in collections:
            yield process_collection(collection, *jobs)
        return
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(process_collection, item, *jobs) for item in collections]
        for future in as_completed(futures):
            yield future.result()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="fingerprint.py",
        description="Fingerprint original videos or photo collections into hashes-only JSON.",
    )
    # Optional, because `--fetch-model` does its one job and exits without looking at any path.
    result.add_argument(
        "paths", nargs="*", type=Path, metavar="PATH", help="files or folders"
    )
    result.add_argument(
        "--photos",
        action="store_true",
        help="treat every folder as one photo collection (jpg png webp avif) instead of videos",
    )
    result.add_argument(
        "--out", type=Path, default=Path("fingerprints"), help="output folder"
    )
    result.add_argument(
        "--creator",
        action="append",
        dest="creators",
        metavar="NAME",
        help="repeatable creator name or alias stored in the files: primary creator first, then "
        "co-creators (or pass them at import)",
    )
    result.add_argument(
        "--title",
        help="reference title (only with one video or collection; default: its name)",
    )
    result.add_argument(
        "--fps",
        type=float,
        default=1.0,
        help="samples per second, videos only (default 1; 2 for the denser grid, see README)",
    )
    result.add_argument(
        "--no-dihedral", action="store_true", help="only the original orientation"
    )
    result.add_argument(
        "--embeddings",
        action="store_true",
        help="also store one 512-bit embedding sketch per moment (ADR 0037); needs the model, "
        "see --fetch-model. Roughly 55 ms extra per sampled frame.",
    )
    result.add_argument(
        "--crop-grid",
        action="store_true",
        help="also store 256 crops of every moment (ADR 0039), videos only; recovers the 1-3 %% "
        "re-frame the sites apply. About 1.9 s of CPU per sampled second and 8.2 KB per moment, "
        "and it imports alongside the plain reference under its own pipeline tag.",
    )
    result.add_argument(
        "--crop-grid-dedup",
        type=int,
        metavar="BITS",
        default=None,
        help=f"prune crops within BITS of one already kept ({crop_grid.DEDUP_BITS} halves the "
        "blob for 0.03 of recall at a threshold of 31); off by default",
    )
    result.add_argument(
        "--fetch-model",
        action="store_true",
        help="download and verify the embedding model (once per machine), then exit",
    )
    result.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="videos or collections hashed in parallel (default: half the CPU cores)",
    )
    return result


def next_steps(out: Path, creators: Sequence[str]) -> str:
    names = (
        " ".join(f"--creator {shlex.quote(name)}" for name in creators)
        or '--creator "NAME"'
    )
    command = f"import_fingerprints {names} ~/{REMOTE_DIR}/"
    return "\n".join(
        (
            "Next steps:",
            f"  rsync -av {shlex.quote(str(out))}/ {REMOTE_HOST}:{REMOTE_DIR}/",
            f"  ssh {REMOTE_HOST}",
            f"  cd ~/marceline/backend && uv run python manage.py {command} --dry-run",
            f"  cd ~/marceline/backend && uv run python manage.py {command}",
            "  Delete the JSON files on both machines after the import.",
        )
    )


def summarise(counts: dict[str, int], out: Path, creators: Sequence[str]) -> int:
    log(
        f"Summary: {counts['done']} done, {counts['skipped']} skipped, {counts['failed']} failed"
    )
    if counts["done"] or counts["skipped"]:
        log(next_steps(out, creators))
    return 1 if counts["failed"] else 0


def run_videos(cli: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    missing_tools = [
        tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None
    ]
    if missing_tools:
        log(
            f"{' and '.join(missing_tools)} not found on PATH. Install with: brew install ffmpeg"
        )
        return 1
    videos, missing = find_files(args.paths, VIDEO_EXTENSIONS)
    if args.title is not None and len(videos) != 1:
        cli.error(f"--title needs exactly one video, found {len(videos)}")
    for path in missing:
        log(f"failed {path}: no such file or directory")
    if not videos:
        log("No videos found.")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    log(
        f"Fingerprinting {len(videos)} video(s) into {args.out} with {args.jobs} job(s)"
    )
    counts = {"done": 0, "skipped": 0, "failed": len(missing)}
    for outcome in run_all(videos, args, args.title):
        counts[outcome.status] += 1
        log(f"{outcome.status} {outcome.path.name}: {outcome.detail}")
    return summarise(counts, args.out, tuple(args.creators or ()))


def run_photos(cli: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    collections, problems = find_collections(args.paths)
    if args.title is not None and len(collections) != 1:
        cli.error(
            f"--title needs exactly one photo collection, found {len(collections)}"
        )
    if args.title is not None:
        first = collections[0]
        collections = [Collection(args.title, first.folder, first.images)]
    for path in problems:
        log(f"failed {path}: no images there ({' '.join(sorted(IMAGE_EXTENSIONS))})")
    if not collections:
        log("No photo collections found.")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    total = sum(len(item.images) for item in collections)
    log(
        f"Fingerprinting {len(collections)} photo collection(s), {total} images, "
        f"into {args.out} with {args.jobs} job(s)"
    )
    counts = {"done": 0, "skipped": 0, "failed": len(problems)}
    for outcome in run_all_collections(collections, args):
        counts[outcome.status] += 1
        log(f"{outcome.status} {outcome.path.name}: {outcome.detail}")
    return summarise(counts, args.out, tuple(args.creators or ()))


def fetch_model() -> int:
    """Download the pinned ONNX encoder into the local cache and verify it (ADR 0037)."""
    import urllib.request

    target = embedding.model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        try:
            embedding.verify_model(target)
        except embedding.EmbeddingModelInvalid as exc:
            log(f"{exc}\nDelete it and run --fetch-model again.")
            return 1
        log(f"{target} is the pinned model.")
        return 0
    log(f"Downloading {embedding.MODEL_URL}\n  -> {target}")
    staged = target.with_suffix(".part")
    try:
        with urllib.request.urlopen(embedding.MODEL_URL, timeout=600) as response:
            staged.write_bytes(response.read())
        embedding.verify_model(staged)
    except (
        OSError,
        embedding.EmbeddingModelInvalid,
        embedding.EmbeddingModelMissing,
    ) as exc:
        staged.unlink(missing_ok=True)
        log(f"failed: {exc}")
        return 1
    staged.replace(target)
    log(
        f"{target}: sha256 {embedding.MODEL_SHA256} — pipeline {embedding.EMBEDDING_PIPELINE}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.fetch_model:
        return fetch_model()
    if not args.paths:
        cli.error("give at least one PATH (or --fetch-model on its own)")
    if args.jobs < 1:
        cli.error("--jobs must be at least 1")
    if not (args.fps > 0 and args.fps != float("inf")):
        cli.error("--fps must be finite and positive")
    if args.crop_grid and args.photos:
        cli.error(
            "--crop-grid is for videos: the lattice answers a re-framed video moment"
        )
    if args.crop_grid_dedup is not None:
        if not args.crop_grid:
            cli.error("--crop-grid-dedup needs --crop-grid")
        if not 0 < args.crop_grid_dedup <= 256:
            cli.error("--crop-grid-dedup must be 1..256 bits")
    if args.embeddings:
        # Fail before minutes of decoding, and say exactly what to do about it.
        try:
            embedding.verify_model(embedding.model_path())
        except (
            embedding.EmbeddingModelMissing,
            embedding.EmbeddingModelInvalid,
        ) as exc:
            log(str(exc))
            return 1
    return run_photos(cli, args) if args.photos else run_videos(cli, args)


if __name__ == "__main__":
    sys.exit(main())
