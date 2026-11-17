"""Fingerprint uploaded originals, one at a time, and delete the bytes (issue #59, ADR 0044).

The loop is deliberately dull: reap whatever the TTL says is too old, take one queued job, run it,
repeat. **Concurrency is one** — 13 CPU-minutes per video-hour measured on the development VPS,
whose other cores crawl — and it is enforced by a per-process advisory lock, like every other
single-instance process here, so a second container exits instead of fighting for the box.

The reap runs first on every poll and not only at the start: a job whose worker was killed
mid-decode is the case the retention statement has to survive, and the only thing that notices is
this line. The scheduler runs the same reap (`upload_spool`) so a stopped worker is not the only
thing standing between a crash and a file that stays.

Refuses to run while `CLOUD_FINGERPRINTING_ENABLED` is off, rather than idling: an operator who
started this container meant to switch the feature on.
"""

import logging
import time
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.locks import UPLOAD_WORKER_LOCKS, advisory_unlock, try_advisory_lock
from marceline.models import MediaUpload
from marceline.uploads import service, spool

logger = logging.getLogger("marceline.uploads")

#: Seconds between polls when there was nothing to do.
IDLE_SLEEP_S = 5.0


class Command(BaseCommand):
    help = "Fingerprint uploaded originals and delete the bytes (ADR 0044)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--once", action="store_true", help="Process what is queued and stop.")
        parser.add_argument("--limit", type=int, default=0, help="At most N jobs (implies --once).")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.CLOUD_FINGERPRINTING_ENABLED:
            raise CommandError("CLOUD_FINGERPRINTING_ENABLED is off; nothing would be processed")
        problems = spool.problems()
        if problems:
            raise CommandError("; ".join(problems))
        limit = int(options["limit"] or 0)
        once = bool(options["once"]) or limit > 0
        done = 0
        if not try_advisory_lock(UPLOAD_WORKER_LOCKS, 0):
            raise CommandError("another fingerprint worker holds the lock")
        try:
            while True:
                reaped = spool.reap()
                if reaped:
                    self._expire(reaped)
                job = service.claim()
                if job is None:
                    if once:
                        break
                    time.sleep(IDLE_SLEEP_S)
                    continue
                code = service.run_job(job)
                done += 1
                self.stdout.write(f"{job.public_id}: {code}")
                if limit and done >= limit:
                    break
        finally:
            advisory_unlock(UPLOAD_WORKER_LOCKS, 0)
        self.stdout.write(f"{done} upload(s) processed")

    def _expire(self, reaped: list[str]) -> None:
        """Mark the rows of reaped jobs, if they still exist. The bytes are already gone: this only
        records that they were taken by the backstop rather than by the happy path."""
        from django.utils import timezone

        MediaUpload.objects.filter(
            public_id__in=reaped, status__in=MediaUpload.OPEN_STATUSES
        ).update(
            status=MediaUpload.Status.EXPIRED,
            result_code="expired",
            bytes_deleted_at=timezone.now(),
        )
        logger.warning("reaped %s upload(s) past the spool TTL", len(reaped))
