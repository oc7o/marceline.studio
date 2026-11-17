"""Import hashes-only reference files from `client/fingerprint.py` (ADR 0010, 0020, 0022).

Lives in the project package because an import also records the original's owner
(`--organisation`, or inherited from an earlier reference of the same file; ADR 0031), and
`matching` may not know about organisations. The import itself is `matching.references`.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError, transaction

from accounts.models import Organisation
from catalog.models import Creator
from marceline.app.ownership import Via, describe, organisation_named, owner_for_new, register
from matching.creators import NAME_SEPARATOR, creators_of
from matching.fingerprint import Fingerprint, compare_frames, from_json
from matching.models import Reference
from matching.references import (
    attach_embeddings,
    import_fingerprint,
    mark_shared_frames,
    resolve_creators,
)


@dataclass(frozen=True)
class Ignored:
    """A file the operator has permanently decided to never import, and why."""

    sha256: str
    reason: str


# Files the operator has permanently decided to never import, so an accidental re-upload of the
# same file imports nothing — a durable property of the import, not a one-off decision made once
# at the terminal. A handful of entries belongs in version control as a constant, not in a new
# model or setting. Full sha256 preferred; see `ignored_reason` for how a shorter entry matches.
IGNORED_FILES: tuple[Ignored, ...] = (
    Ignored(
        # Full digest unknown: this file exists only in the operator's local fingerprinting data
        # (never imported, so its full sha256 was never stored anywhere reachable from here).
        # Matches by this 12-hex-character prefix instead of the full 64 — see `ignored_reason`.
        sha256="0a1b2c3d4e5f",
        reason='"Sample Title 6" - cast never settled (operator decision, 2026-09-17)',
    ),
)


def ignored_reason(sha256: str) -> str | None:
    """Why this file's sha256 is permanently ignored, or None.

    Matches a full digest exactly; a shorter, documented prefix (only for an `IGNORED_FILES` entry
    whose full digest could not be recovered) matches too, since an exact match is just the case
    where the prefix is the whole string. Never generalise this into prefix lookup for anything
    else in the import — a handful of operator decisions is a constant, not a feature.
    """
    return next((entry.reason for entry in IGNORED_FILES if sha256.startswith(entry.sha256)), None)


def grid_rate(fps: float | None) -> str:
    """` at 2 fps` for a video reference, nothing for a photo collection."""
    return "" if fps is None else f" at {fps:g} fps"


def grid_note(fingerprint: Fingerprint) -> str:
    """`, crop grids on 2,940 moments (23.7 MB)` for a gridded file, nothing for any other.

    Worth printing because it is the one import whose size an operator can feel: 8.2 KB per moment
    against the 512 bytes of plain hashes, 629 MB for the whole library (ADR 0039 §8.2). A moment
    whose corner crops fell below the quality floor has no grid and simply keeps matching on its
    plain hash, which is why the count is not always the frame count.
    """
    if fingerprint.crop_grid is None:
        return ""
    blobs = [frame.crop_grid for frame in fingerprint.frames if frame.crop_grid]
    return (
        f", crop grids on {len(blobs)} moments ({sum(len(b) for b in blobs) / 2**20:.1f} MB) "
        f"— re-run `calibrate_confidence` after switching MATCHING_REFERENCE_CROP_GRID on"
    )


def fingerprint_files(paths: list[Path]) -> list[Path]:
    """JSON files named on the command line, plus `*.json` directly inside directories."""
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    return files


class Outcome(NamedTuple):
    """One file's result: the printed line, whether it was imported, and whether it was ignored."""

    line: str
    created: bool = False
    ignored: bool = False


