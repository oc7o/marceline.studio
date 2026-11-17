"""Assign originals to an organisation, move them, or unassign them — deliberately (ADR 0031).

The only command-line way an original changes owner after it was registered. It names every row it
would touch, including the other references of the same file (a v0/v1 re-fingerprint moves with
its twin, so one video never belongs to two organisations), and writes nothing with `--dry-run`:

    manage.py assign_originals --organisation "Test Customer" --reference 14 --dry-run
    manage.py assign_originals --organisation "Test Customer" --creator "Some Creator"
    manage.py assign_originals --unassign --reference 14

`--creator` selects every original the creator performs in (primary or co-performer). Each move is
logged (`marceline.ownership`).

**The case file does not follow the original** (ADR 0045, issue #85): it would show the new owner
the previous owner's stamps, so the move deletes it and `sync_cases` rebuilds one for the new
owner. Everything else the previous owner holds about that file — notice drafts, rights record,
public id, refused claims — is marked for removal and deleted `CUSTOMER_DATA_RETENTION_DAYS` later
by `customer_data_retention`, so there is time to offer them an export first
(`manage.py export_customer_data`). The dry run prints both before anything happens.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q

from marceline.app.ownership import (
    Via,
    describe,
    losses,
    organisation_named,
    reassign,
    with_twins,
)
from marceline.deletion import handover
from marceline.deletion.handover import HandoverEffect
from matching.models import Reference
from matching.references import resolve_creator


class Command(BaseCommand):
    help = "Deliberately assign originals to an organisation, or unassign them (logged)."

    def add_arguments(self, parser: CommandParser) -> None:
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--organisation", metavar="NAME", help="The new owner.")
        target.add_argument(
            "--unassign",
            action="store_true",
            help="Remove the owner: the originals become invisible to every customer.",
        )
        parser.add_argument(
            "--reference", dest="references", type=int, action="append", metavar="ID"
        )
        parser.add_argument("--creator", dest="creators", action="append", metavar="NAME")
        parser.add_argument("--dry-run", action="store_true", help="Show the moves; write nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        ids = options["references"] or []
        names = options["creators"] or []
        if not ids and not names:
            raise CommandError("name the originals: --reference ID and/or --creator NAME")
        try:
            organisation = (
                None if options["unassign"] else organisation_named(options["organisation"])
            )
            selected = set(ids)
            missing = selected - set(
                Reference.objects.filter(pk__in=ids).values_list("pk", flat=True)
            )
            if missing:
                raise ValueError(f"no original with id {', '.join(map(str, sorted(missing)))}")
            for name in names:
                creator = resolve_creator(name)
                performs = Reference.objects.filter(Q(creator=creator) | Q(creators=creator))
                selected.update(performs.values_list("pk", flat=True))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        references = with_twins(selected)
        if not references:
            raise CommandError("no originals match")
        dry_run = options["dry_run"]
        # The preview writes nothing and is what the handover report is built from: after a real
        # `reassign` the case rows are already gone and the marks already set, so asking then
        # would report zeroes (ADR 0045).
        preview = reassign(references, organisation, via=Via.COMMAND, dry_run=True)
        effects = handover.plan(losses(references, preview))
        changes = (
            preview
            if dry_run
            else reassign(references, organisation, via=Via.COMMAND, dry_run=False)
        )
        moved = 0
        for reference, change in zip(references, changes, strict=True):
            twin = "" if reference.pk in selected else " (same file as a named original)"
            if not change.changed:
                self.stdout.write(
                    f"original {reference.pk} {reference.title!r}: already {describe(change.after)}"
                    f"{twin}"
                )
                continue
            moved += 1
            verb = "would move" if dry_run else "moved"
            self.stdout.write(
                f"original {reference.pk} {reference.title!r}: {verb} from "
                f"{describe(change.before)} to {describe(change.after)}{twin}"
            )
        summary = "would change" if dry_run else "changed"
        self.stdout.write(f"{summary} {moved} of {len(references)} original(s)")
        self._print_handover(effects, dry_run=dry_run)
        if dry_run:
            self.stdout.write("dry run: nothing written")

    def _print_handover(self, effects: list[HandoverEffect], *, dry_run: bool) -> None:
        """What the previous owners lose (ADR 0045). Printed by a dry run too — it is the point."""
        for effect in effects:
            verb = "would reset" if dry_run else "reset"
            mark = (
                "already pending"
                if effect.already_marked
                else ("would mark" if dry_run else "marked")
            )
            self.stdout.write(
                f"organisation {effect.organisation_id} loses file {effect.sha256[:12]}…: "
                f"{verb} {effect.cases} case file(s) with {effect.findings} finding(s); "
                f"{mark} for removal ({effect.reason}) — deleted after "
                "CUSTOMER_DATA_RETENTION_DAYS"
            )
        if effects and dry_run:
            self.stdout.write(
                "offer the previous owner an export before applying: "
                "manage.py export_customer_data --organisation NAME --out PATH"
            )
