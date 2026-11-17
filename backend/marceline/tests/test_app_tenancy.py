"""The tenancy guarantee, from the negative side first.

One organisation must never learn anything about another's originals — including whether they
exist. Every refusal here is a **404**: a 403 would answer "yes, that row is real, it is just not
yours", which is precisely the fact worth hiding. These tests are the point of the change; if one
of them starts passing for the wrong reason (say, because a view stopped scoping its queryset and
the id simply does not exist in that test), the last test in this file catches it by asserting
that the owner *can* read the very same original.
"""

from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from accounts.models import Membership, Organisation
from index.models import Page
from marceline.tests.app_helpers import (
    APP,
    UNKNOWN_ORIGINAL,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.models import Reference, ReferenceLeak

# Every read-only customer endpoint that answers about one organisation's own data.
LISTS = ("originals", "findings", "cases", "removals", "invoices")


@pytest.fixture
def organisation_a(db: None) -> Organisation:
    return make_organisation("Alpha Studio", Organisation.Segment.ENTERPRISE)


@pytest.fixture
def organisation_b(db: None) -> Organisation:
    return make_organisation("Bravo Creator")


@pytest.fixture
def owner_a(organisation_a: Organisation) -> User:
    return add_member(organisation_a, "alpha-owner")


@pytest.fixture
def owner_b(organisation_b: Organisation) -> User:
    return add_member(organisation_b, "bravo-owner")


@pytest.fixture
def original_a(owner_a: User) -> Reference:
    return make_reference(owner_a, title="Alpha's own original")


@pytest.fixture
def finding_a(original_a: Reference, make_page: Callable[[str], Page]) -> Page:
    """A confirmed finding of Alpha's that has come down: it appears in every list Alpha has."""
    page = make_page("alpha-leak")
    page.availability = Page.Availability.REMOVED
    page.availability_checked_at = timezone.now()
    page.save(update_fields=["availability", "availability_checked_at"])
    ReferenceLeak.objects.create(reference=original_a, page=page, status="confirmed")
    return page


def test_another_organisations_original_is_404_never_403(
    owner_b: User, original_a: Reference
) -> None:
    response = client_for(owner_b).get(f"{APP}/originals/{public_id_of(original_a)}")

    assert response.status_code == 404
    assert response.status_code != 403, "403 would confirm the original exists"
    assert original_a.title not in response.content.decode()


def test_a_foreign_original_is_indistinguishable_from_a_missing_one(
    owner_b: User, original_a: Reference
) -> None:
    client = client_for(owner_b)

    foreign = client.get(f"{APP}/originals/{public_id_of(original_a)}")
    missing = client.get(f"{APP}/originals/{UNKNOWN_ORIGINAL}")

    assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())


def test_another_organisations_original_is_not_in_the_list(
    owner_b: User, original_a: Reference
) -> None:
    response = client_for(owner_b).get(f"{APP}/originals")

    assert response.status_code == 200
    assert response.json() == []


def test_anonymous_requests_get_nothing(client: Client, original_a: Reference) -> None:
    paths = (
        f"{APP}/me",
        *(f"{APP}/{name}" for name in LISTS),
        f"{APP}/originals/{public_id_of(original_a)}",
        f"{APP}/cases/{public_id_of(original_a)}",
    )
    for path in paths:
        response = client.get(path)

        assert response.status_code == 401
        assert original_a.title not in response.content.decode()


def test_staff_are_not_customers(staff_client: Client, original_a: Reference) -> None:
    """`is_staff` opens the review API, not somebody else's account."""
    assert staff_client.get(f"{APP}/originals/{public_id_of(original_a)}").status_code == 404
    assert staff_client.get(f"{APP}/cases/{public_id_of(original_a)}").status_code == 404
    for name in LISTS:
        assert staff_client.get(f"{APP}/{name}").json() == []
    assert staff_client.get(f"{APP}/me").json()["organisation"] is None
    # The same staff session reads the same original through the review API, which is its job.
    assert staff_client.get(f"/api/v1/review/originals/{original_a.pk}").status_code == 200


def test_an_unclaimed_original_belongs_to_nobody(owner_a: User, db: None) -> None:
    """Unassigned operator/CLI imports are never auto-claimed by anyone."""
    imported = make_reference(None, title="Operator import")

    # It has no public id at all; its primary key is no way in either.
    response = client_for(owner_a).get(f"{APP}/originals/{imported.pk}")

    assert response.status_code == 404
    assert client_for(owner_a).get(f"{APP}/originals").json() == []


def test_a_user_without_an_organisation_owns_nothing(
    user_client: Client, original_a: Reference
) -> None:
    # The coverage bucket is the same for every account and says nothing about anybody's data.
    assert user_client.get(f"{APP}/me").json() == {
        "username": "customer",
        "organisation": None,
        "monitored_sources": "fewer_than_10",
        "can_add_originals": False,
        # ADR 0044: no organisation, so no upload either — and the switch is off anyway.
        "can_upload_media": False,
        # ADR 0045 amendment 2026-09-18: no organisation, so nothing to export either.
        "can_export_data": False,
        # ADR 0055: no organisation, so no account-control step either (and the switch is off).
        "can_verify_account": False,
        # ADR 0056: no organisation, so nothing to ask about (and the switch is off).
        "can_request_data": False,
        # ADR 0054: unconfirmed, and no organisation to ask for a confirmation for. Neither field
        # gates anything — they are facts the app renders a banner from.
        "email_verified": False,
        "can_verify_email": False,
    }
    assert user_client.get(f"{APP}/originals").json() == []
    assert user_client.get(f"{APP}/originals/{public_id_of(original_a)}").status_code == 404


