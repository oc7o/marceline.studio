"""Run the frame-embedding pilot exactly as ADR 0037 pre-registered it.

    manage.py embedding_pilot --stage plan      # what would be fetched, and what it costs
    manage.py embedding_pilot --stage fetch     # the bounded re-fetch (real requests)
    manage.py embedding_pilot --stage measure   # the three numbers and the verdict

`plan` and `measure` write nothing and request nothing. `fetch` is the only stage that touches the
network, needs `--apply`, and honours the media gate, robots.txt, the per-host delays and the
request budgets like every other fetch in this system — `index.media_fetch` is the only door.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.request_accounting import RequestAccounting
from core.http import PoliteClient
from index.models import Media
from marceline import embedding_pilot
from matching import embedding, embedding_candidates
from matching.media_robots import MediaRobots
from matching.media_worker import CLIENT_MAX_BYTES

STAGES = ("plan", "fetch", "measure")


class Command(BaseCommand):
    help = (
        "The frame-embedding pilot (ADR 0037): plan the bounded page set, re-fetch it and embed "
        "in memory, then measure against the pre-registered exit criterion."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--stage", choices=STAGES, default="plan")
        parser.add_argument(
            "--apply", action="store_true", help="Required by --stage fetch: make real requests."
        )
        parser.add_argument("--site", help="Only media of pages on this domain.")
        parser.add_argument(
            "--limit", type=int, help="Stop after N media (spread the set over a day)."
        )
        parser.add_argument(
            "--refresh-candidates",
            action="store_true",
            help="With --stage measure: refresh the candidates first (same as match_references).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        stage = options["stage"]
        if stage == "plan":
            self._plan()
        elif stage == "fetch":
            self._fetch(options)
        else:
            self._measure(options)

    def _plan(self) -> None:
        plan = embedding_pilot.plan()
        for line in plan.lines():
            self.stdout.write(line)
        self.stdout.write(
            "Nothing was requested or written. `--stage fetch --apply` makes the requests."
        )

    def _fetch(self, options: dict[str, Any]) -> None:
        if not options["apply"]:
            raise CommandError("--stage fetch makes real requests; pass --apply to confirm")
        model = embedding.model_path(settings.MATCHING_EMBEDDING_MODEL_PATH or None)
        try:
            embedding.verify_model(model)
        except (embedding.EmbeddingModelMissing, embedding.EmbeddingModelInvalid) as exc:
            raise CommandError(str(exc)) from exc
        plan = embedding_pilot.plan()
        if not plan.references_embedded:
            raise CommandError(
                "no original carries sketches yet, so a re-fetch could not be compared with "
                "anything. Re-fingerprint the originals first "
                "(`client/fingerprint.py --embeddings`, then `import_fingerprints`)."
            )
        if over := plan.over_budget():
            names = ", ".join(
                f"{domain} ({count} > {allowed})" for domain, (count, allowed) in over.items()
            )
            self.stderr.write(
                f"warning: {names} — run with --site and --limit over several days instead"
            )
        rows = (
            Media.objects.filter(page_id__in=plan.pages, status=Media.Status.DONE)
            .exclude(kind=Media.Kind.CAPTURE)
            .exclude(eligible_at__isnull=True)
            .select_related("page", "page__site")
            .order_by("pk")
        )
        if options["site"]:
            rows = rows.filter(page__site__domain=options["site"])
        if options["limit"]:
            rows = rows[: options["limit"]]
        media_rows = list(rows)
        self.stdout.write(
            f"re-fetching {len(media_rows)} media with {embedding.EMBEDDING_PIPELINE}"
        )
        robots = MediaRobots(None, str(settings.CRAWLER_USER_AGENT))
        with PoliteClient(
            str(settings.CRAWLER_USER_AGENT),
            max_bytes=CLIENT_MAX_BYTES,
            observer=RequestAccounting(),
            public_only=True,
        ) as client:
            report = embedding_pilot.fetch_and_embed(
                client,
                media_rows,
                robots=robots,
                model_path=settings.MATCHING_EMBEDDING_MODEL_PATH or None,
            )
        for line in report.lines():
            self.stdout.write(line)

    def _measure(self, options: dict[str, Any]) -> None:
        if options["refresh_candidates"]:
            refreshed = embedding_candidates.refresh(top_k=settings.MATCHING_EMBEDDING_TOP_K)
            self.stdout.write(refreshed.line())
            if reason := refreshed.empty_reason():
                self.stdout.write(f"  nothing to compare: {reason}")
        measurement = embedding_pilot.measure()
        for line in measurement.lines():
            self.stdout.write(line)
