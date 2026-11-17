"""The contained decoder (issue #59, ADR 0044): a spooled file in, hashes out, nothing else.

Run as `python -I -m matching.cloud_decoder` by `matching.cloud_hash.hash_file`, with a scrubbed
environment, hard resource limits and — in the compose service — no route to the internet. It
reads one JSON spec on stdin and writes JSON **lines** on stdout: progress while it hashes, then
one answer.

    {"progress": {"frames": 42, "total": 3600}} how far along it is — two integers, nothing else
    {"ok": true,  "fingerprint": {…}}          the `marceline-fingerprint` document
    {"ok": false, "code": "unsupported_codec"} a refusal, by code

The progress lines are the customer's fingerprinting bar (ADR 0044 amendment 2026-09-18). They
travel on the pipe this process already has, because that is the whole containment: the child has
the file and no database credentials, the parent has the credentials and never opens the file. A
line carries **two integers** and `cloud_hash.parse_progress` coerces them again on the other
side, so nothing read out of the customer's file can reach a row or a log by this route.

**It imports no Django**, on purpose: this process must be startable without database credentials,
and there must be nothing in it that could reach the database if the file it opens turns out to be
an exploit. It computes no new recipe either — `fingerprint_video` is the same function
`add_reference` and `client/fingerprint.py` call, so the result carries `PIPELINE` and is
bit-comparable with every other reference.

It writes **no file**, and cannot: `RLIMIT_FSIZE` is zero in the parent's `preexec_fn`. A frame
never reaches a disk, a log or a pipe other than this one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from matching import cloud_hash
from matching.fingerprint import fingerprint_video, to_json
from matching.pdq import MediaDecodeError


def probe(path: Path) -> cloud_hash.Probe:
    command = cloud_hash.probe_command(path)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=cloud_hash.PROBE_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise cloud_hash.CloudHashError("probe_timeout") from exc
    if result.returncode != 0:
        raise cloud_hash.CloudHashError("unreadable_file")
    return cloud_hash.parse_probe(result.stdout)


def report(frames: int, total: int | None) -> None:
    """Write one progress line and flush it, so the parent sees it while the decode runs."""
    sys.stdout.write(cloud_hash.progress_line(frames, total) + "\n")
    sys.stdout.flush()


def run(spec: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(spec["path"]))
    measured = probe(path)
    refused = cloud_hash.refusal(measured)
    if refused is not None:
        return {"ok": False, "code": refused}
    fps = float(spec["fps"])
    # Only now is there a denominator at all: it comes from the duration ffprobe just read. Before
    # this line the parent has been told nothing, which is what a null total means on the status.
    total = cloud_hash.expected_positions(measured.duration_s, fps)
    report(0, total)
    # `file_stat` taken before the digest was computed is what `fingerprint_video` wants in order
    # to notice a file that changed underneath it without reading every byte a second time. The
    # spool file is complete and nothing writes to it after this point.
    stat = path.stat()
    fingerprint = fingerprint_video(
        path,
        fps=fps,
        title=str(spec["title"]) or None,
        creators=[str(name) for name in spec.get("creators") or []],
        sha256=str(spec["sha256"]),
        file_stat=stat,
        timeout_s=float(spec["timeout_s"]),
        progress=lambda frames: report(frames, total),
    )
    return {"ok": True, "fingerprint": to_json(fingerprint)}


def main() -> int:
    try:
        spec = json.loads(sys.stdin.read())
    except ValueError:
        sys.stdout.write(json.dumps({"ok": False, "code": "decode_failed"}) + "\n")
        return 0
    try:
        answer = run(spec)
    except cloud_hash.CloudHashError as error:
        answer = {"ok": False, "code": error.code}
    except MediaDecodeError:
        answer = {"ok": False, "code": "decode_failed"}
    except (ValueError, OSError, MemoryError):
        # Never the exception's text: it can quote the file's own bytes or its path.
        answer = {"ok": False, "code": "decode_failed"}
    sys.stdout.write(json.dumps(answer) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
