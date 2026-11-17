from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.models import Site
from catalog.request_accounting import RequestAccounting, idle_sleep
from core.http import PoliteClient
from core.locks import CRAWL_WORKER_LOCKS, advisory_locks
from crawler.models import CrawlTask
from crawler.worker import CrawlWorker


class Command(BaseCommand):
    help = "Process crawl tasks: robots.txt, polite fetch, parse, store."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only tasks of this domain.")
        parser.add_argument("--limit", type=int, help="Stop after N tasks (implies --once).")
        parser.add_argument("--once", action="store_true", help="Exit when the queue is empty.")

    def handle(self, *args: Any, **options: Any) -> None:
        sites = Site.objects.all()
        if options["site"]:
            sites = sites.filter(domain=options["site"])
        # One crawler per site (review M9): the per-host delay lives in this process, so a second
        # worker for the same site would double the request rate.
        with advisory_locks(CRAWL_WORKER_LOCKS, sites.values_list("pk", flat=True)) as busy:
            if busy:
                domains = Site.objects.filter(pk__in=busy).order_by("domain")
                raise CommandError(
                    "another crawl worker is already running for "
                    f"{', '.join(domains.values_list('domain', flat=True))}; "
                    "one crawler per site, exiting"
                )
            user_agent = settings.CRAWLER_USER_AGENT
            # Every request is counted and budgeted (issue #74); closing the client flushes.
            with PoliteClient(user_agent, observer=RequestAccounting()) as client:
                worker = CrawlWorker(client, user_agent, on_result=self._report)
                processed = worker.run(
                    site_domain=options["site"],
                    limit=options["limit"],
                    once=options["once"],
                    sleep=idle_sleep(client),
                )
        self.stdout.write(f"processed {processed} task(s)")

    def _report(self, task: CrawlTask) -> None:
        detail = task.decision_reason or task.last_error
        self.stdout.write(f"{task.status:<8} {task.kind:<8} {task.url} {detail}".rstrip())
