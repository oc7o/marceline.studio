from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from matching import metadata_candidates
from matching.capture_triage import METADATA_SETTING


class Command(BaseCommand):
    help = (
        "Report the third signal's candidates (ADR 0040): originals whose duration, cast and "
        "title a page's stored metadata is consistent with. Requests nothing, reads no pixels, "
        "works on every page already indexed — and writes nothing at all. Since the amendment of "
        "2026-09-18 a candidate is an aiming signal, not a review item: the browser-capture "
        "triage of ADR 0016 turns it into 'fingerprint this page next' and PDQ then decides by "
        "itself. To see what would actually be queued, run "
        "`triage_browser_capture --dry-run --metadata-candidates`."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        report = metadata_candidates.aim()
        self.stdout.write(report.line())
        if reason := report.empty_reason():
            self.stdout.write(f"  nothing proposed: {reason}")
        else:
            self.stdout.write(f"  {report.aim_line()}")
        state = "on" if getattr(settings, METADATA_SETTING, False) else "off"
        self.stdout.write(
            "Nothing was written: this signal puts nothing in front of a human (ADR 0040 "
            f"amendment). It aims browser captures, and only while {METADATA_SETTING} is on — "
            f"it is currently {state}."
        )
