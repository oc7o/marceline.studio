"""`seed_test_customer`: the documented, idempotent way to give the test account data to own.

The interesting assertions are the refusals. This command runs against a database holding the
operator's own evaluation material, so it must never move a user, never take an original from
another organisation, and never touch an original that was not named on the command line.
"""

from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import Membership, Organisation
from marceline.app.ownership import owner_of
from marceline.tests.app_helpers import add_member, make_organisation, make_reference

USERNAME = "test-customer@marceline.studio"


def run(*args: str) -> str:
    out = StringIO()
    call_command("seed_test_customer", *args, stdout=out)
    return out.getvalue()


def test_creates_the_account_its_organisation_and_the_membership(db: None) -> None:
    imported = make_reference(None, title="Operator import")

    run("--reference", str(imported.pk))

    user = User.objects.get(username=USERNAME)
    assert not user.is_staff
    assert not user.has_usable_password()
    membership = Membership.objects.select_related("organisation").get(user=user)
    assert membership.role == Membership.Role.OWNER
    assert membership.organisation.segment == Organisation.Segment.PROFESSIONAL
    assert owner_of(imported.pk) == membership.organisation
    imported.refresh_from_db()
    assert imported.uploaded_by_id is None, "nobody uploaded it; ownership is the column"


def test_running_it_twice_changes_nothing(db: None) -> None:
    imported = make_reference(None, title="Operator import")
    run("--reference", str(imported.pk))

    second = run("--reference", str(imported.pk))

    assert "already owned" in second
    assert Organisation.objects.count() == 1
    assert Membership.objects.count() == 1


def test_dry_run_writes_nothing(db: None) -> None:
    imported = make_reference(None, title="Operator import")

    output = run("--dry-run", "--reference", str(imported.pk))

    assert "rolled back" in output
    assert not Organisation.objects.exists()
    assert not User.objects.filter(username=USERNAME).exists()
    assert owner_of(imported.pk) is None


def test_it_refuses_to_take_an_original_from_another_organisation(db: None) -> None:
    other = add_member(make_organisation("Bravo Creator"), "bravo-owner")
    theirs = make_reference(other, title="Bravo's original")

    with pytest.raises(CommandError, match="refusing to reassign"):
        run("--reference", str(theirs.pk))

    assert owner_of(theirs.pk) == other.memberships.get().organisation
    # The whole run is one transaction, so the refusal leaves no half-made account behind.
    assert not User.objects.filter(username=USERNAME).exists()


def test_it_refuses_to_move_a_user_between_organisations(db: None) -> None:
    elsewhere = make_organisation("Somewhere Else")
    user = User.objects.create_user(USERNAME)
    Membership.objects.create(organisation=elsewhere, user=user)

    with pytest.raises(CommandError, match="refusing to move"):
        run()

    assert Membership.objects.get(user=user).organisation == elsewhere


def test_it_touches_no_original_that_was_not_named(db: None) -> None:
    named = make_reference(None, title="Named on the command line")
    untouched = make_reference(None, title="The operator's evaluation material")

    run("--reference", str(named.pk))

    assert owner_of(untouched.pk) is None


def test_an_unknown_original_id_is_an_error(db: None) -> None:
    with pytest.raises(CommandError, match="no original with id"):
        run("--reference", "999999")
