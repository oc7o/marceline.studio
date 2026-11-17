import sys
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from crawler.backup_alerts import configured, recipients, send_backup_alert


class Command(BaseCommand):
    help = (
        "Mail the operator that a backup run failed or is overdue (issue #39). Called by "
        "infra/backup/backup-run.sh and backup-check.sh; off unless BACKUP_ALERT_TO/FROM (or "
        "SAFETY_ALERT_TO/FROM) are set. See crawler/backup_alerts.py."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--subject", required=True, help="One line, no page content.")
        parser.add_argument("--body", help="Alert body as text.")
        parser.add_argument("--body-file", help="Read the body from this file, or '-' for stdin.")
        parser.add_argument(
            "--status",
            action="store_true",
            help="Only report whether the alert is configured; send nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["status"]:
            if configured():
                self.stdout.write(f"backup alert configured for {len(recipients())} recipient(s)")
            else:
                self.stdout.write("backup alert not configured (BACKUP_ALERT_TO/FROM unset)")
            return

        body = self._body(options)
        if not configured():
            # Not an error: staying unconfigured is a valid (default) choice, and the calling
            # script still prints and returns the failure itself.
            self.stdout.write("backup alert not configured (BACKUP_ALERT_TO/FROM unset); not sent")
            return
        if not send_backup_alert(str(options["subject"]), body):
            raise CommandError("backup alert could not be sent; see the log")
        self.stdout.write(f"backup alert sent to {len(recipients())} recipient(s)")

    def _body(self, options: dict[str, Any]) -> str:
        if options["body"] and options["body_file"]:
            raise CommandError("use either --body or --body-file, not both")
        if options["body"]:
            return str(options["body"])
        source = options["body_file"]
        if not source:
            raise CommandError("one of --body or --body-file is required")
        if source == "-":
            return sys.stdin.read()
        path = Path(source)
        if not path.is_file():
            raise CommandError(f"no such body file: {path}")
        return path.read_text()
