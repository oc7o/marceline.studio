"""Mark one original of one organisation for removal (ADR 0045, issue #85).

    manage.py remove_original --organisation "Test Customer" --reference 14          # dry run
    manage.py remove_original --organisation "Test Customer" --reference 14 --apply
    manage.py remove_original --organisation "Test Customer" --sha256 abc… --lift --apply

The scope of a mark is **organisation x file** (sha256), the way ADR 0031 identifies an original,
so it outlives the ownership row: the usual case is an owner switch, which `assign_originals`
already marks by itself. Use this one when a customer removes a work, or when a rights claim on one
work is withdrawn or lost.

Marking writes a date and deletes nothing; `customer_data_retention` deletes it
`CUSTOMER_DATA_RETENTION_DAYS` later, and only while `CUSTOMER_DATA_DELETION_ENABLED`.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from marceline.app.ownership import organisation_named, owned_references
from marceline.deletion.models import OriginalRemoval
from marceline.deletion.services import due_at, lift_removal, remove_original
from matching.models import Reference


class Command(BaseCommand):
    help = (
        "Mark one original of one organisation for removal (ADR 0045). Defaults to a dry run; "
        "pass --apply to write the mark. Nothing is deleted now."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organisation", required=True, metavar="NAME")
        what = parser.add_mutually_exclusive_group(required=True)
        what.add_argument("--reference", type=int, metavar="ID", help="Any reference of the file.")
        what.add_argument("--sha256", metavar="HEX", help="The file itself.")
        parser.add_argument(
            "--reason",
            default=OriginalRemoval.Reason.CUSTOMER_REQUEST,
            choices=[value for value, _label in OriginalRemoval.Reason.choices],
        )
        parser.add_argument("--note", default="", help="Short staff note. No personal data.")
        parser.add_argument("--lift", action="store_true", help="Take back a pending mark.")
        parser.add_argument("--apply", action="store_true", help="Write the mark (or lift it).")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            organisation = organisation_named(options["organisation"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        sha256 = self._sha256(options)
        dry_run = not options["apply"]
        if options["lift"]:
            self._lift(organisation, sha256, dry_run=dry_run)
            return
        owned = owned_references(organisation).filter(sha256=sha256)
        self.stdout.write(
            f"organisation {organisation.pk}, file {sha256[:12]}…: "
            f"{owned.count()} owned original(s), "
            f"{Reference.objects.filter(sha256=sha256).count()} reference(s) of the file in total"
        )
        if not owned.exists():
            self.stdout.write(
                "this organisation owns no reference of the file any more — its notice drafts, "
                "rights record, public id and refused claims are still deleted, the references "
                "themselves are not (they are somebody else's originals now)"
            )
        removed_at = timezone.now()
        verb = "would mark" if dry_run else "marked"
        self.stdout.write(
            f"{verb} it for removal ({options['reason']}); deleted from "
            f"{due_at(removed_at).date().isoformat()}"
        )
        if dry_run:
            self.stdout.write("dry run: nothing written")
            return
        _row, created = remove_original(
            organisation,
            sha256,
            removed_at=removed_at,
            reason=options["reason"],
            note=options["note"] or "",
        )
        if not created:
            self.stdout.write("a removal was already pending; its own clock is kept")

    def _sha256(self, options: dict[str, Any]) -> str:
        if options["sha256"]:
            return str(options["sha256"]).strip().lower()
        reference = Reference.objects.filter(pk=options["reference"]).first()
        if reference is None:
            raise CommandError(f"no original with id {options['reference']}")
        return reference.sha256

    def _lift(self, organisation: Any, sha256: str, *, dry_run: bool) -> None:
        pending = OriginalRemoval.objects.filter(
            organisation=organisation, sha256=sha256, deleted_at__isnull=True
        ).exists()
        if not pending:
            self.stdout.write("no pending removal for this organisation and file")
            return
        if dry_run:
            self.stdout.write("would lift the pending removal\ndry run: nothing written")
            return
        lift_removal(organisation, sha256)
        self.stdout.write("lifted the pending removal")
