"""Hash a customer-uploaded original in a contained child process (issue #59, ADR 0044).

This module is the whole containment of the most hostile input the project takes: a video file an
account holder chose, handed to ffmpeg. It is deliberately **pure and Django-free**, so the process
that opens the file can be started without database credentials, and so every argument list it
builds can be asserted character by character in a test.

What it decides, outermost first:

- **The environment the decoder gets** (`child_environment`): `PATH` and a handful of locale
  variables and nothing else. No `POSTGRES_*`, no `DJANGO_SECRET_KEY`, no key path — the child has
  no reason to reach the database and must not be able to.
- **The resource limits** (`rlimits`): address space, CPU seconds, open files, and `RLIMIT_FSIZE`
  of **zero**, so the decoder cannot create a file at all. That is what makes "no frame ever leaves
  the worker" a property of the process rather than a promise about the code.
- **ffprobe before ffmpeg** (`probe_command`, `refusal`): container format, video codec, pixel
  count, duration and stream count against short allowlists. A refusal here costs one probe and
  never starts a decoder.
- **The two argument lists**, with `-protocol_whitelist file` on both. A crafted playlist — an
  `ffconcat`, an `.m3u8`, a `concat:` input listing an http URL or `/etc/passwd` — is how ffmpeg is
  turned into a fetcher or a file reader, and that whitelist is the only thing that stops it.
  **Never add a protocol to it**, and never put a customer-supplied string into an ffmpeg argument:
  the only variable part of either command is the spool path, which this side generates.
- **What may cross back** (`progress_line`, `parse_progress`, ADR 0044 amendment 2026-09-18): the
  child writes JSON lines on the one pipe it has — progress lines while it hashes, then its answer
  — and `parse_progress` reduces a progress line to **two integers**, coerced here. Nothing else
  from that pipe is ever kept, so no text built from the customer's file can reach a log or a
  database row, and the containment is unchanged: the child still holds no credential and the
  parent still never opens the file. The child's stderr is not even read (`DEVNULL`), because a
  decoder's error text can quote the bytes it choked on.

The hashing itself is not reimplemented here. `matching.cloud_decoder` calls
`matching.fingerprint.fingerprint_video` — the same function `add_reference` and
`client/fingerprint.py` call — so a cloud-fingerprinted original carries `PIPELINE` and is
bit-comparable with every other reference (`matching/tests/test_cloud_hash.py` proves it on a clip
generated at test time).
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Container formats a customer master actually is, as ffprobe names them (`format_name` is a
#: comma-separated list of the formats the demuxer answers to, and ffprobe prints it verbatim).
ALLOWED_FORMATS = frozenset(
    {
        "mov,mp4,m4a,3gp,3g2,mj2",
        "matroska,webm",
        "avi",
        "mpegts",
    }
)
#: Video codecs we decode. ProRes and DNxHD are on the list precisely because a browser cannot
#: open them — they are the reason cloud fingerprinting exists at all.
ALLOWED_CODECS = frozenset(
    {
        "h264",
        "hevc",
        "vp8",
        "vp9",
        "av1",
        "mpeg4",
        "mpeg2video",
        "prores",
        "dnxhd",
        "mjpeg",
    }
)
#: 7680 x 4320. A frame larger than this is not a master, it is an attempt to make a decoder
#: allocate.
MAX_PIXELS = 7680 * 4320
#: Four hours. `MAX_POSITIONS` in the customer endpoint already caps the sampled positions; this
#: caps the decoding work before a single frame is read.
MAX_DURATION_S = 4 * 60 * 60
#: A master has a video stream, one or two audio streams and perhaps a timecode or subtitle track.
#: Hundreds of streams is a decoder stress test, not a film.
MAX_STREAMS = 12

#: Wall clock for one ffprobe.
PROBE_TIMEOUT_S = 60.0
#: Wall clock inside the child for the whole ffmpeg decode (handed to `fingerprint_video`, which
#: arms its own watchdog on the ffmpeg process).
FFMPEG_TIMEOUT_S = 4.0 * 60 * 60
#: Wall clock the *parent* gives the whole child process, a little above the one above so that the
#: child's own, tidier error normally wins.
DECODE_TIMEOUT_S = 4.5 * 60 * 60
#: Address space for the child, inherited by ffmpeg. Below the container's memory limit on purpose:
#: a runaway file should fail this one job, not have the kernel kill the worker.
RLIMIT_ADDRESS_SPACE = 1536 * 1024 * 1024
#: CPU seconds. Measured: about 13 CPU-minutes per video-hour for 1080p H.264 on the development
#: VPS (2026-09-18), so this is generous even for a long 4K master.
RLIMIT_CPU_SECONDS = 5 * 60 * 60
#: **Zero.** The decoder may not create a file: no frame, no screenshot, no temporary copy, not
#: even a `.pyc` (which is why `PYTHONDONTWRITEBYTECODE` is in the child's environment).
RLIMIT_FILE_SIZE = 0
#: Enough for the interpreter, the spool file and the pipes to ffmpeg.
RLIMIT_OPEN_FILES = 64

#: The exact variables the child gets. Everything else — above all the database credentials the
#: worker holds — is dropped. `matching/tests/test_cloud_hash.py` asserts this key set.
CHILD_ENVIRONMENT_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE")
FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin"

#: How long the parent waits on the pipe before looking at the clock again. Small enough that a
#: timeout is noticed promptly, large enough that a quiet child costs nothing.
READ_POLL_S = 1.0
#: How much is read from the pipe at a time.
READ_BLOCK_BYTES = 64 * 1024

#: Two integers per progress callback: frames hashed so far, and the expected total or None while
#: nothing knows it. A `None` total is the honest answer, never an invented denominator.
OnProgress = Callable[[int, "int | None"], None]


class CloudHashError(Exception):
    """A refusal with a machine-readable code, never a message a customer would read."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Probe:
    """What ffprobe says about a file, reduced to the five facts that decide whether we decode."""

    format_name: str
    duration_s: float
    streams: int
    codec: str
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height


