from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from catalog import site_objections
from catalog.models import Site, SiteObjection


class Command(BaseCommand):
    help = (
        "Record a site operator's objection and apply the decision through the existing switches "
        "(issue #108, ADR 0050, docs/runbooks/site-objection.md). --list shows the record; "
        "--record opens a case; --apply ID applies its decision (Site.enabled, the pause, the "
        "origin media budget) with the objection as the recorded reason. Never a reply, never a "
        "URL, never a deletion."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--list", action="store_true", help="Print the recorded objections.")
        parser.add_argument("--site", help="The domain the objection is about.")
        parser.add_argument(
            "--record",
            action="store_true",
            help="Record a new objection for --site (needs --received-on).",
        )
        parser.add_argument("--received-on", help="When it reached us, YYYY-MM-DD.")
        parser.add_argument(
            "--kind",
            choices=[value for value, _ in SiteObjection.Kind.choices],
            default=SiteObjection.Kind.OPERATOR_REQUEST,
        )
        parser.add_argument(
            "--channel",
            choices=[value for value, _ in SiteObjection.Channel.choices],
            default=SiteObjection.Channel.EMAIL,
        )
        parser.add_argument(
            "--note",
            default="",
            help="What was asked, in one or two lines. No name, address, telephone number or "
            "quoted letter — that stays in the mailbox.",
        )
        parser.add_argument(
            "--decision",
            choices=[value for value, _ in SiteObjection.Decision.choices],
            help="The decision to record (and to apply with --apply).",
        )
        parser.add_argument("--reason", default="", help="Why. Required to decline.")
        parser.add_argument("--decided-by", default="", help="Who decided (username or operator).")
        parser.add_argument(
            "--apply",
            type=int,
            metavar="ID",
            help="Apply this objection's recorded decision to the site's switches.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --apply: print what would change and write nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["record"]:
            self._record(options)
            return
        if options["apply"] is not None:
            self._apply(options)
            return
        self._list(options)

    def _list(self, options: dict[str, Any]) -> None:
        rows = SiteObjection.objects.select_related("site")
        if options["site"]:
            rows = rows.filter(site__domain=options["site"])
        printed = 0
        for row in rows:
            printed += 1
            applied = f"applied {row.applied_at:%Y-%m-%d}" if row.applied_at else "not applied"
            self.stdout.write(
                f"#{row.pk} {row.site.domain}: {row.kind} via {row.channel}, received "
                f"{row.received_on} -> {row.decision} ({applied})"
            )
            if row.decision_reason:
                self.stdout.write(f"    reason: {row.decision_reason}")
        if not printed:
            self.stdout.write("no site objection recorded")

    def _record(self, options: dict[str, Any]) -> None:
        if not options["site"]:
            raise CommandError("--record needs --site")
        if not options["received_on"]:
            raise CommandError("--record needs --received-on YYYY-MM-DD")
        site = Site.objects.filter(domain=options["site"]).first()
        if site is None:
            raise CommandError(f"no such site: {options['site']}")
        decision = options["decision"] or SiteObjection.Decision.PENDING
        if decision == SiteObjection.Decision.DECLINED and not options["reason"]:
            raise CommandError("declining needs --reason")
        objection = SiteObjection.objects.create(
            site=site,
            received_on=date.fromisoformat(options["received_on"]),
            kind=options["kind"],
            channel=options["channel"],
            note=options["note"],
            decision=decision,
            decision_reason=options["reason"],
            decided_by=options["decided_by"],
            decided_at=timezone.now() if decision != SiteObjection.Decision.PENDING else None,
        )
        self.stdout.write(
            f"recorded objection #{objection.pk} for {site.domain} ({decision}). "
            f"Apply it with --apply {objection.pk}."
        )

    def _apply(self, options: dict[str, Any]) -> None:
        objection = SiteObjection.objects.filter(pk=options["apply"]).select_related("site").first()
        if objection is None:
            raise CommandError(f"no such objection: {options['apply']}")
        now = timezone.now()
        changes = site_objections.changes_for(objection, now=now)
        if not changes:
            self.stdout.write(
                f"#{objection.pk} is {objection.decision}: nothing to apply to "
                f"{objection.site.domain}"
            )
            return
        for field, value in sorted(changes.items()):
            self.stdout.write(f"{objection.site.domain}.{field} -> {value}")
        if options["dry_run"]:
            self.stdout.write("dry run: nothing written")
            return
        site_objections.apply_decision(
            objection, actor=options["decided_by"] or "operator", now=now
        )
        self.stdout.write(f"applied objection #{objection.pk} to {objection.site.domain}")
