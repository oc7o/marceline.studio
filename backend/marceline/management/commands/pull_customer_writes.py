"""Development: pull customer writes from production's outbox and apply them (ADR 0028 amendment
"The production write path").

Cron every minute in the `export` container (it holds the signing key and reaches production's
Tailscale-only listener, `CACHE_PURGE_BASE_URL`). Single instance (advisory lock): a second run
while one is busy exits quietly. Each run pulls up to `--max-batches` batches, applies the items in
order, acknowledges every settled outcome, and deletes acknowledged rows older than seven days.

`--retry ITEM` applies a quarantined write again from its kept payload (after the cause is fixed);
the next run tells production. Exits non-zero, with an error code and no URL, when production cannot
be reached or does not verify.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from core.locks import advisory_unlock, try_advisory_lock
from delivery import inbox
from delivery.client import DeliveryError
from delivery.envelope import EnvelopeError
from delivery.models import InboxItem
from marceline import customer_writes
from marceline.export import signing_key


class Command(BaseCommand):
    help = "Pull customer writes from production, apply them in order, acknowledge (development)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-batches", type=int, default=10)
        parser.add_argument("--retry", metavar="ITEM", help="Apply a quarantined write again.")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.MARCELINE_ROLE != "dev":
            raise CommandError("pull_customer_writes runs on development (MARCELINE_ROLE=dev) only")
        if not try_advisory_lock(inbox.PULL_LOCK_SPACE, 0):
            self.stdout.write("another pull is running; nothing to do")
            return
        try:
            if options["retry"]:
                self._retry(options["retry"])
            self._pull(max_batches=max(1, options["max_batches"]))
        finally:
            advisory_unlock(inbox.PULL_LOCK_SPACE, 0)

    def _retry(self, item_id: str) -> None:
        try:
            row = inbox.retry(item_id, customer_writes.apply, now=timezone.now())
        except InboxItem.DoesNotExist as exc:
            raise CommandError(f"no quarantined write {item_id}") from exc
        self.stdout.write(f"retry of {item_id}: {row.status} {row.result_code or row.reason}")

    def _pull(self, *, max_batches: int) -> None:
        try:
            sign, verify = signing_key(), inbox.verification_key()
            totals = {"pulled": 0, "settled": 0, "failed": 0, "waiting": 0, "acknowledged": 0}
            for _ in range(max_batches):
                report = inbox.run_once(
                    customer_writes.apply, signing_key=sign, verify_key=verify, now=timezone.now
                )
                for name in totals:
                    totals[name] += getattr(report, name)
                if not report.more:
                    break
        except (DeliveryError, EnvelopeError) as exc:
            raise CommandError(f"customer writes not pulled: {exc.code}") from exc
        deleted = inbox.housekeeping(now=timezone.now())
        self.stdout.write(
            "customer writes: {pulled} pulled, {settled} settled, {failed} failed, "
            "{waiting} waiting, {acknowledged} acknowledged".format(**totals)
            + f", {deleted} old record(s) deleted"
        )
