"""Reassigning an original in the admin: deliberate, confirmed, twins included (ADR 0031)."""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from accounts.models import Organisation
from marceline.app.ownership import owned_references, owner_of
from marceline.models import OriginalOwnership
from marceline.tests.app_helpers import make_organisation, make_reference, own
from matching.models import Reference

pytestmark = pytest.mark.django_db

CHANGELIST = "/admin/marceline/originalownership/"


@pytest.fixture
def admin_client() -> Client:
    client = Client()
    client.force_login(User.objects.create_superuser("operator", password="secret-pass-123"))
    return client


@pytest.fixture
def alpha() -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def bravo() -> Organisation:
    return make_organisation("Bravo Creator")


def test_assigning_an_unassigned_original_needs_no_confirmation(
    admin_client: Client, alpha: Organisation
) -> None:
    imported = make_reference(None, title="Operator import")

    response = admin_client.post(
        f"{CHANGELIST}add/", {"reference": imported.pk, "organisation": alpha.pk}
    )

    assert response.status_code == 302, response.content
    row = OriginalOwnership.objects.get(pk=imported.pk)
    assert (row.organisation, row.assigned_via) == (alpha, "admin")
    assert row.assigned_by is not None
    assert row.assigned_by.get_username() == "operator"


def test_moving_an_original_away_from_an_organisation_needs_the_confirmation_tick(
    admin_client: Client, alpha: Organisation, bravo: Organisation
) -> None:
    v0 = make_reference(None, title="Alpha's original", organisation=alpha)
    v1 = own(
        Reference.objects.create(
            creator=v0.creator, title=v0.title, sha256=v0.sha256, frame_fps=2, pipeline="v1"
        ),
        alpha,
    )
    change = f"{CHANGELIST}{v0.pk}/change/"

    refused = admin_client.post(change, {"organisation": bravo.pk})

    assert refused.status_code == 200
    assert "Tick “Yes, move it” to confirm" in refused.content.decode()
    assert owner_of(v0.pk) == alpha

    confirmed = admin_client.post(change, {"organisation": bravo.pk, "confirm_move": "on"})

    assert confirmed.status_code == 302, confirmed.content
    assert list(owned_references(bravo).order_by("pk")) == [v0, v1]
    assert list(owned_references(alpha)) == []


def test_adding_a_row_that_would_take_a_twin_from_another_organisation_needs_confirmation(
    admin_client: Client, alpha: Organisation, bravo: Organisation
) -> None:
    v0 = make_reference(None, title="Alpha's original", organisation=alpha)
    v1 = Reference.objects.create(
        creator=v0.creator, title=v0.title, sha256=v0.sha256, frame_fps=2, pipeline="v1"
    )

    refused = admin_client.post(f"{CHANGELIST}add/", {"reference": v1.pk, "organisation": bravo.pk})

    assert refused.status_code == 200
    assert owner_of(v0.pk) == alpha
    assert owner_of(v1.pk) is None


def test_deleting_a_row_unassigns_the_file(admin_client: Client, alpha: Organisation) -> None:
    original = make_reference(None, title="Alpha's original", organisation=alpha)

    response = admin_client.post(f"{CHANGELIST}{original.pk}/delete/", {"post": "yes"})

    assert response.status_code == 302
    assert owner_of(original.pk) is None
    assert Reference.objects.filter(pk=original.pk).exists()
