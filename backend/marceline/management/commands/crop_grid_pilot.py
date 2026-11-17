"""Run the reference-side crop grid pilot exactly as ADR 0039 pre-registered it.

    manage.py crop_grid_pilot --stage select    # which 8 originals to re-fingerprint, and the cost
    manage.py crop_grid_pilot --stage plan      # what can be judged, once they are imported
    manage.py crop_grid_pilot --stage measure   # the three numbers and the verdict

**Every stage is read-only and makes no request.** Unlike the frame-embedding pilot (ADR 0037),
which had to re-fetch about 3,000 media before it could measure anything, the crop grid is
retroactive: it is compared against page hashes that are already stored. There is deliberately no
`--apply` here, because there is nothing to apply — the work happens on the operator's Mac
(`client/fingerprint.py --crop-grid`) and arrives through `import_fingerprints`.

`measure` refuses to declare success on anything the criteria do not cover: no gridded original, no
near-miss page or no human verdict on the new proposals reads as NOT MEASURED, never as a pass.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from marceline import crop_grid_pilot
from matching import crop_index

STAGES = ("select", "plan", "measure")


class Command(BaseCommand):
    help = (
        "The reference-side crop grid pilot (ADR 0039): pick the originals to re-fingerprint, "
        "see what can be judged, then measure against the pre-registered exit criterion. "
        "Read-only; no requests."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--stage", choices=STAGES, default="select")
        parser.add_argument(
            "--originals",
            type=int,
            default=crop_grid_pilot.PILOT_ORIGINALS,
            help="With --stage select: how many originals to recommend (default 8, the "
            "operator's decision of 2026-09-18).",
        )
        parser.add_argument(
            "--sha256",
            action="append",
            dest="sha256s",
            metavar="DIGEST",
            help="Repeatable: judge only these files instead of every gridded one.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        stage = options["stage"]
        if stage == "select":
            lines = crop_grid_pilot.selection_lines(options["originals"])
        elif stage == "plan":
            lines = crop_grid_pilot.plan(options["sha256s"]).lines()
        else:
            lines = crop_grid_pilot.measure(options["sha256s"]).lines()
        for line in lines:
            self.stdout.write(line)
        if stage != "select" and not crop_index.enabled():
            self.stdout.write(
                "Note: MATCHING_REFERENCE_CROP_GRID is off, so the matcher itself still ignores "
                "the grids. This stage reads them directly and is unaffected."
            )
