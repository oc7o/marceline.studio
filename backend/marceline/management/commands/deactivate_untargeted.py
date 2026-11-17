from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from marceline.targets import (
    NoTargetsError,
    SiteActivity,
    TargetKind,
    apply_activity,
    group_targets,
    pending_broad_listings,
    plan_activity,
    stop_broad_listings,
    targets,
    uncrawled_watchlist,
)

COLUMNS = (
    ("pages", "pages"),
    ("active", "active"),
    ("keep", "active after"),
    ("deactivate", "deactivate"),
    ("reactivate", "reactivate"),
    ("frames", "frames"),
    ("frames_keep", "frames active after"),
    ("frames_deactivate", "frames deactivate"),
)


class Command(BaseCommand):
    help = (
        "Targeted indexing (issue #71): deactivate pages that belong to no target creator and "
        "reactivate pages that belong to one again. Deactivated pages are kept, never deleted, but "
        "matching, clustering, duplicates and rechecks skip them. Dry run unless --apply."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", help="Report counts per site only (the default)."
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Change page activity and rebuild the site x creator breadcrumbs.",
        )
        parser.add_argument(
            "--stop-broad-listings",
            action="store_true",
            help="With --apply: also skip pending newest/catalogue listing chains in the queue.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        hunted = targets()
        names = group_targets(hunted)
        for kind in TargetKind:
            listed = ", ".join(names.get(kind, [])) or "none"
            self.stdout.write(f"Targets ({kind}): {len(names.get(kind, []))}: {listed}")
        noted = [creator.name for creator in uncrawled_watchlist(hunted)]
        self.stdout.write(
            f"Watchlist, not crawled (no seeds, pages not kept active): {len(noted)}: "
            + (", ".join(noted) or "none")
        )
        plan = plan_activity(hunted)
        width = max([len("site"), *(len(row.domain) for row in plan.sites)])
        self.stdout.write("site".ljust(width) + "".join(f"  {label:>10}" for _, label in COLUMNS))
        for row in [*plan.sites, plan.total]:
            self.stdout.write(_row(row, width))
        self.stdout.write(
            "Kept because (overlapping): "
            + ", ".join(f"{name} {count}" for name, count in plan.reasons.items())
        )
        broad = pending_broad_listings().count()
        self.stdout.write(f"Pending broad-listing tasks (--stop-broad-listings): {broad}")
        if not options["apply"]:
            self.stdout.write("Dry run: nothing changed.")
            return
        try:
            result = apply_activity(hunted)
        except NoTargetsError as exc:
            raise CommandError(f"Refusing: {exc}") from exc
        self.stdout.write(
            f"Applied: {result.deactivated} page(s) deactivated, {result.reactivated} "
            f"reactivated; {result.breadcrumbs} breadcrumb(s) rebuilt."
        )
        if options["stop_broad_listings"]:
            self.stdout.write(f"Stopped {stop_broad_listings()} broad-listing task(s).")


def _row(row: SiteActivity, width: int) -> str:
    return row.domain.ljust(width) + "".join(
        f"  {getattr(row, name):>{max(10, len(label))}}" for name, label in COLUMNS
    )
