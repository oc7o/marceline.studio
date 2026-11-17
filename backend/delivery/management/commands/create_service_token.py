"""Create a scoped service token for the development side of the two-machine API (ADR 0028).

Run on development. Prints the token once; only its SHA-256 digest is stored. Revoke in the admin
(*Delivery → Service tokens*, set `revoked_at`) and create a new one to rotate.

Two scopes, and a token has exactly one:

- `export` (the default) — production's read-only bundle and accounts fetch; goes into production's
  `CACHE_EXPORT_TOKEN`.
- `upload_relay` — the streaming relay for cloud fingerprinting (ADR 0044 §9); goes into
  production's `CLOUD_RELAY_TOKEN`. It may append to, complete, cancel and read an upload it
  created, and nothing else. **Never reuse the export token for it**: the whole point of a second
  scope is that neither credential can do the other's work.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from delivery.models import ServiceToken
from delivery.tokens import create_token


class Command(BaseCommand):
    help = "Create a scoped service token and print it once."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--name", required=True, help="A label, e.g. prod-2026-09.")
        parser.add_argument(
            "--scope",
            choices=[scope.value for scope in ServiceToken.Scope],
            default=ServiceToken.Scope.EXPORT,
            help="What the token may do. Default: export.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        name = options["name"]
        if ServiceToken.objects.filter(name=name).exists():
            raise CommandError(f"a token named {name!r} exists; choose another name")
        _, token = create_token(name, options["scope"])
        self.stderr.write(f"token {name!r} created; it is shown only this once")
        self.stdout.write(token)
