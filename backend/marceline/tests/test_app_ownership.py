"""`marceline.app.ownership` is the single definition of what an organisation owns."""

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.db import transaction
from django.db.models import ProtectedError

from accounts.models import Membership, Organisation
from marceline.app.ownership import (
    Via,
    inherited_owner,
    membership_of,
    organisation_of,
    owned_reference,
    owned_references,
    owner_of,
    reassign,
    register,
    with_twins,
)
from marceline.tests.app_helpers import add_member, make_organisation, make_reference, own
from matching.models import Reference


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def owner(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


def test_anonymous_and_unaffiliated_users_have_no_organisation(db: None) -> None:
    stranger = User.objects.create_user("stranger")

    assert organisation_of(AnonymousUser()) is None
    assert membership_of(AnonymousUser()) is None
    assert organisation_of(stranger) is None
    assert list(owned_references(None)) == []


def test_ownership_is_the_organisation_column(organisation: Organisation, owner: User) -> None:
    mine = make_reference(owner)

    assert organisation_of(owner) == organisation
    assert list(owned_references(organisation)) == [mine]
    assert owned_reference(organisation, mine.pk) == mine
    assert owner_of(mine.pk) == organisation


def test_unclaimed_and_foreign_originals_are_not_owned(
    organisation: Organisation, owner: User
) -> None:
    imported = make_reference(None, title="Operator import")
    other = add_member(make_organisation("Bravo Creator"), "bravo-owner")
    theirs = make_reference(other, title="Bravo's original")

    owned = list(owned_references(organisation))

    assert owned == []
    assert owned_reference(organisation, imported.pk) is None
    assert owned_reference(organisation, theirs.pk) is None
    assert owner_of(imported.pk) is None


def test_every_member_sees_the_organisations_originals_exactly_once(
    organisation: Organisation, owner: User
) -> None:
    colleague = add_member(organisation, "alpha-editor", role=Membership.Role.MEMBER)
    first = make_reference(owner, title="One")
    second = make_reference(colleague, title="Two")

    owned = list(owned_references(organisation).order_by("pk"))

    assert owned == [first, second]


def test_moving_a_member_moves_none_of_their_uploads(
    organisation: Organisation, owner: User
) -> None:
    """Issue #76: ownership follows the original, never the uploader's current membership."""
    uploaded = make_reference(owner, title="Uploaded while at Alpha")
    bravo = make_organisation("Bravo Creator")

    Membership.objects.filter(user=owner).update(organisation=bravo)

    assert list(owned_references(organisation)) == [uploaded]
    assert list(owned_references(bravo)) == []


def test_a_member_who_leaves_or_is_deleted_takes_nothing_with_them(
    organisation: Organisation, owner: User
) -> None:
    uploaded = make_reference(owner, title="Uploaded by a leaver")

    Membership.objects.filter(user=owner).delete()
    assert list(owned_references(organisation)) == [uploaded]

    owner.delete()
    uploaded.refresh_from_db()
    assert uploaded.uploaded_by_id is None
    assert list(owned_references(organisation)) == [uploaded]


def test_a_staff_uploader_with_a_membership_sees_only_its_own_organisation(
    organisation: Organisation,
) -> None:
    """Review finding M2: a reviewer who uploads for a customer must not own the upload."""
    reviewer = add_member(organisation, "reviewer-with-membership", is_staff=True)
    customer = make_organisation("Bravo Creator")
    for_customer = make_reference(reviewer, title="Uploaded for Bravo", organisation=customer)

    assert list(owned_references(organisation_of(reviewer))) == []
    assert list(owned_references(customer)) == [for_customer]


def test_an_organisation_that_owns_originals_cannot_be_deleted(
    organisation: Organisation, owner: User
) -> None:
    make_reference(owner)

    with pytest.raises(ProtectedError), transaction.atomic():
        organisation.delete()


def test_register_records_the_owner_once_and_refuses_to_overwrite(
    organisation: Organisation,
) -> None:
    imported = make_reference(None)
    register(imported, None, via=Via.IMPORT)
    assert owner_of(imported.pk) is None

    register(imported, organisation, via=Via.IMPORT)
    assert owner_of(imported.pk) == organisation

    with pytest.raises(ValueError, match="already has an owner"):
        register(imported, make_organisation("Bravo Creator"), via=Via.IMPORT)
    assert owner_of(imported.pk) == organisation


def twin(reference: Reference, pipeline: str) -> Reference:
    """The same file fingerprinted again under another recipe (issue #70)."""
    return Reference.objects.create(
        creator=reference.creator,
        title=reference.title,
        sha256=reference.sha256,
        frame_fps=1,
        pipeline=pipeline,
    )


def test_an_owner_is_inherited_from_earlier_references_of_the_same_file(
    organisation: Organisation, owner: User
) -> None:
    v0 = make_reference(owner)
    unassigned = make_reference(None)

    inherited = inherited_owner(v0.sha256)

    assert inherited == (organisation, v0)
    assert inherited_owner(unassigned.sha256) is None
    assert inherited_owner("f" * 64) is None


def test_twins_owned_by_different_organisations_are_an_operator_decision(
    organisation: Organisation, owner: User
) -> None:
    v0 = make_reference(owner)
    own(twin(v0, "ffmpeg-rgb512-pdq-v1"), make_organisation("Bravo Creator"))

    with pytest.raises(ValueError, match="different organisations"):
        inherited_owner(v0.sha256)


def test_reassign_moves_clears_and_reports_unchanged_rows(
    organisation: Organisation, owner: User
) -> None:
    bravo = make_organisation("Bravo Creator")
    alpha_original = make_reference(owner, title="Alpha")
    unassigned = make_reference(None, title="Import")

    dry = reassign([alpha_original, unassigned], bravo, via=Via.COMMAND, dry_run=True)
    assert [(c.reference_id, c.before, c.after, c.changed) for c in dry] == [
        (alpha_original.pk, organisation, bravo, True),
        (unassigned.pk, None, bravo, True),
    ]
    assert owner_of(alpha_original.pk) == organisation
    assert owner_of(unassigned.pk) is None

    reassign([alpha_original, unassigned], bravo, via=Via.COMMAND)
    assert list(owned_references(bravo).order_by("pk")) == [alpha_original, unassigned]
    assert list(owned_references(organisation)) == []

    again = reassign([alpha_original], bravo, via=Via.COMMAND)
    assert [change.changed for change in again] == [False]

    reassign([alpha_original], None, via=Via.COMMAND)
    assert owner_of(alpha_original.pk) is None
    assert list(owned_references(bravo)) == [unassigned]


def test_with_twins_adds_every_reference_of_the_same_file(owner: User) -> None:
    v0 = make_reference(owner)
    v1 = twin(v0, "ffmpeg-rgb512-pdq-v1")
    other = make_reference(owner)

    assert with_twins([v1.pk]) == [v0, v1]
    assert with_twins([other.pk, v0.pk]) == [v0, v1, other]
