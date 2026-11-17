"""Issue the invoices subscriptions owe: one per started, not yet invoiced period. Idempotent.

Not scheduled (ADR 0032): the operator runs it, `--dry-run` first, and sends the PDFs by hand.
"""

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from billing import services


class Command(BaseCommand):
    help = "Issue due subscription invoices (idempotent; --dry-run first)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="List due periods only.")
        parser.add_argument(
            "--date",
            type=date.fromisoformat,
            default=None,
            help="Bill as of this day (YYYY-MM-DD; default: today in Germany).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        on: date = options["date"] or services.today()
        try:
            run = services.issue_due_invoices(on=on, dry_run=options["dry_run"])
        except services.BillingError as error:
            raise CommandError(str(error)) from error
        for subscription_id, period in run.due:
            self.stdout.write(
                f"due: subscription {subscription_id} {period.start.isoformat()}"
                f"..{period.end.isoformat()}"
            )
        for number in run.issued:
            self.stdout.write(f"issued: {number}")
        for subscription_id, reason in run.failed:
            self.stderr.write(f"failed: subscription {subscription_id}: {reason}")
        verb = "would issue" if options["dry_run"] else "issued"
        count = len(run.due) if options["dry_run"] else len(run.issued)
        self.stdout.write(f"{verb} {count} invoice(s), {len(run.failed)} failed, as of {on}")
        if run.failed:
            raise CommandError(f"{len(run.failed)} subscription(s) could not be invoiced")
