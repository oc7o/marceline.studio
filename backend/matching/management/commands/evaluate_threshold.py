from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from matching.duplicates import DEFAULT_MAX_DISTANCE
from matching.evaluate import evaluate_thresholds, suggest_threshold
from matching.pdq import MIN_QUALITY
from matching.services import labeled_pairs


class Command(BaseCommand):
    help = "Precision/recall of PDQ distance thresholds against same/different PairLabels."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-threshold", type=int, default=64)
        parser.add_argument("--min-precision", type=float, default=0.95)
        parser.add_argument("--min-score", type=float, default=0.0)
        parser.add_argument(
            "--score-distance",
            type=int,
            default=DEFAULT_MAX_DISTANCE,
            help="Distance at which frames count as matched when computing pair scores.",
        )
        parser.add_argument("--min-quality", type=int, default=MIN_QUALITY)

    def handle(self, *args: Any, **options: Any) -> None:
        pairs = labeled_pairs(options["score_distance"], options["min_quality"])
        if not pairs:
            self.stdout.write("No same/different pair labels yet. Label pairs in the admin first.")
            return
        same = sum(pair.same for pair in pairs)
        self.stdout.write(
            f"{len(pairs)} labeled pairs ({same} same, {len(pairs) - same} different)"
        )
        results = evaluate_thresholds(
            pairs, range(options["max_threshold"] + 1), options["min_score"]
        )
        self.stdout.write("thresh  precision  recall     f1   tp   fp   fn   tn")
        for r in results:
            self.stdout.write(
                f"{r.threshold:6d}  {r.precision:9.3f}  {r.recall:6.3f}  {r.f1:5.3f}  "
                f"{r.true_positives:3d}  {r.false_positives:3d}  "
                f"{r.false_negatives:3d}  {r.true_negatives:3d}"
            )
        best = suggest_threshold(results, options["min_precision"])
        if best is None:
            self.stdout.write(
                self.style.WARNING(f"no threshold reaches precision {options['min_precision']}")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"suggested threshold: {best.threshold} "
                    f"(precision {best.precision:.3f}, recall {best.recall:.3f})"
                )
            )
