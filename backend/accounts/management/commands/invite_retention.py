from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from accounts.invite_retention import InviteRetentionReport, apply_retention


class Command(BaseCommand):
    help = (
        "Delete an unredeemed customer invite (accounts.CustomerInvite) --retain-days after it "
        "stopped being usable: the revocation timestamp for a revoked one, expires_at for one "
        "that simply ran out. A still-open invite is never a candidate, and neither is one whose "
        "redemption is sitting in the outbox (handed_over_at). A redeemed invite is never touched "
        "here. Deletion is irreversible, so this defaults to a dry run that writes nothing -- "
        "pass --apply after reading it. Idempotent and safe to run from cron."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", help="Report counts only (the default)."
        )
        mode.add_argument("--apply", action="store_true", help="Delete. Irreversible.")
        parser.add_argument(
            "--retain-days",
            type=int,
            default=settings.ONBOARDING_INVITE_RETENTION_DAYS,
            help="Days an unredeemed invite is kept after it stopped being usable "
            f"(default {settings.ONBOARDING_INVITE_RETENTION_DAYS}).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = apply_retention(dry_run=not options["apply"], retain_days=options["retain_days"])
        self._print(report)

    def _print(self, report: InviteRetentionReport) -> None:
        mode = " (dry run, nothing written)" if report.dry_run else ""
        self.stdout.write(f"invite retention {report.now.isoformat()}{mode}")
        self.stdout.write(f"unredeemed invites with a clock running: {report.clocked}")
        verb = "to delete" if report.dry_run else "deleted"
        self.stdout.write(
            f"{verb}: {report.due} invite(s) unusable before "
            f"{report.delete_before.date().isoformat()} ({report.retain_days} days)"
        )
        if report.in_flight:
            self.stdout.write(
                f"kept: {report.in_flight} invite(s) whose redemption is still in the outbox"
            )
