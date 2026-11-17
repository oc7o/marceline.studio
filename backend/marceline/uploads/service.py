"""Run one upload job: hash the spooled file, register the original, delete the bytes (ADR 0044).

The shape of this module is the retention guarantee. `run_job` is one `try` whose `finally` deletes
the job's directory — on success, on a refusal, on a decoder crash, on a timeout and on anything
nobody thought of — and `_settle` writes `bytes_deleted_at` only from `spool.delete`'s own verified
answer. There is no branch that returns without passing through that `finally`, and adding one
would be the bug `marceline/tests/test_no_retention.py` exists to catch.

Nothing here decides what an original is. Once the hashes exist the job hands them to
`marceline.app.fingerprints.register_fingerprint` — the function the browser upload runs — so
ownership, the rights declaration, the duplicate check, the generic 409 and the staff-only
`RefusedClaim` are not merely "the same rules" but the same code.

The decode does not happen in this process either: `matching.cloud_hash.hash_file` starts a child
with no database credentials and hard limits. This module holds the database connection; the child
holds the customer's file. They never hold both — and that is also why the progress the customer
sees is written *here*: the child reports two integers over the pipe it already has, and this
process, which is the one with the credentials, writes them to the job row through
`ProgressWriter`, throttled so a 45-minute original costs a handful of updates rather than 2,700.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from marceline.app import upload_declaration
from marceline.app.fingerprints import Refused, register_fingerprint
from marceline.app.uploads import Refused as UploadRefused
from marceline.models import MediaUpload
from marceline.uploads import spool
from matching.cloud_hash import CloudHashError, hash_file
from matching.fingerprint import FingerprintFormatError, from_json

logger = logging.getLogger("marceline.uploads")

#: What `Reference.source_note` records for a cloud-fingerprinted original, so it is
#: distinguishable from a browser one without a second registration path.
SOURCE_NOTE = "Fingerprinted by Marceline from a file the customer uploaded"

#: The shortest gap between two progress updates of one job. One write a second is already more
#: than a customer can read; the decoder reports every sampled frame, which at 1 fps over a
#: 45-minute original would otherwise be 2,700 updates.
PROGRESS_MIN_INTERVAL_S = 1.0


class ProgressWriter:
    """Write the child's frame counters to the job row, at most once a second and only on change.

    Deliberately a small object with an injected clock and an injected write, so the throttle can
    be tested without a decoder and without a database. It carries numbers only: `frames` and
    `total` are what `cloud_hash.parse_progress` coerced out of the pipe, and nothing else from
    the child is ever passed to it.
    """

    def __init__(
        self,
        job: MediaUpload,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = PROGRESS_MIN_INTERVAL_S,
        write: Callable[[int, int | None], None] | None = None,
    ) -> None:
        self._job = job
        self._clock = clock
        self._min_interval_s = min_interval_s
        self._write = write or self._store
        self._last_at: float | None = None
        self._last: tuple[int | None, int | None] | None = None
        self.writes = 0

    def _store(self, frames: int, total: int | None) -> None:
        MediaUpload.objects.filter(pk=self._job.pk).update(frames_done=frames, frames_total=total)
        self._job.frames_done = frames
        self._job.frames_total = total

    def _percent(self, frames: int, total: int | None) -> int | None:
        if total is None or total < 1:
            return None
        return min(100, int(frames * 100 / total))

    def __call__(self, frames: int, total: int | None) -> None:
        step = (self._percent(frames, total), total)
        if self._last == step and total is not None:
            # Nothing a customer could see has changed: same whole percent, same denominator.
            return
        now = self._clock()
        if self._last_at is not None and now - self._last_at < self._min_interval_s:
            return
        self._last_at = now
        self._last = step
        self.writes += 1
        self._write(frames, total)

    def flush(self, frames: int, total: int | None) -> None:
        """Write the final count unconditionally, throttle or no throttle.

        The 1 Hz throttle above exists to keep a long decode cheap, and its price is that the very
        last update — the one that would show a finished job at 100 % — can land inside the same
        second as the update before it and be dropped, so a job that hashed all 30 of its 30
        moments settles on a row that still reads 23. This is the one caller allowed to bypass
        `__call__`'s gate, and it is only ever the decode's own last word: `run_job` calls it once,
        after `hash_file` has returned successfully, with the fingerprint's own frame count for
        both numbers, so a `done` job's bar always reaches its total.
        """
        self._last_at = self._clock()
        self._last = (self._percent(frames, total), total)
        self.writes += 1
        self._write(frames, total)


def claim(now: datetime | None = None) -> MediaUpload | None:
    """Take one queued job, or None. `SKIP LOCKED`, like every other queue in this codebase."""
    moment = now or timezone.now()
    with transaction.atomic():
        job = (
            MediaUpload.objects.select_for_update(skip_locked=True)
            .filter(status=MediaUpload.Status.QUEUED)
            .order_by("created_at", "pk")
            .first()
        )
        if job is None:
            return None
        job.status = MediaUpload.Status.HASHING
        job.hashed_at = moment
        # A job is only ever claimed once, but a re-queued row must not show a stale bar.
        job.frames_done = 0
        job.frames_total = None
        job.save(update_fields=["status", "hashed_at", "frames_done", "frames_total"])
        return job


def _settle(job: MediaUpload, *, status: str, code: str, original_id: str = "") -> None:
    """Record the outcome and, separately, whether the bytes are really gone."""
    deleted = spool.delete(job.public_id)
    job.status = status
    job.result_code = code
    job.original_id = original_id
    job.bytes_deleted_at = timezone.now() if deleted else None
    job.save(
        update_fields=[
            "status",
            "result_code",
            "original_id",
            "bytes_deleted_at",
            "duration_s",
            "pipeline",
        ]
    )
    if not deleted:
        # The reaper will take it; this is the one line that says the happy path did not.
        logger.error("upload %s: the spool directory did not go away", job.public_id)


def run_job(job: MediaUpload) -> str:
    """Hash, register, delete. The resulting code, which is also `MediaUpload.result_code`."""
    status = MediaUpload.Status.FAILED
    code = "processing_failed"
    original_id = ""
    try:
        if job.created_by is None:
            # The account was deleted while the file was in the spool. There is nobody to record
            # as having declared the rights, so the job is refused and the bytes still go.
            raise Refused(403, "no_organisation")
        if job.upload_declaration_version != upload_declaration.CURRENT_VERSION:
            # The declaration was replaced while this file was waiting (ADR 0044 amendment
            # 2026-09-19) — the same rule the rights declaration gets at registration. Consent to
            # a wording nobody shows any more is not consent to this one, so the job is refused and
            # the bytes go in the `finally` below like every other exit.
            raise UploadRefused(400, "upload_declaration_required")
        progress = ProgressWriter(job)
        document = hash_file(
            spool.path_of(job.public_id),
            fps=job.frame_fps,
            title=job.title,
            creators=list(job.creators),
            sha256=job.sha256,
            on_progress=progress,
        )
        fingerprint = from_json(document)
        # The throttle above can drop the decode's own last update (issue #59 trial finding 1):
        # write the real count now, so a done job's bar always reads frames_done == frames_total
        # rather than whatever percent happened to fall in the last throttled second.
        progress.flush(fingerprint.frame_count, fingerprint.frame_count)
        created, original = register_fingerprint(
            job.created_by,
            job.organisation,
            fingerprint,
            rights_text_version=job.rights_text_version,
            language=job.rights_language,
            source_note=SOURCE_NOTE,
        )
        job.duration_s = fingerprint.duration_s
        job.pipeline = fingerprint.pipeline
        status, code = MediaUpload.Status.DONE, "created" if created else "already_present"
        original_id = original.public_id
    except CloudHashError as error:
        status, code = MediaUpload.Status.REFUSED, error.code
    except (Refused, UploadRefused) as refused:
        status, code = MediaUpload.Status.REFUSED, refused.code
    except FingerprintFormatError:
        status, code = MediaUpload.Status.FAILED, "processing_failed"
        logger.exception("upload %s: the decoder's document did not validate", job.public_id)
    except Exception:
        logger.exception("upload %s: processing failed", job.public_id)
    finally:
        # Every path, including the ones nobody thought of. This is the retention guarantee.
        _settle(job, status=status, code=code, original_id=original_id)
    return code
