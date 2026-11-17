from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from marceline.runtime.lanes import LANES, PAGES, LaneOptions, LaneWork, MediaWork, PagesWork
from marceline.runtime.runtime import LaneBusyError, Runtime, RuntimeConfig
from matching import embedding

ALL_SITES = ("all", "*")


def parse_sites(values: list[str]) -> frozenset[str] | None:
    """`RUNTIME_SITES` / `--sites`: domains, or `all`. Nothing at all is refused (None = all)."""
    domains = {value.strip().lower() for value in values if value.strip()}
    if not domains:
        raise CommandError(
            "no sites to serve: set RUNTIME_SITES (or --sites) to domains or 'all' (ADR 0030)"
        )
    if domains & set(ALL_SITES):
        return None
    return frozenset(domains)


class Command(BaseCommand):
    help = (
        "Crawl runtime (ADR 0030): serve every selected site from one process per lane — "
        "pages (crawl tasks) or media (fetch + PDQ hash) — on database host slots."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--lane", choices=LANES, required=True)
        parser.add_argument(
            "--sites",
            help="Comma-separated domains or 'all'; default RUNTIME_SITES. Sites whose legacy "
            "per-site worker runs are skipped until it stops.",
        )
        parser.add_argument("--threads", type=int, help="Task threads (default per lane setting).")
        parser.add_argument(
            "--once", action="store_true", help="Exit once no served site has claimable work."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        raw = (
            options["sites"].split(",") if options["sites"] is not None else settings.RUNTIME_SITES
        )
        sites = parse_sites(list(raw))
        lane = options["lane"]
        pages = lane == PAGES
        threads = options["threads"] or (
            settings.RUNTIME_PAGE_THREADS if pages else settings.RUNTIME_MEDIA_THREADS
        )
        if threads < 1:
            raise CommandError("--threads must be at least 1")
        # Frame embeddings (ADR 0037) cost ~160 MB of resident set per hash child, so the number
        # of children is a memory decision, not only a throughput one: refuse to start rather
        # than let the media lane be OOM-killed. Verify the model up front too — a lane that
        # discovered a missing file on its first media would hash thousands of frames without the
        # second signal and record nothing about it.
        embed = bool(settings.MATCHING_EMBEDDINGS_ENABLED) and not pages
        model_path = str(settings.MATCHING_EMBEDDING_MODEL_PATH) or None
        processes = settings.RUNTIME_HASH_PROCESSES
        if embed:
            try:
                embedding.verify_model(embedding.model_path(model_path))
                processes, note = embedding.hash_processes(processes, embeddings=True)
            except (
                embedding.EmbeddingModelMissing,
                embedding.EmbeddingModelInvalid,
                embedding.EmbeddingUnavailable,
            ) as exc:
                raise CommandError(str(exc)) from exc
            if note:
                self.stderr.write(note)
        lane_options = LaneOptions(
            user_agent=settings.CRAWLER_USER_AGENT,
            deadline_s=settings.RUNTIME_PAGE_DEADLINE_S
            if pages
            else settings.RUNTIME_MEDIA_DEADLINE_S,
            slot_max_wait_s=settings.RUNTIME_SLOT_MAX_WAIT_S,
            cdn_delay_s=settings.CDN_HOST_DELAY_S,
            hash_processes=processes,
            embed=embed,
            model_path=model_path,
        )
        work: LaneWork = PagesWork(lane_options) if pages else MediaWork(lane_options)
        config = RuntimeConfig(
            threads=threads,
            sites=sites,
            max_tasks=settings.RUNTIME_MAX_TASKS,
            max_rss_mb=float(settings.RUNTIME_MAX_RSS_MB),
            max_uptime_s=settings.RUNTIME_MAX_UPTIME_H * 3600,
            once=options["once"],
        )
        try:
            reason = Runtime(work, config).run()
        except LaneBusyError as exc:
            work.close()
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{lane} lane stopped: {reason}")
