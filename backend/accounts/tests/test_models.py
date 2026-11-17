"""Constraints belong in the database, not only in Python (backend/CLAUDE.md)."""

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from accounts.models import Membership, Organisation


@pytest.fixture
def organisation(db: None) -> Organisation:
    return Organisation.objects.create(name="Alpha Studio")


def test_professional_is_the_default_segment(organisation: Organisation) -> None:
    assert organisation.segment == Organisation.Segment.PROFESSIONAL
    assert str(organisation) == "Alpha Studio (professional)"


def test_organisation_names_are_unique_case_insensitively(organisation: Organisation) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Organisation.objects.create(name="alpha studio", segment=Organisation.Segment.ENTERPRISE)


def test_a_user_belongs_to_at_most_one_organisation(organisation: Organisation) -> None:
    """Phase 1 answers "which organisation is this request scoped to" with exactly one row."""
    user = User.objects.create_user("alpha-owner")
    Membership.objects.create(organisation=organisation, user=user)
    other = Organisation.objects.create(name="Bravo Creator")

    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(organisation=other, user=user, role=Membership.Role.MEMBER)


def test_owner_is_the_default_role(organisation: Organisation) -> None:
    membership = Membership.objects.create(
        organisation=organisation, user=User.objects.create_user("alpha-owner")
    )

    assert membership.role == Membership.Role.OWNER
