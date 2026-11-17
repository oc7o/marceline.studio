"""The upload spool: the only place a customer's media ever exists (issue #59, ADR 0044).

One directory per job, named after the job's public id (`upl_…`, a random UUIDv4 — never anything
the customer sent), mode 0700, holding exactly one file, mode 0600. It lives in its own Docker
volume, mounted by `web` and `fingerprint-worker` and by nothing else, and `infra/backup/*` pushes
one Postgres dump and one curated `~/.claude` tarball — so the spool is outside every backup by
construction rather than by an exclusion rule somebody has to remember.

The root itself is also 0700, and `problems()` fixes it when it is not: a container's volume is
often mounted already at the mount point's default 0755, before this module ever creates anything
under it, and `mkdir(exist_ok=True)` does not change an existing directory's mode (trial finding
2). Fixed when we own the directory; reported as a problem, never silently left, when we do not.

What this module guarantees, and `marceline/tests/test_no_retention.py` proves:

- `delete()` removes the whole job directory and returns True **only** after a follow-up check says
  it is gone; `MediaUpload.bytes_deleted_at` is written from that return value and nowhere else.
- `reap()` removes every job directory older than the hard TTL, whatever the database believes — a
  crashed worker, a lost row, a container killed mid-job. It is the backstop that makes the
  retention statement true rather than conditional on the happy path.
- Nothing here copies, moves, renames, opens for reading outside hashing, or logs the path.

Chunks are written at their own offset into one file, so a retry of the last chunk overwrites it,
a resume needs no state beyond the file's size, and a 10 GB upload never costs 20 GB of disk.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core import public_ids
from core.checkout_paths import inside_checkout

#: The one file inside a job's directory. A fixed name: nothing the customer sent is ever used to
#: build a path.
FILE_NAME = "upload"
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
#: How much of the file is read at a time when the digest is computed.
DIGEST_CHUNK = 4 * 1024 * 1024


class SpoolError(Exception):
    """A refusal with a machine-readable code (`unavailable`, `unknown_upload`, `out_of_order`,
    `too_large`, `spool_full`)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Assembled:
    size_bytes: int
    sha256: str


def repository_root() -> Path:
    return Path(settings.BASE_DIR).resolve().parent


def inside_the_checkout(resolved: Path) -> bool:
    """Whether this path lies in the source checkout, as the container sees it too.

    Delegates to `core.checkout_paths.inside_checkout`, the rule shared with the rights proof
    vault (`marceline.rights.vault`), which had the identical false positive in a container.
    """
    return inside_checkout(
        resolved, code_dir=Path(settings.BASE_DIR).resolve(), checkout_root=repository_root()
    )


def _mode_problem(directory: Path) -> str | None:
    """Fix the spool root's mode if it is wrong and we own it; report it if we do not.

    A container's volume is often mounted already, as the mount point's default 0755, before this
    module ever writes to it — `mkdir(parents=True, exist_ok=True, mode=...)` does not change an
    existing directory's mode, so ADR 0044 §3's 0700 promise silently did not hold for the root
    (per-job directories and files were still 0700/0600, which is why the trial did not notice
    until it was checked directly). Fixed when we can; reported, never ignored, when we cannot.
    """
    if not directory.is_dir():
        return None
    current = stat.S_IMODE(directory.stat().st_mode)
    if current == DIRECTORY_MODE:
        return None
    try:
        directory.chmod(DIRECTORY_MODE)
    except PermissionError:
        return (
            f"CLOUD_UPLOAD_SPOOL_DIR is mode {oct(current)}, not {oct(DIRECTORY_MODE)}, and "
            "cannot be fixed (not its owner)"
        )
    return None


def problems() -> list[str]:
    """Why the spool cannot be used, or an empty list. Same shape as the rights vault's."""
    found: list[str] = []
    raw = settings.CLOUD_UPLOAD_SPOOL_DIR
    if not raw:
        found.append("CLOUD_UPLOAD_SPOOL_DIR is not set")
        return found
    directory = Path(raw)
    if not directory.is_absolute():
        found.append("CLOUD_UPLOAD_SPOOL_DIR must be an absolute path")
        return found
    resolved = directory.resolve()
    if inside_the_checkout(resolved):
        found.append("CLOUD_UPLOAD_SPOOL_DIR must be outside the repository")
        return found
    mode_problem = _mode_problem(resolved)
    if mode_problem is not None:
        found.append(mode_problem)
    return found