class Command(BaseCommand):
    help = (
        "Import reference fingerprint files computed by client/fingerprint.py (hashes only): "
        "video originals and photo collections (one file is one collection)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("paths", nargs="+", type=Path, metavar="PATH")
        parser.add_argument(
            "--creator",
            action="append",
            dest="creators",
            metavar="NAME",
            help="Repeatable; overrides the creators named in the files. The first is the "
            "primary creator (rights holder), the rest co-performers.",
        )
        parser.add_argument(
            "--organisation",
            metavar="NAME",
            help="Organisation that owns the imported originals. Without it each file inherits "
            "the owner of an earlier reference of the same file (like the cast); otherwise it "
            "stays unassigned, invisible to customers.",
        )
        parser.add_argument("--source-note", default="")
        parser.add_argument("--dry-run", action="store_true", help="Validate only; write nothing.")
        parser.add_argument(
            "--compare",
            action="store_true",
            help="For files already imported, report hash parity with the stored reference.",
        )
        # A re-fingerprint that only adds embeddings needs no flag: a file on the same grid whose
        # sketches the stored reference lacks attaches them (ADR 0037), and one whose grid differs
        # is imported beside it as always.

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            chosen = options["organisation"]
            options["organisation"] = organisation_named(chosen) if chosen else None
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        imported = errors = ignored = 0
        for path in fingerprint_files(options["paths"]):
            try:
                outcome = self.process(path, options)
            except (ValueError, OSError, DatabaseError) as exc:
                errors += 1
                outcome = Outcome(f"error: {exc}")
            imported += outcome.created
            ignored += outcome.ignored
            self.stdout.write(f"{path.name}: {outcome.line}")
        if imported and not options["dry_run"]:
            mark_shared_frames()
        verb = "would import" if options["dry_run"] else "imported"
        self.stdout.write(f"{verb} {imported}, ignored {ignored}, errors {errors}")
        if errors:
            raise CommandError(f"{errors} fingerprint file(s) failed")

    def process(self, path: Path, options: dict[str, Any]) -> Outcome:
        fingerprint = from_json(json.loads(path.read_text(encoding="utf-8")))
        if (reason := ignored_reason(fingerprint.sha256)) is not None:
            # Never even queried: an ignored file creates no Reference, no frame hashes and no
            # ownership row, on a dry run or a --compare exactly like a real import.
            return Outcome(f"ignored: {reason}", ignored=True)
        rows = list(Reference.objects.filter(sha256=fingerprint.sha256).order_by("pk"))

        def same_grid(row: Reference) -> bool:
            return row.pipeline == fingerprint.pipeline and row.frame_fps == fingerprint.fps

        same = next((row for row in rows if same_grid(row)), None)
        # The same original hashed by an older recipe, or at another sampling rate (a 2 fps
        # re-fingerprint of a 1 fps original), sits on a different sampling grid, so this file is
        # imported *beside* it instead of being refused (issue #70, ADR 0022 and its 2026-09-17
        # amendment). The old reference keeps matching until the operator retires it.
        others = [row for row in rows if not same_grid(row)]
        if same is not None:
            if options["compare"]:
                return Outcome(self.parity(same, fingerprint))
            # Same grid, and the file brings the second signal the stored row does not have yet:
            # attach the sketches to the existing rows instead of importing the same PDQ hashes
            # twice (ADR 0037). The frame grid is unchanged, so the reference's identity is too.
            if fingerprint.embedding_pipeline and not same.embedding_pipeline:
                positions = sum(1 for frame in fingerprint.frames if frame.embedding is not None)
                if options["dry_run"]:
                    return Outcome(
                        f"would add {positions} {fingerprint.embedding_pipeline} sketches to "
                        f"reference {same.pk}"
                    )
                attachment = attach_embeddings(fingerprint)
                return Outcome(
                    f"added {attachment.positions} {fingerprint.embedding_pipeline} sketches to "
                    f"reference {attachment.reference_id}"
                )
            return Outcome(f"skipped (already imported as reference {same.pk})")
        if others and options["compare"]:
            # Prefer the row on the same recipe: only the rate differs, so the moments both grids
            # sample must hash identically.
            target = sorted(others, key=lambda row: row.pipeline != fingerprint.pipeline)[0]
            return Outcome(self.parity(target, fingerprint))
        beside = "".join(
            f" (alongside reference {row.pk} on {row.pipeline}{grid_rate(row.frame_fps)})"
            for row in others
        )
        creator, co_creators, inherited = self.creators_for(fingerprint, others, options)
        who = NAME_SEPARATOR.join([creator.name, *(row.name for row in co_creators)])
        # Never inherit silently: the cast came from another row, so say which one — on the
        # dry run *and* on the import, since the import line is the one that gets read.
        cast_note = f" (cast from reference {inherited.pk}: {who})" if inherited is not None else ""
        counts = f"{fingerprint.frame_count} {fingerprint.unit}, {len(fingerprint.frames)} hashes"
        counts += grid_note(fingerprint)
        chosen: Organisation | None = options["organisation"]
        owner, source = owner_for_new(fingerprint.sha256, chosen)
        # Same rule as the cast: an inherited owner is always named with where it came from.
        owner_note = f", owner {describe(owner)}" + (
            f" (owner from reference {source.pk})" if source is not None else ""
        )
        if options["dry_run"]:
            return Outcome(f"would import for {who}: {counts}{beside}{owner_note}", created=True)
        with transaction.atomic():
            reference = import_fingerprint(
                creator=creator,
                fingerprint=fingerprint,
                creators=co_creators,
                source_note=options["source_note"],
                mark_shared=False,
            )
            register(reference, owner, via=Via.INHERITED if source is not None else Via.IMPORT)
        return Outcome(
            f"imported reference {reference.pk}: {counts}{beside}{cast_note}{owner_note}",
            created=True,
        )

    def creators_for(
        self,
        fingerprint: Fingerprint,
        others: list[Reference],
        options: dict[str, Any],
    ) -> tuple[Creator, list[Creator], Reference | None]:
        """The cast for this file: `--creator`, else the file's own, else the same file's cast.

        A re-fingerprinted original keeps its sha256, so a reference with that digest on another
        pipeline is *the same video* and its cast is authoritative. Bulk re-runs after a pipeline
        change therefore need no `--creator` at all (issue #70). Inheriting is never silent: the
        caller prints which reference the names came from.
        """
        names = options["creators"] or list(fingerprint.creators)
        if names:
            try:
                creator, co_creators = resolve_creators(names)
            except ValueError:
                if options["creators"] or not others:
                    raise
            else:
                return creator, co_creators, None
        source = others[0] if others else None
        if source is None:
            raise ValueError(
                "no creator in the file and no earlier reference of it; pass --creator"
            )
        cast = creators_of(source)
        return cast[0], cast[1:], source

    def parity(self, reference: Reference, fingerprint: Fingerprint) -> str:
        # Same rate: compare by sample index. Different rates: frame_index means different moments,
        # so compare the positions both grids sample, by timestamp (to the millisecond).
        by_time = reference.frame_fps != fingerprint.fps and fingerprint.fps is not None
        stored = (
            (round(timestamp * 1000) if by_time else index, orientation, bytes(pdq))
            for index, timestamp, orientation, pdq in reference.frames.values_list(
                "frame_index", "timestamp_s", "dihedral", "pdq"
            )
        )
        result = compare_frames(
            (
                (
                    round(frame.timestamp_s * 1000) if by_time else frame.frame_index,
                    frame.dihedral,
                    frame.pdq,
                )
                for frame in fingerprint.frames
            ),
            stored,
        )

        def rate(value: float | None) -> str:
            """A photo collection has no sampling rate at all."""
            return "none" if value is None else f"{value:g}"

        notes = []
        if reference.frame_fps != fingerprint.fps:
            stored_fps, file_fps = rate(reference.frame_fps), rate(fingerprint.fps)
            notes.append(
                f" (fps differs: stored {stored_fps}, file {file_fps} - compared at the moments "
                "both sample; this file would import as a denser grid beside the stored one)"
            )
        if reference.pipeline != fingerprint.pipeline:
            notes.append(
                f" (pipeline differs: stored {reference.pipeline}, file {fingerprint.pipeline}"
                " - different sampling grids, so a large distance here is expected and this file"
                " would import as a second original, not as a duplicate)"
            )
        return (
            f"compare with reference {reference.pk}: {result.both} pairs in both, "
            f"{result.only_first} only in file, {result.only_second} only stored; "
            f"distance 0 for {result.identical_share:.1%}, p50 {result.p50}, p90 {result.p90}, "
            f"p99 {result.p99}, max {result.max}" + "".join(notes)
        )
