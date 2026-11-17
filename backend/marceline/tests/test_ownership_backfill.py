"""The backfill of `marceline/migrations/0002` (issue #76): today's rule, written down once.

The migration's function is imported and run against the current models — it is plain ORM over
fields the historical models share — so this pins exactly which originals `make migrate` assigns.
"""

from importlib import import_module

from django.contrib.auth.models import User

from accounts.models import Membership
from catalog.models import Creator
from marceline.models import OriginalOwnership
from marceline.tests.app_helpers import add_member, make_organisation, own
from matching.models import Reference

migration = import_module("marceline.migrations.0002_backfill_original_ownership")


def unowned_reference(uploader: User | None, title: str) -> Reference:
    """A reference as it stood before the column: an uploader, no ownership row."""
    return Reference.objects.create(
        creator=Creator.objects.get_or_create(name="Synthetic Artist")[0],
        title=title,
        sha256=title.ljust(64, "0"),
        frame_fps=1,
        uploaded_by=uploader,
    )


def run() -> dict[str, int]:
    counts: dict[str, int] = migration.backfill(Reference, Membership, OriginalOwnership)
    return counts


def owners() -> dict[int, int]:
    return dict(OriginalOwnership.objects.values_list("reference_id", "organisation_id"))


def test_backfill_assigns_what_the_uploaders_membership_said_and_nothing_else(db: None) -> None:
    test_customer = make_organisation("Test Customer")
    bravo = make_organisation("Bravo Creator")
    customer = add_member(test_customer, "test-customer@marceline.studio")
    reviewer = add_member(bravo, "reviewer", is_staff=True)
    loner = User.objects.create_user("no-membership")

    v1 = unowned_reference(customer, "v1")
    v0_twin = unowned_reference(customer, "v0")
    by_reviewer = unowned_reference(reviewer, "reviewer")
    operator_import = unowned_reference(None, "operator")
    by_loner = unowned_reference(loner, "loner")
    already = own(unowned_reference(customer, "already"), bravo)

    counts = run()

    assert counts == {
        "assigned": 3,
        "already_owned": 1,
        "no_uploader": 1,
        "uploader_without_membership": 1,
    }
    assert owners() == {
        v1.pk: test_customer.pk,
        v0_twin.pk: test_customer.pk,
        by_reviewer.pk: bravo.pk,
        already.pk: bravo.pk,  # a deliberate assignment is never overwritten
    }
    assert operator_import.pk not in owners()
    assert by_loner.pk not in owners()
    backfilled = OriginalOwnership.objects.filter(assigned_via=migration.VIA)
    assert set(backfilled.values_list("reference_id", flat=True)) == {
        v1.pk,
        v0_twin.pk,
        by_reviewer.pk,
    }


def test_backfill_is_idempotent(db: None) -> None:
    organisation = make_organisation("Test Customer")
    unowned_reference(add_member(organisation, "customer"), "v1")
    run()
    before = sorted(owners().items())

    counts = run()

    assert counts == {
        "assigned": 0,
        "already_owned": 1,
        "no_uploader": 0,
        "uploader_without_membership": 0,
    }
    assert sorted(owners().items()) == before