def available() -> bool:
    return not problems()


def root() -> Path:
    if problems():
        raise SpoolError("unavailable", "the upload spool is not configured")
    directory = Path(settings.CLOUD_UPLOAD_SPOOL_DIR)
    directory.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    return directory


def job_directory(public_id: str) -> Path:
    """The job's own directory. Refuses anything that is not one of our generated ids, so no
    caller can turn a path into a traversal."""
    if not public_ids.is_valid("upl", public_id):
        raise SpoolError("unknown_upload", "not an upload id")
    return root() / public_id


def path_of(public_id: str) -> Path:
    return job_directory(public_id) / FILE_NAME


def create(public_id: str) -> None:
    """Make the job's directory and its empty file."""
    directory = job_directory(public_id)
    directory.mkdir(mode=DIRECTORY_MODE, exist_ok=True)
    path = directory / FILE_NAME
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        os.close(descriptor)


def received_bytes(public_id: str) -> int:
    path = path_of(public_id)
    return path.stat().st_size if path.exists() else 0


def append(public_id: str, index: int, data: bytes, *, chunk_bytes: int, max_bytes: int) -> int:
    """Write chunk `index` at its own offset; the new size.

    Only the next chunk is accepted — the customer resumes from `received_bytes // chunk_bytes` —
    or the one just written again, which overwrites it byte for byte and is what a client does
    when it did not see the answer. Anything else is `out_of_order`, because a gap in the middle
    of a file would be silently hashed as zeros.
    """
    path = path_of(public_id)
    if not path.exists():
        raise SpoolError("unknown_upload", "no spool file for this upload")
    if index < 0 or len(data) > chunk_bytes:
        raise SpoolError("out_of_order", "chunk index or size out of range")
    size = path.stat().st_size
    complete = size // chunk_bytes
    if size % chunk_bytes:
        # A short chunk was written: it can only be the last one, and only it may be re-sent.
        allowed = {complete}
    else:
        allowed = {complete} | ({complete - 1} if complete else set())
    if index not in allowed:
        raise SpoolError("out_of_order", "send the chunk after the last one received")
    offset = index * chunk_bytes
    if offset + len(data) > max_bytes:
        raise SpoolError("too_large", "the upload is larger than it declared")
    with path.open("r+b") as handle:
        handle.seek(offset)
        handle.write(data)
        handle.truncate()
    return offset + len(data)


def assemble(public_id: str) -> Assembled:
    """The finished file's size and digest, read from what is actually on disk."""
    path = path_of(public_id)
    if not path.exists():
        raise SpoolError("unknown_upload", "no spool file for this upload")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(DIGEST_CHUNK):
            digest.update(block)
            size += len(block)
    return Assembled(size_bytes=size, sha256=digest.hexdigest())


def delete(public_id: str) -> bool:
    """Remove the job's directory. True only once a follow-up check says it is really gone.

    `MediaUpload.bytes_deleted_at` is written from this return value and from nothing else, so a
    row that claims the bytes are gone is a row where this returned True.
    """
    try:
        directory = job_directory(public_id)
    except SpoolError:
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()


def total_bytes() -> int:
    """How much the whole spool holds right now, for the global cap."""
    if problems():
        return 0
    total = 0
    for entry in root().iterdir():
        file = entry / FILE_NAME
        if file.is_file():
            total += file.stat().st_size
    return total


def stale(now: datetime | None = None, ttl_hours: float | None = None) -> list[str]:
    """Job ids whose directory is older than the hard TTL. Reads only modification times."""
    if problems():
        return []
    moment = now or timezone.now()
    hours = settings.CLOUD_UPLOAD_TTL_HOURS if ttl_hours is None else ttl_hours
    cutoff = moment - timedelta(hours=hours)
    found: list[str] = []
    for entry in sorted(root().iterdir()):
        if not entry.is_dir() or not public_ids.is_valid("upl", entry.name):
            continue
        changed = datetime.fromtimestamp(entry.stat().st_mtime, tz=moment.tzinfo)
        if changed < cutoff:
            found.append(entry.name)
    return found


def reap(now: datetime | None = None, ttl_hours: float | None = None) -> list[str]:
    """Delete every job directory past the TTL and return the ids. The backstop: it does not ask
    the database, so a crashed worker, a lost row or a container killed mid-job leaves nothing."""
    return [job for job in stale(now, ttl_hours) if delete(job)]
