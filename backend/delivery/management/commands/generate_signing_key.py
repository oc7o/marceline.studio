"""Create development's Ed25519 signing key for the production cache (ADR 0028).

Writes the private key to `--private-key` (mode 0600, never overwrites) and prints **only the public
key** — the PEM production needs at `CACHE_VERIFY_KEY_PATH`. Keep the private key outside the
repository (e.g. `/srv/marceline/keys/`) and point `CACHE_SIGNING_KEY_PATH` at it on development.
"""

import os
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from delivery.envelope import generate_private_key, private_key_pem, public_key_pem


class Command(BaseCommand):
    help = "Generate the Ed25519 signing key; writes the private key, prints the public key."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--private-key", required=True, help="Where to write the private key (must not exist)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = Path(options["private_key"])
        if path.exists():
            raise CommandError(f"{path} exists; refusing to overwrite a signing key")
        key = generate_private_key()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise CommandError(f"cannot create {path}: {exc.strerror}") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(private_key_pem(key))
        self.stderr.write(f"private key written to {path} (mode 0600); the public key follows")
        self.stdout.write(public_key_pem(key.public_key()).decode(), ending="")
