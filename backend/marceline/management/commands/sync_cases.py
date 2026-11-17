"""Reconcile every organisation's customer cases (ADR 0025): what a read does, for all at once.

Idempotent and safe next to customer reads (per-original lock in `marceline.app.cases`). Needed
wherever `CASE_SYNC_ON_READ` is off, and keeps first confirmation dates close to the decision.
"""

from typing import Any

from django.core.management.base import BaseCommand

from marceline.app.cases import sync_all_cases


class Command(BaseCommand):
    help = "Reconcile the stored cases of every original an organisation owns."

    def handle(self, *args: Any, **options: Any) -> None:
        views = sync_all_cases()
        with_case = sum(1 for view in views if view.case is not None)
        self.stdout.write(f"{len(views)} originals reconciled, {with_case} with a case")
