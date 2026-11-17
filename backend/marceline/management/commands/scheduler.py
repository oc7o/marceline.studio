from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from marceline import scheduler


class Command(BaseCommand):
    help = "Run periodic batch jobs (crawl seeds, re-checks, clusters, reference matching)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Run due jobs once and exit.")
        parser.add_argument("--only", metavar="JOB", help="Only this job.")

    def handle(self, *args: Any, **options: Any) -> None:
        jobs = list(scheduler.JOBS)
        if options["only"]:
            jobs = [job for job in jobs if job.name == options["only"]]
            if not jobs:
                names = ", ".join(job.name for job in scheduler.JOBS)
                raise CommandError(f"Unknown job {options['only']!r}; choose from: {names}")
        try:
            for job in jobs:
                job.interval()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if not scheduler.run_scheduler(jobs, once=options["once"]):
            # Like `crawl_worker` and `crawl_runtime`: a run that did nothing must not exit 0.
            # `scheduler --only page_retention` next to the scheduler container would otherwise
            # look as though it had run, and the compose service would look healthy while the
            # lock it lost to is held by a process that is already gone.
            raise CommandError(
                "another scheduler holds the single-instance lock; no job was run, exiting"
            )
