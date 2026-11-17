"""Set up the test customer account: an organisation, a membership, and a few owned originals.

Idempotent — run it as often as you like. It creates only what is missing and it is deliberately
timid about other people's data:

- it never moves a user from one organisation to another;
- it **never takes an original away from another organisation**: a reference is assigned only when
  it is named explicitly with `--reference` and nobody owns it yet (moving one is
  `assign_originals`);
- without `--reference` it touches no originals at all. Which of the operator's 25 imported
  references a test account owns is an operator decision, not a default in code (the customer UI
  plan lists it as an open question), so the ids are always typed out:

      manage.py seed_test_customer --reference 14 --reference 15 --dry-run
      manage.py seed_test_customer --reference 14 --reference 15

Ownership is the organisation column (`marceline/app/ownership.py`, ADR 0031), so assigning the
original to the test customer's organisation is the whole of "give this to the test customer".
`uploaded_by` is left alone: nobody uploaded these.
"""

from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from accounts.business_status import check_before_access
from accounts.models import Membership, Organisation
from marceline.app.ownership import Via, owner_of, register
from matching.models import Reference

DEFAULT_USERNAME = "test-customer@marceline.studio"
DEFAULT_ORGANISATION = "Test Customer"


class Command(BaseCommand):
    help = "Put the test customer into an organisation and assign it named originals (idempotent)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--user", default=DEFAULT_USERNAME, help="Account username.")
        parser.add_argument("--organisation", default=DEFAULT_ORGANISATION)
        parser.add_argument(
            "--segment",
            choices=[choice.value for choice in Organisation.Segment],
            default=Organisation.Segment.PROFESSIONAL.value,
            help="Only applied when the organisation is created.",
        )
        parser.add_argument(
            "--reference",
            dest="references",
            type=int,
            action="append",
            help="Id of an original to assign; repeatable. Only unassigned originals are taken.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Roll everything back.")

    def handle(self, *args: Any, **options: Any) -> None:
        with transaction.atomic():
            user = self.account(options["user"])
            organisation = self.organisation(options["organisation"], options["segment"])
            self.membership(user, organisation)
            for reference_id in options["references"] or []:
                self.assign(reference_id, organisation)
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write("dry run: rolled back, nothing written")

    def account(self, username: str) -> User:
        user = User.objects.filter(username=username).first()
        if user is None:
            # No usable password: the operator sets one with `manage.py changepassword`.
            user = User.objects.create_user(
                username=username, email=username if "@" in username else ""
            )
            self.stdout.write(f"created user {username} (no usable password)")
        else:
            self.stdout.write(f"user {username} exists")
        if user.is_staff:
            self.stdout.write(
                self.style.WARNING(
                    f"{username} is staff; a reviewer account should not also be a tenant"
                )
            )
        return user

    def organisation(self, name: str, segment: str) -> Organisation:
        existing = Organisation.objects.filter(name__iexact=name).first()
        if existing is None:
            created = Organisation.objects.create(name=name, segment=segment)
            self.stdout.write(f"created organisation {created.pk}: {name} ({segment})")
            return created
        if existing.segment != segment:
            self.stdout.write(
                self.style.WARNING(
                    f"organisation {existing.pk} is {existing.segment}, not {segment}; kept as is"
                )
            )
        self.stdout.write(f"organisation {existing.pk} exists: {existing.name}")
        return existing

    def membership(self, user: User, organisation: Organisation) -> Membership:
        existing = Membership.objects.select_related("organisation").filter(user=user).first()
        if existing is not None and existing.organisation_id != organisation.pk:
            raise CommandError(
                f"{user.get_username()} already belongs to organisation "
                f"{existing.organisation_id} ({existing.organisation.name}); refusing to move them"
            )
        if existing is not None:
            self.stdout.write(f"membership exists: {user.get_username()} ({existing.role})")
            return existing
        created = Membership.objects.create(
            organisation=organisation, user=user, role=Membership.Role.OWNER
        )
        self.stdout.write(f"added {user.get_username()} to {organisation.name} as owner")
        # Issue #92: a membership is the grant of customer access, so the *Unternehmer* evidence
        # belongs on file by now. This command is the development test path, never a contracting
        # party, so it always only says so — `enforce=False` keeps it working whatever
        # `ONBOARDING_BUSINESS_STATUS_GUARD` is set to.
        message = check_before_access(organisation, enforce=False)
        if message is not None:
            self.stdout.write(self.style.WARNING(message))
        return created

    def assign(self, reference_id: int, organisation: Organisation) -> None:
        reference = Reference.objects.filter(pk=reference_id).first()
        if reference is None:
            raise CommandError(f"no original with id {reference_id}")
        owner = owner_of(reference.pk)
        if owner is not None and owner.pk == organisation.pk:
            self.stdout.write(f"original {reference_id} already owned: {reference.title}")
            return
        if owner is not None:
            raise CommandError(
                f"original {reference_id} is owned by organisation {owner.pk}; "
                "refusing to reassign it (assign_originals moves it deliberately)"
            )
        register(reference, organisation, via=Via.COMMAND)
        self.stdout.write(f"assigned original {reference_id} to {organisation.name}")
