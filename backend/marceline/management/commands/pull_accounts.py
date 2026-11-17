"""Production: fetch development's signed accounts document and apply it (ADR 0028).

Run every 15 minutes from cron on the production host (runbook). Exits non-zero, with an error
code and no URL, when the fetch or the verification fails; the accounts already there stay.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from delivery.client import DeliveryError
from delivery.envelope import EnvelopeError
from marceline.accounts_replica import pull_accounts


class Command(BaseCommand):
    help = "Pull the signed accounts document from development and apply it (production only)."

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.MARCELINE_ROLE != "prod":
            raise CommandError("pull_accounts runs on production (MARCELINE_ROLE=prod) only")
        try:
            payload = pull_accounts(now=timezone.now())
        except (DeliveryError, EnvelopeError) as exc:
            raise CommandError(f"accounts not applied: {exc.code}") from exc
        self.stdout.write(
            f"accounts applied: {len(payload.organisations)} organisation(s), "
            f"{len(payload.users)} user(s), as of {payload.as_of:%Y-%m-%d %H:%M:%S}"
        )
