"""Production: report stale customer writes and delete settled ones (ADR 0028 amendment "The
production write path").

Cron every 15 minutes next to `pull_accounts`. Writes pending longer than
`OUTBOX_STALE_ALERT_HOURS` are logged at ERROR once each (Sentry reports them), a paused outbox
(development has not pulled for `OUTBOX_PAUSE_AFTER_HOURS`) is logged at ERROR on every run, and
items settled more than seven days ago are deleted. Idempotent.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from delivery import outbox


class Command(BaseCommand):
    help = "Report stale customer writes and delete settled ones (production only)."

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.MARCELINE_ROLE != "prod":
            raise CommandError("outbox_housekeeping runs on production (MARCELINE_ROLE=prod) only")
        result = outbox.housekeeping(now=timezone.now())
        self.stdout.write(
            f"outbox: {result.stale_reported} newly stale, {result.deleted} settled item(s) "
            f"deleted{', paused' if result.paused else ''}"
        )
