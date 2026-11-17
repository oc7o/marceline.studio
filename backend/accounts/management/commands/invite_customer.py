"""Issue a customer invite and print its link (issue #115, ADR 0052).

**This command sends nothing.** The project has no customer mail transport, so v1 prints the link
and the operator pastes it into the message he is writing to that creator anyway. The seam for mailing later is `accounts/invites.py`; there is deliberately
no flag for it, because a switch that turns on an unwritten transport is worse than no switch.

The link is printed **once**: the row keeps only a SHA-256 of the token, so a lost link is reissued,
never recovered.
"""

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts import invites, onboarding
from accounts.models import CustomerInvite, Organisation


class Command(BaseCommand):
    help = "Issue a single-use invite for a creator and print the link (ADR 0052)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", required=True, help="The address the invite goes to.")
        destination = parser.add_mutually_exclusive_group(required=True)
        destination.add_argument(
            "--organisation", help="Join this existing organisation (by exact name)."
        )
        destination.add_argument(
            "--new-organisation", help="Create an organisation with this name on redemption."
        )
        parser.add_argument(
            "--segment",
            choices=[choice.value for choice in Organisation.Segment],
            default=Organisation.Segment.PROFESSIONAL.value,
            help="Only used with --new-organisation. Billing and onboarding only.",
        )
        parser.add_argument(
            "--expires-in-days",
            type=int,
            default=None,
            help="Overrides ONBOARDING_INVITE_EXPIRY_DAYS for this one invite.",
        )
        parser.add_argument(
            "--note",
            default="",
            help="Why this invite exists, for staff. No contact details beyond the address.",
        )
        parser.add_argument(
            "--revoke",
            type=int,
            default=None,
            metavar="ID",
            help="Revoke this invite instead of issuing one. Its link then answers the neutral "
            "page, like every other token that does not work.",
        )
        parser.add_argument("--list", action="store_true", help="Counts only: open/redeemed/…")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["list"]:
            for name, count in invites.counts().items():
                self.stdout.write(f"{name:>10}: {count}")
            return

        if options["revoke"] is not None:
            invite = CustomerInvite.objects.filter(pk=options["revoke"]).first()
            if invite is None:
                raise CommandError(f"no invite {options['revoke']}")
            invites.revoke(invite, reason="revoked with manage.py invite_customer")
            self.stdout.write(self.style.SUCCESS(f"invite {invite.pk} revoked"))
            return

        if not onboarding.invites_enabled():
            raise CommandError(
                "ONBOARDING_INVITES_ENABLED is off (issue #115, ADR 0052). With it off every "
                "/api/v1/app/invite route answers the same neutral 404, so an issued link would "
                "not work; set it in .env and recreate `web` (a container keeps the environment "
                "it was created with)."
            )

        organisation = None
        if options["organisation"]:
            organisation = Organisation.objects.filter(name=options["organisation"]).first()
            if organisation is None:
                raise CommandError(f"no organisation named {options['organisation']!r}")

        expires_at = None
        if options["expires_in_days"] is not None:
            expires_at = timezone.now() + timedelta(days=max(int(options["expires_in_days"]), 1))

        invite, token = invites.issue(
            email=options["email"],
            organisation=organisation,
            organisation_name=options["new_organisation"] or "",
            segment=options["segment"],
            note=options["note"],
            expires_at=expires_at,
        )

        self.stdout.write(f"invite {invite.pk} -> {invite.destination}")
        self.stdout.write(f"expires {invite.expires_at:%Y-%m-%d %H:%M} UTC")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(invites.invite_link(token)))
        self.stdout.write("")
        self.stdout.write(
            "Shown once — only a SHA-256 of the token is stored. Send it yourself; nothing here "
            "mails anybody."
        )
        if not onboarding.documents_available():
            self.stdout.write(
                self.style.WARNING(
                    "ONBOARDING_DOCUMENTS names no published pilot agreement and service privacy "
                    "notice, so this invite cannot be completed yet: the accept step refuses "
                    "(ADR 0052 §3). Do not fill that setting with a draft."
                )
            )
        if not settings.ONBOARDING_INVITE_BASE_URL:
            self.stdout.write(
                self.style.WARNING(
                    "ONBOARDING_INVITE_BASE_URL is empty, so the link above has no origin."
                )
            )
