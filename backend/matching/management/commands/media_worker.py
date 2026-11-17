from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.models import Site
from catalog.request_accounting import RequestAccounting, idle_sleep
from core.http import PoliteClient
from core.locks import MEDIA_WORKER_LOCKS, advisory_locks
from index.models import Media
from matching import embedding
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker


class Command(BaseCommand):
    help = "Fetch pending media, PDQ-hash it and store the frame hashes (bytes are discarded)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only media of pages on this domain.")
        parser.add_argument("--limit", type=int, help="Stop after N media (implies --once).")
        parser.add_argument("--once", action="store_true", help="Exit when the queue is empty.")
        # Frame embeddings (ADR 0037) are opt-in *here* even when the setting is on, and that is
        # deliberate: there is one legacy media worker per site — about twenty processes — and
        # each one holding the model would cost ~160 MB, i.e. more than the development box has
        # spare. The second signal belongs in the crawl runtime's shared hash pool
        # (`crawl_runtime --lane media`, ADR 0030), which the setting does switch on. Use this
        # flag for a single site, e.g. the pilot's re-fetch.
        parser.add_argument(
            "--embed",
            action="store_true",
            help="Also compute the second signal's sketch (ADR 0037); needs the pinned model.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["embed"]:
            path = embedding.model_path(settings.MATCHING_EMBEDDING_MODEL_PATH or None)
            try:
                embedding.verify_model(path)
            except (embedding.EmbeddingModelMissing, embedding.EmbeddingModelInvalid) as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"embedding every frame with {embedding.EMBEDDING_PIPELINE}")
        sites = Site.objects.all()
        if options["site"]:
            sites = sites.filter(domain=options["site"])
        # One media worker per site (review M9): the per-host delay lives in this process.
        with advisory_locks(MEDIA_WORKER_LOCKS, sites.values_list("pk", flat=True)) as busy:
            if busy:
                domains = Site.objects.filter(pk__in=busy).order_by("domain")
                raise CommandError(
                    "another media worker is already running for "
                    f"{', '.join(domains.values_list('domain', flat=True))}; "
                    "one media worker per site, exiting"
                )
            # Every request is counted and budgeted (issue #74); closing the client flushes.
            # Media URLs come from crawled pages: every hop must reach a public address only, and
            # the connection goes to the address that was checked (SSRF, DNS rebinding).
            with PoliteClient(
                settings.CRAWLER_USER_AGENT,
                max_bytes=CLIENT_MAX_BYTES,
                observer=RequestAccounting(),
                public_only=True,
            ) as client:
                worker = MediaWorker(
                    client,
                    on_result=self._report,
                    user_agent=settings.CRAWLER_USER_AGENT,
                    embed=options["embed"],
                )
                processed = worker.run(
                    site_domain=options["site"],
                    limit=options["limit"],
                    once=options["once"],
                    sleep=idle_sleep(client),
                )
        reused = ", ".join(f"{count} by {reason}" for reason, count in worker.reused.items())
        self.stdout.write(f"processed {processed} media (hashes reused: {reused})")

    def _report(self, media: Media) -> None:
        frames = media.hashes.count() if media.status == Media.Status.DONE else 0
        detail = media.last_error or f"{frames} frame hash(es)"
        if media.status == Media.Status.DONE and hasattr(media, "reuse"):
            detail += f" reused from media {media.reuse.source_id} ({media.reuse.reason})"
        self.stdout.write(f"{media.status:<8} {media.kind:<8} {media.url} {detail}")