def test_a_colleague_in_the_same_organisation_does_see_it(
    organisation_a: Organisation, owner_a: User, original_a: Reference
) -> None:
    """The counterweight: the 404s above are scope, not a broken lookup."""
    colleague = add_member(organisation_a, "alpha-editor", role=Membership.Role.MEMBER)

    response = client_for(colleague).get(f"{APP}/originals/{public_id_of(original_a)}")

    assert response.status_code == 200
    assert response.json()["original"]["title"] == "Alpha's own original"
    assert [row["id"] for row in client_for(colleague).get(f"{APP}/originals").json()] == [
        public_id_of(original_a)
    ]


def test_another_organisations_case_is_404_never_403(
    owner_b: User, original_a: Reference, finding_a: Page
) -> None:
    response = client_for(owner_b).get(f"{APP}/cases/{public_id_of(original_a)}")

    assert response.status_code == 404
    assert response.status_code != 403, "403 would confirm the case exists"
    assert original_a.title not in response.content.decode()
    assert finding_a.url not in response.content.decode()


def test_a_foreign_case_is_indistinguishable_from_a_missing_one(
    owner_b: User, original_a: Reference, finding_a: Page
) -> None:
    """Byte-identical, not merely the same status: the body is part of the answer."""
    client = client_for(owner_b)

    foreign = client.get(f"{APP}/cases/{public_id_of(original_a)}")
    missing = client.get(f"{APP}/cases/{UNKNOWN_ORIGINAL}")

    assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())
    assert foreign.content == missing.content


def test_another_organisations_findings_cases_and_removals_are_not_in_the_lists(
    owner_b: User, original_a: Reference, finding_a: Page
) -> None:
    client = client_for(owner_b)

    for name in LISTS:
        response = client.get(f"{APP}/{name}")

        assert (response.status_code, response.json()) == (200, [])
        assert finding_a.url not in response.content.decode()


def test_the_owner_does_see_that_very_case(
    owner_a: User, original_a: Reference, finding_a: Page
) -> None:
    """The counterweight: the 404s and empty lists above are scope, not a broken lookup."""
    client = client_for(owner_a)

    assert [row["original_id"] for row in client.get(f"{APP}/cases").json()] == [
        public_id_of(original_a)
    ]
    assert [row["url"] for row in client.get(f"{APP}/findings").json()] == [finding_a.url]
    assert [row["url"] for row in client.get(f"{APP}/removals").json()] == [finding_a.url]
    assert (
        client.get(f"{APP}/cases/{public_id_of(original_a)}").json()["case"]["state"]
        == "removed_watching"
    )


def test_me_describes_the_organisation_not_the_account_holder(
    organisation_a: Organisation, owner_a: User
) -> None:
    body = client_for(owner_a).get(f"{APP}/me").json()

    assert body["username"] == "alpha-owner"
    assert body["organisation"]["id"] == organisation_a.public_id
    assert body["organisation"]["segment"] == "enterprise"
    assert body["organisation"]["role"] == "owner"


def test_a_member_moved_to_another_organisation_leaves_the_originals_behind(
    organisation_a: Organisation,
    owner_a: User,
    owner_b: User,
    original_a: Reference,
    finding_a: Page,
) -> None:
    """Issue #76: the uploader's current membership decides nothing about an original."""
    Membership.objects.filter(user=owner_a).update(
        organisation=owner_b.memberships.get().organisation
    )
    moved = client_for(owner_a)

    foreign = moved.get(f"{APP}/cases/{public_id_of(original_a)}")
    missing = moved.get(f"{APP}/cases/{UNKNOWN_ORIGINAL}")
    assert foreign.status_code == 404
    assert foreign.content == missing.content
    for name in LISTS:
        assert moved.get(f"{APP}/{name}").json() == []

    # The organisation keeps its original and its case, whoever is left to read them.
    colleague = add_member(organisation_a, "alpha-editor", role=Membership.Role.MEMBER)
    assert [row["original_id"] for row in client_for(colleague).get(f"{APP}/cases").json()] == [
        public_id_of(original_a)
    ]


def test_a_staff_uploader_with_a_membership_does_not_see_what_it_uploaded_for_a_customer(
    organisation_a: Organisation, organisation_b: Organisation
) -> None:
    """Review finding M2: `uploaded_by` is audit data, not a key into anybody's account."""
    reviewer = add_member(organisation_b, "reviewer", is_staff=True)
    for_alpha = make_reference(
        reviewer, title="Uploaded by a reviewer for Alpha", organisation=organisation_a
    )
    client = client_for(reviewer)

    foreign = client.get(f"{APP}/originals/{public_id_of(for_alpha)}")
    missing = client.get(f"{APP}/originals/{UNKNOWN_ORIGINAL}")
    assert foreign.status_code == 404
    assert foreign.content == missing.content
    assert client.get(f"{APP}/originals").json() == []
