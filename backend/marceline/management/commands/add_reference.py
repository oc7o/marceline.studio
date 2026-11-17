"""Fingerprint a local original with ffmpeg and register it (ADR 0010); never store the file.

Lives in the project package because registering an original also records its owner
(`--organisation`, ADR 0031), and `matching` may not know about organisations.
"""

import argparse
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError

from marceline.app.ownership import Via, describe, organisation_named, owner_for_new, register
from matching import embedding
from matching.fingerprint import file_sha256
from matching.pdq import MediaDecodeError
from matching.references import add_reference, resolve_creators


class Command(BaseCommand):
    help = "Import fingerprints from a local original video; never store the file."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--creator",
            action="append",
            required=True,
            dest="creators",
            metavar="NAME",
            help="Repeatable: the primary creator (rights holder) first, then co-performers.",
        )
        parser.add_argument("--file", required=True, type=Path)
        parser.add_argument("--title")
        parser.add_argument("--fps", type=float, default=1.0)
        parser.add_argument("--source-note", default="")
        parser.add_argument("--dihedral", action=argparse.BooleanOptionalAction, default=True)
        parser.add_argument(
            "--embeddings",
            action="store_true",
            help="Also store the second signal's 512-bit sketch per moment (ADR 0037); needs "
            "the pinned model (`fetch_embedding_model`).",
        )
        parser.add_argument(
            "--organisation",
            metavar="NAME",
            help="Organisation that owns this original. Without it the owner of an earlier "
            "reference of the same file is inherited; otherwise it stays unassigned.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            creator, co_creators = resolve_creators(options["creators"])
            chosen = options["organisation"]
            organisation = organisation_named(chosen) if chosen else None
            # Decide the owner before minutes of hashing, so a contradicting --organisation
            # imports nothing. Reading the digest twice costs far less than decoding the video.
            owner, source = owner_for_new(file_sha256(options["file"]), organisation)
            reference = add_reference(
                creator=creator,
                creators=co_creators,
                path=options["file"],
                title=options["title"],
                fps=options["fps"],
                source_note=options["source_note"],
                dihedral=options["dihedral"],
                embed=options["embeddings"],
            )
            register(reference, owner, via=Via.INHERITED if source else Via.IMPORT)
        except (
            ValueError,
            OSError,
            MediaDecodeError,
            IntegrityError,
            embedding.EmbeddingModelMissing,
            embedding.EmbeddingModelInvalid,
            embedding.EmbeddingUnavailable,
        ) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Reference {reference.pk}: {reference.frames.count()} hashes stored, "
            f"owner {describe(owner)}"
            + (f" (from reference {source.pk})" if source is not None else "")
        )
