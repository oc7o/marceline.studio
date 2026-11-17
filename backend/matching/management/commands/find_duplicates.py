from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from index.models import Page
from matching.duplicates import DEFAULT_MAX_DISTANCE
from matching.pdq import MIN_QUALITY
from matching.services import find_candidates, propose_pairs


class Command(BaseCommand):
    help = "Find page pairs with matching PDQ frame hashes; optionally queue them for labeling."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-distance", type=int, default=DEFAULT_MAX_DISTANCE)
        parser.add_argument("--min-score", type=float, default=0.0)
        parser.add_argument("--min-quality", type=int, default=MIN_QUALITY)
        parser.add_argument("--cross-site-only", action="store_true")
        parser.add_argument(
            "--limit", type=int, default=50, help="Top candidates to print and label."
        )
        parser.add_argument(
            "--create-labels",
            action="store_true",
            help="Create UNSURE PairLabels for the printed candidates (review them in admin).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        candidates = find_candidates(
            max_distance=options["max_distance"],
            min_score=options["min_score"],
            min_quality=options["min_quality"],
            cross_site_only=options["cross_site_only"],
        )
        top = candidates[: options["limit"]]
        pages = Page.objects.select_related("site").in_bulk(
            {page_id for c in top for page_id in (c.page_a, c.page_b)}
        )
        self.stdout.write(f"{len(candidates)} candidate pairs, showing {len(top)}")
        self.stdout.write("score  frames  dist  page_a <-> page_b")
        for c in top:
            a, b = pages[c.page_a], pages[c.page_b]
            self.stdout.write(
                f"{c.score:5.2f}  {c.matched_frames:6d}  {c.min_distance:4d}  "
                f"#{a.pk} {a.site.domain}: {a.title[:40]}  <->  "
                f"#{b.pk} {b.site.domain}: {b.title[:40]}"
            )
        if options["create_labels"]:
            created = propose_pairs(top)
            self.stdout.write(self.style.SUCCESS(f"created {created} UNSURE pair labels"))
