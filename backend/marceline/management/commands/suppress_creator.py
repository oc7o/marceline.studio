"""Apply or lift an index suppression for a name (docs/suppression.md).

The route for a performer who is not our customer and wants us to stop indexing them. Dry run
unless `--apply`, idempotent, and it prints every link, breadcrumb and stored field it would
touch before it touches anything:

    manage.py suppress_creator --creator "Some Name"                       # dry run
    manage.py suppress_creator --creator "Some Name" --apply \
        --basis objection --requested-on 2026-09-17 --identity-check platform_message \
        --identity-note "message from the account that publishes the name" --decided-by operator
    manage.py suppress_creator --creator "Some Name" --lift \
        --reason "withdrawn by the person" --apply
    manage.py suppress_creator --list

Read docs/suppression.md before the first one: what to answer, what not to send back (never a
list of URLs), the deadline and what to record.
"""

from datetime import date, datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.models import Creator, CreatorSuppression
from catalog.suppression import active_suppressions, suppression_of
from marceline.suppression import (
    SuppressionPlan,
    SuppressionRefused,
    apply_suppression,
    lift_suppression,
    plan_suppression,
)


class Command(BaseCommand):
    help = (
        "Suppress a name from the index (docs/suppression.md): drop its page links, breadcrumbs, "
        "name-bearing stored extraction, watchlist entry, prospect verdicts and discovery trail, "
        "and keep it out of seeding, linking, discovery and reports for good. --lift reverses the "
        "marker (nothing is restored). Dry run unless --apply; idempotent."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--creator", metavar="NAME", help="The name to suppress or release.")
        parser.add_argument(
            "--list", action="store_true", help="List the suppressions in force; write nothing."
        )
        parser.add_argument(
            "--lift", action="store_true", help="Lift the suppression instead of applying it."
        )
        parser.add_argument("--reason", default="", help="Why it is lifted (required with --lift).")
        parser.add_argument(
            "--basis",
            choices=[choice for choice, _ in CreatorSuppression.Basis.choices],
            default=CreatorSuppression.Basis.OBJECTION,
            help="What the request was (default: objection, Art. 21 GDPR).",
        )
        parser.add_argument(
            "--requested-on",
            metavar="YYYY-MM-DD",
            help="When the request reached us (default: today).",
        )
        parser.add_argument(
            "--identity-check",
            choices=[choice for choice, _ in CreatorSuppression.IdentityCheck.choices],
            default=CreatorSuppression.IdentityCheck.NOT_VERIFIED,
            help="How identity was checked (paperwork, never a measurement).",
        )
        parser.add_argument(
            "--identity-note", default="", help="What was checked. No contact details."
        )
        parser.add_argument("--note", default="", help="Staff note. No contact details.")
        parser.add_argument(
            "--decided-by", default="operator", help="Who decided (username or 'operator')."
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Report only (the default).")
        mode.add_argument("--apply", action="store_true", help="Write it.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["list"]:
            self._list()
            return
        if not options["creator"]:
            raise CommandError("name the person: --creator NAME (or --list)")
        creator = Creator.objects.filter(name=options["creator"]).first()
        if creator is None:
            raise CommandError(
                f"unknown creator: {options['creator']!r}. Suppression works on a stored name; "
                "add the name (and every spelling as an alias) in the admin first."
            )
        if options["lift"]:
            self._lift(creator, options)
            return
        self._suppress(creator, options)

    # --- listing ------------------------------------------------------------------------------

    def _list(self) -> None:
        rows = list(active_suppressions())
        if not rows:
            self.stdout.write("No suppression in force.")
            return
        self.stdout.write(f"{len(rows)} suppression(s) in force:")
        for row in rows:
            answered = row.answered_on.isoformat() if row.answered_on else "not answered"
            self.stdout.write(
                f"  {row.creator.name!r}: {row.basis}, requested {row.requested_on}, "
                f"answer due {row.answer_due_on} ({answered}), identity {row.identity_check}, "
                f"links removed {row.removed_page_links}, breadcrumbs {row.removed_breadcrumbs}, "
                f"pages redacted {row.redacted_pages}"
            )

    # --- suppressing --------------------------------------------------------------------------

    def _suppress(self, creator: Creator, options: dict[str, Any]) -> None:
        requested_on = _requested_on(options["requested_on"])
        existing = suppression_of(creator)
        if existing is not None and existing.active:
            self.stdout.write(
                f"{creator.name!r} is already suppressed (requested {existing.requested_on}); "
                "re-running only re-applies it to anything stored since."
            )
        plan = plan_suppression(creator)
        for line in _plan_lines(plan, apply=options["apply"]):
            self.stdout.write(line)
        if not options["apply"]:
            self.stdout.write("Dry run, nothing written. Re-run with --apply.")
            return
        try:
            applied, suppression = apply_suppression(
                creator,
                basis=options["basis"],
                requested_on=requested_on,
                identity_check=options["identity_check"],
                identity_note=options["identity_note"],
                note=options["note"],
                decided_by=options["decided_by"],
            )
        except SuppressionRefused as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Applied. {creator.name!r} is suppressed: answer due {suppression.answer_due_on} "
            f"(Art. 12(3) GDPR, one month), identity {suppression.identity_check}."
        )
        self.stdout.write(
            "Next: answer the requester from the template in docs/suppression.md (no URLs, no "
            "site names, no counts), record the answer date in the admin (Catalog -> Creator "
            "suppressions), then run `deactivate_untargeted --apply` so pages kept only by this "
            f"link go inactive ({applied.page_links} link(s) were removed)."
        )

    # --- lifting ------------------------------------------------------------------------------

    def _lift(self, creator: Creator, options: dict[str, Any]) -> None:
        existing = suppression_of(creator)
        if existing is None:
            raise CommandError(f"{creator.name!r} has no suppression to lift")
        if not existing.active:
            self.stdout.write(
                f"{creator.name!r} is not suppressed (lifted {existing.lifted_at:%Y-%m-%d}). "
                "Nothing to do."
            )
            return
        if not options["reason"].strip():
            raise CommandError("say why the suppression is lifted: --reason TEXT")
        self.stdout.write(
            f"{creator.name!r}: the suppression of {existing.requested_on} would be lifted. "
            "Nothing is restored — the next crawl of a page re-links what it credits and "
            "rewrites the stored extraction this suppression redacted."
        )
        if not options["apply"]:
            self.stdout.write("Dry run, nothing written. Re-run with --apply.")
            return
        try:
            suppression = lift_suppression(
                creator, reason=options["reason"], decided_by=options["decided_by"]
            )
        except SuppressionRefused as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Lifted {suppression.lifted_at:%Y-%m-%d}. The record stays with its reason."
        )


def _requested_on(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"--requested-on must be YYYY-MM-DD, not {value!r}") from exc


def _plan_lines(plan: SuppressionPlan, *, apply: bool) -> list[str]:
    """What the run would change (or changed): counts only, never a URL or a page title."""
    verb = "" if apply else "would be "
    names = ", ".join(plan.names)
    lines = [
        f"creator {plan.creator.pk} {plan.creator.name!r} (names covered: {names})",
        f"  page links:        {plan.page_links} {verb}removed, on {plan.link_sites} site(s)",
        f"  breadcrumbs:       {plan.breadcrumbs} site x creator row(s) {verb}removed",
        f"  stored extraction: {plan.redacted_pages} safety-clear page(s) {verb}changed"
        + (f" ({_fields(plan)})" if plan.fields else ""),
        f"  safety-flagged:    {plan.flagged_pages} page(s) name them and are left untouched "
        "(a flagged page's text is the safety record; docs/safety.md)",
        f"  prospect verdicts: {plan.sample_checks} {verb}removed",
        "  watchlist entry:   "
        + (
            f"{verb}removed (purpose {plan.watchlist.purpose}, "
            f"crawl {'on' if plan.watchlist.crawl else 'off'})"
            if plan.watchlist is not None
            else "none"
        ),
        f"  discovery trail:   {plan.candidate_links} candidate link(s) and "
        f"{plan.candidate_queries} recorded query sample(s) {verb}removed, "
        f"{plan.search_calls} search call(s) {verb}scrubbed of their query text (the row stays: "
        "it is the search spend ledger, ADR 0048)",
        "  kept: the pages, their URLs, titles, timestamps, media rows and PDQ hashes, and every "
        "other performer's links (docs/suppression.md)",
    ]
    if plan.owned_as_co_performer:
        lines.append(
            f"  customer originals: {plan.owned_as_co_performer} owned original(s) name them as a "
            "co-performer — left untouched (customer contract data); they are no crawl target "
            "either way"
        )
    if plan.owned_as_rights_holder:
        lines.append(
            f"  REFUSED: {plan.owned_as_rights_holder} owned original(s) name them as the rights "
            "holder; see docs/suppression.md"
        )
    return lines


def _fields(plan: SuppressionPlan) -> str:
    return ", ".join(f"{field} {count}" for field, count in plan.fields.items())
