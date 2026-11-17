"""Mark an organisation's pilot as ended, which starts the deletion clock (ADR 0045, issue #85).

    manage.py end_pilot --organisation "Test Customer" --ended-on 2026-09-30        # dry run
    manage.py end_pilot --organisation "Test Customer" --ended-on 2026-09-30 --apply
    manage.py end_pilot --organisation "Test Customer" --lift --apply
    manage.py end_pilot --list

Marking writes a date and deletes nothing: `customer_data_retention` does the deleting, after
`CUSTOMER_DATA_RETENTION_DAYS` and only while `CUSTOMER_DATA_DELETION_ENABLED`. Offer the export
first (`manage.py export_customer_data`).
"""

from datetime import datetime, time
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from marceline.app.ownership import organisation_named
from marceline.deletion.models import OrganisationEnding
from marceline.deletion.services import due_at, end_pilot, lift_ending


class Command(BaseCommand):
    help = (
        "Mark an organisation's pilot ended (ADR 0045). The scheduled job deletes its data "
        "CUSTOMER_DATA_RETENTION_DAYS later. Defaults to a dry run; pass --apply to write."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organisation", metavar="NAME", help="The customer.")
        parser.add_argument(
            "--ended-on", metavar="YYYY-MM-DD", help="The day the pilot ended (default: today)."
        )
        parser.add_argument("--reason", default="", help="Short staff note. No personal data.")
        parser.add_argument(
            "--lift", action="store_true", help="Take back a pending mark; deletes nothing."
        )
        parser.add_argument("--list", action="store_true", help="Show every pending mark.")
        parser.add_argument("--apply", action="store_true", help="Write the mark (or lift it).")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["list"]:
            self._list()
            return
        name = options["organisation"]
        if not name:
            raise CommandError("name the customer: --organisation NAME (or --list)")
        try:
            organisation = organisation_named(name)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        dry_run = not options["apply"]
        if options["lift"]:
            self._lift(organisation, dry_run=dry_run)
            return
        ended_at = self._ended_at(options["ended_on"])
        verb = "would mark" if dry_run else "marked"
        self.stdout.write(
            f"{verb} organisation {organisation.pk} ended on {ended_at.date().isoformat()}; "
            f"its data is deleted from {due_at(ended_at).date().isoformat()}"
        )
        if dry_run:
            self.stdout.write("dry run: nothing written")
            return
        _row, created = end_pilot(organisation, ended_at=ended_at, reason=options["reason"] or "")
        if not created:
            self.stdout.write("an ending was already recorded; its date is now the one above")
        self.stdout.write(
            "offer the export before the window is up: "
            f"manage.py export_customer_data --organisation {organisation.name!r}"
        )

    def _ended_at(self, value: str | None) -> datetime:
        if not value:
            return timezone.now()
        try:
            day = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CommandError(f"--ended-on takes YYYY-MM-DD, got {value!r}") from exc
        return timezone.make_aware(datetime.combine(day, time.min))

    def _lift(self, organisation: Any, *, dry_run: bool) -> None:
        pending = OrganisationEnding.objects.filter(
            organisation=organisation, deleted_at__isnull=True
        ).exists()
        if not pending:
            self.stdout.write(f"organisation {organisation.pk} has no pending ending to lift")
            return
        if dry_run:
            self.stdout.write(
                f"would lift the pending ending of organisation {organisation.pk}\n"
                "dry run: nothing written"
            )
            return
        lift_ending(organisation)
        self.stdout.write(f"lifted the pending ending of organisation {organisation.pk}")

    def _list(self) -> None:
        rows = OrganisationEnding.objects.select_related("organisation").order_by("ended_at")
        if not rows:
            self.stdout.write("no organisation is marked as ended")
            return
        for row in rows:
            state = (
                f"deleted {row.deleted_at.date().isoformat()}"
                if row.deleted_at
                else f"due {due_at(row.ended_at).date().isoformat()}"
            )
            exported = row.exported_at.date().isoformat() if row.exported_at else "never"
            self.stdout.write(
                f"organisation {row.organisation_id}: ended {row.ended_at.date().isoformat()}, "
                f"{state}, exported {exported}"
            )
