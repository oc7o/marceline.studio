"""Ask the do-not-contact record before writing to anybody (issue #98, docs/do-not-contact.md)."""

import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.do_not_contact import KINDS, MissingSecret, UnusableIdentifier
from catalog.services import contact_check

KIND_NAMES = tuple(kind for kind, _ in KINDS)


class Command(BaseCommand):
    help = (
        "May we contact these identifiers? Prints allowed/blocked per input position and never "
        "echoes an identifier. Exits non-zero if any input is blocked or unusable, so it can "
        "guard a script. Identifiers come from the arguments, or from stdin, one per line."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "identifiers",
            nargs="*",
            help="The identifiers to check. With none, they are read from stdin, one per line.",
        )
        parser.add_argument(
            "--kind",
            default="email",
            choices=KIND_NAMES,
            help="What kind of identifier these are (default: email). One kind per run: the kind "
            "is part of the hash, so an address checked as the wrong kind reads as allowed.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        kind = options["kind"]
        identifiers = list(options["identifiers"])
        if not identifiers:
            identifiers = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
        if not identifiers:
            raise CommandError("no identifiers given (arguments or stdin, one per line)")

        # Normalisation can fail per input, so each is checked on its own: one unusable line must
        # not hide the answer for the rest.
        results: list[str] = []
        unusable = 0
        blocked = 0
        for position, identifier in enumerate(identifiers, start=1):
            try:
                is_blocked = contact_check(kind, [identifier])[0]
            except MissingSecret as exc:
                raise CommandError(str(exc)) from exc
            except UnusableIdentifier as exc:
                unusable += 1
                # `exc` says what is wrong with the shape ("no local part"), never the value.
                results.append(f"{position}: unusable ({exc})")
                continue
            blocked += is_blocked
            results.append(f"{position}: {'blocked' if is_blocked else 'allowed'}")

        for line in results:
            self.stdout.write(line)
        self.stdout.write(
            f"{len(identifiers)} checked as {kind}: {blocked} blocked, {unusable} unusable"
        )
        if blocked:
            raise CommandError(
                f"{blocked} of {len(identifiers)} identifiers said no: do not contact them "
                "(docs/do-not-contact.md)"
            )
        if unusable:
            raise CommandError(
                f"{unusable} of {len(identifiers)} identifiers could not be checked; an unchecked "
                "identifier is not an allowed one"
            )