def child_environment(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the decoder runs with: a path, a locale, and nothing that opens anything.

    `PATH` is taken from the parent because the interpreter and ffmpeg have to be found; it is not
    a credential. `HOME` points at the container's tmpfs so nothing looks in a real home directory.
    """
    environment = dict(os.environ if parent is None else parent)
    return {
        "PATH": environment.get("PATH") or FALLBACK_PATH,
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def rlimits() -> list[tuple[int, tuple[int, int]]]:
    """(resource, (soft, hard)) pairs applied in the child before it executes anything."""
    return [
        (resource.RLIMIT_AS, (RLIMIT_ADDRESS_SPACE, RLIMIT_ADDRESS_SPACE)),
        (resource.RLIMIT_CPU, (RLIMIT_CPU_SECONDS, RLIMIT_CPU_SECONDS)),
        (resource.RLIMIT_FSIZE, (RLIMIT_FILE_SIZE, RLIMIT_FILE_SIZE)),
        (resource.RLIMIT_NOFILE, (RLIMIT_OPEN_FILES, RLIMIT_OPEN_FILES)),
    ]


def apply_rlimits() -> None:
    """`preexec_fn` for the child: the limits above, and its own process group so a timeout can
    kill ffmpeg with it."""
    for which, limits in rlimits():
        resource.setrlimit(which, limits)
    os.setsid()


def probe_command(path: Path) -> list[str]:
    """The one ffprobe we run. `-protocol_whitelist file` is not optional."""
    return [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-show_entries",
        "format=format_name,duration,nb_streams:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(path),
    ]


def parse_probe(payload: str) -> Probe:
    """ffprobe's JSON reduced to a `Probe`. Raises `CloudHashError` if there is no video stream."""
    data: Any
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise CloudHashError("unreadable_file", "ffprobe answered nothing usable") from exc
    container = data.get("format") or {}
    streams = [stream for stream in (data.get("streams") or []) if isinstance(stream, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise CloudHashError("no_video_stream", "the file has no video stream")
    try:
        duration = float(str(container.get("duration")))
    except (TypeError, ValueError) as exc:
        raise CloudHashError("unreadable_file", "the file declares no duration") from exc
    try:
        count = int(str(container.get("nb_streams")))
    except (TypeError, ValueError):
        count = len(streams)
    return Probe(
        format_name=str(container.get("format_name") or ""),
        duration_s=duration,
        streams=count,
        codec=str(video.get("codec_name") or ""),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
    )


def refusal(probe: Probe) -> str | None:
    """Why we will not decode this, or None. Codes only — the customer sees the code, never a
    sentence built from the file."""
    if probe.format_name not in ALLOWED_FORMATS:
        return "unsupported_container"
    if probe.codec not in ALLOWED_CODECS:
        return "unsupported_codec"
    if probe.streams > MAX_STREAMS:
        return "too_many_streams"
    if probe.width < 1 or probe.height < 1 or probe.pixels > MAX_PIXELS:
        return "frame_too_large"
    if not 0 < probe.duration_s <= MAX_DURATION_S:
        return "too_long"
    return None


def decoder_command(executable: str | None = None) -> list[str]:
    """How the child is started. No customer-supplied argument, ever: the spec travels on stdin.

    `-E` ignores the inherited `PYTHON*` variables and `-s` the user site directory, so nothing
    outside the image can inject a module into the decoder. Not `-I`, which would also stop `-m`
    from putting the working directory (the backend package root, set by `hash_file`) on the path
    and so make `matching` unimportable.
    """
    return [executable or sys.executable, "-E", "-s", "-m", "matching.cloud_decoder"]


def expected_positions(duration_s: float, fps: float) -> int | None:
    """How many moments a decode of this length will sample, or None if it cannot be said.

    An **estimate** from the duration ffprobe reported: the sample grid is one picture every
    `1/fps` seconds (`matching.frames.iter_video_frames`), so a file whose real length differs
    from its declared one ends a little above or below it. The screen therefore clamps its bar and
    never turns this into a time — ADR 0044 §7 bans a duration promise.
    """
    if not (duration_s > 0 and fps > 0):
        return None
    return max(1, int(duration_s * fps))


def progress_line(frames: int, total: int | None) -> str:
    """One line the decoder writes while it hashes. Two numbers; there is no third field."""
    return json.dumps(
        {"progress": {"frames": int(frames), "total": None if total is None else int(total)}}
    )


def parse_progress(payload: object) -> tuple[int, int | None] | None:
    """(frames, total) out of one decoded line, or None when it is not a progress line.

    Everything is coerced to an integer here, which is the point: this is the only thing the
    parent keeps from the child's pipe besides the fingerprint document itself, and a number
    cannot carry a file name, a path or a decoder's complaint about the customer's bytes.
    """
    if not isinstance(payload, dict):
        return None
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        return None
    try:
        frames = int(progress["frames"])
    except (KeyError, TypeError, ValueError):
        return None
    if frames < 0:
        return None
    total: int | None
    try:
        raw = progress.get("total")
        total = None if raw is None else int(raw)
    except (TypeError, ValueError):
        total = None
    if total is not None and total < 1:
        total = None
    return frames, total


def decoder_spec(
    path: Path, *, fps: float, title: str, creators: Sequence[str], sha256: str
) -> dict[str, Any]:
    """The JSON the child reads on stdin."""
    return {
        "path": str(path),
        "fps": fps,
        "title": title,
        "creators": list(creators),
        "sha256": sha256,
        "timeout_s": FFMPEG_TIMEOUT_S,
    }


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it started. `apply_rlimits` gave it its own session, so the
    process group is the only handle that also reaches ffmpeg."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):  # the kill above is SIGKILL
        process.wait(timeout=10)


def _read_answer(
    process: subprocess.Popen[bytes], *, deadline: float, on_progress: OnProgress | None
) -> dict[str, Any] | None:
    """Drain the child's JSON lines until it closes the pipe; its last non-progress line.

    Progress lines are handed to `on_progress` and dropped; nothing else from the stream is kept
    except the answer document. Raises `CloudHashError("decode_timeout")` at the deadline.
    """
    stream = process.stdout
    assert stream is not None
    descriptor = stream.fileno()
    buffer = bytearray()
    start = 0
    answer: dict[str, Any] | None = None

    def handle(raw: bytes) -> None:
        nonlocal answer
        if not raw.strip():
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            # A line we cannot read is dropped rather than reported: its text is the child's,
            # and the child is holding the customer's file.
            return
        step = parse_progress(payload)
        if step is not None:
            if on_progress is not None:
                on_progress(*step)
            return
        if isinstance(payload, dict):
            answer = payload

    while True:
        if time.monotonic() > deadline:
            raise CloudHashError("decode_timeout", "the decoder ran past its deadline")
        if not select.select([descriptor], [], [], READ_POLL_S)[0]:
            continue
        block = os.read(descriptor, READ_BLOCK_BYTES)
        if not block:
            break
        buffer += block
        while (end := buffer.find(b"\n", start)) >= 0:
            handle(bytes(buffer[start:end]))
            start = end + 1
        if start:
            # Only the tail of a line is carried over, so the progress stream costs no memory;
            # the answer arrives as one long line and is simply appended to.
            del buffer[:start]
            start = 0
    handle(bytes(buffer))
    return answer


def hash_file(
    path: Path,
    *,
    fps: float,
    title: str,
    creators: Sequence[str],
    sha256: str,
    timeout_s: float = DECODE_TIMEOUT_S,
    executable: str | None = None,
    environment: Mapping[str, str] | None = None,
    on_progress: OnProgress | None = None,
) -> dict[str, Any]:
    """Fingerprint one spooled file in a contained child process; the hashes-only document.

    `on_progress(frames, total)` is called while the child hashes, with the frames it has done and
    the expected total (or None while nothing knows it). It is the only thing that crosses the
    containment boundary besides the document, it carries nothing but integers, and the caller is
    expected to throttle what it does with them (`marceline.uploads.service.ProgressWriter`).

    Raises `CloudHashError` with one of the refusal codes above, `decode_timeout`, or
    `decode_failed` (the child crashed — its exit code, never its output, which could quote the
    file).
    """
    command = decoder_command(executable)
    spec = json.dumps(decoder_spec(path, fps=fps, title=title, creators=creators, sha256=sha256))
    deadline = time.monotonic() + timeout_s
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # Never read: a decoder's error text can quote the bytes it choked on, and this parent
        # writes to the database and to logs.
        stderr=subprocess.DEVNULL,
        env=dict(environment) if environment is not None else child_environment(),
        preexec_fn=apply_rlimits,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    try:
        assert process.stdin is not None
        try:
            process.stdin.write(spec.encode())
            process.stdin.close()
        except (BrokenPipeError, ValueError):
            # The child died before it read its spec; its exit code below says so.
            pass
        answer = _read_answer(process, deadline=deadline, on_progress=on_progress)
        returncode = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except CloudHashError:
        _stop(process)
        raise
    except subprocess.TimeoutExpired as exc:
        _stop(process)
        raise CloudHashError("decode_timeout", f"the decoder exceeded {timeout_s:g} s") from exc
    except BaseException:
        _stop(process)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    if returncode != 0:
        raise CloudHashError("decode_failed", f"the decoder exited {returncode}")
    if answer is None:
        raise CloudHashError("decode_failed", "the decoder answered nothing usable")
    if not answer.get("ok"):
        raise CloudHashError(str(answer.get("code") or "decode_failed"), "")
    document = answer.get("fingerprint")
    if not isinstance(document, dict):
        raise CloudHashError("decode_failed", "the decoder answered no document")
    return document
