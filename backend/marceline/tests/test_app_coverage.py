"""Coverage as a customer may learn it: a floor bucket, never the count, never the sites.

Operator decision (2026-09-16): the list of sites we crawl is not shown to customers, because it
tells a competitor exactly what we cover. What a customer gets is the rough size, so that
"nothing found" and "handled" can say what they are measured against.
"""

import json

import pytest
from django.contrib.auth.models import User

from accounts.models import Organisation
from catalog.models import Site
from marceline.app.coverage import bucket_for
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation


def make_sites(enabled: int, disabled: int = 0) -> None:
    Site.objects.bulk_create(
        [Site(domain=f"on-{index}.example", enabled=True) for index in range(enabled)]
        + [Site(domain=f"off-{index}.example", enabled=False) for index in range(disabled)]
    )


@pytest.mark.parametrize(
    ("count", "bucket"),
    [
        (0, "fewer_than_10"),
        (9, "fewer_than_10"),
        (10, "10_plus"),
        (17, "10_plus"),
        (49, "10_plus"),
        (50, "50_plus"),
        (99, "50_plus"),
        (100, "100_plus"),
        (250, "250_plus"),
        (500, "500_plus"),
        (999, "500_plus"),
        (1000, "1000_plus"),
        (25000, "1000_plus"),
    ],
)
def test_the_bucket_is_the_floor_of_the_count(count: int, bucket: str) -> None:
    assert bucket_for(count) == bucket


@pytest.fixture
def member(db: None) -> User:
    return add_member(make_organisation("Alpha Studio"), "alpha-owner")


def test_me_carries_the_bucket_of_enabled_sites_only(member: User) -> None:
    # Today's shape: 17 enabled sites is "10+". Disabled sites are not being watched.
    make_sites(enabled=17, disabled=40)

    response = client_for(member).get(f"{APP}/me")

    assert response.status_code == 200
    assert response.json()["monitored_sources"] == "10_plus"


def test_the_answer_names_no_site_and_no_count(member: User) -> None:
    make_sites(enabled=17, disabled=3)

    body = client_for(member).get(f"{APP}/me").content.decode()

    assert "example" not in body
    for number in ("17", "20", "3"):
        assert number not in json.dumps(json.loads(body)["monitored_sources"])
    assert set(json.loads(body)) == {
        "username",
        "organisation",
        "monitored_sources",
        "can_add_originals",
        # ADR 0044: whether this machine takes files at all. A capability of the machine, not a
        # number about anybody's data.
        "can_upload_media",
        # ADR 0045 amendment 2026-09-18: whether this machine answers the customer's own export.
        "can_export_data",
        # ADR 0055: whether this machine offers the account-control step at all.
        "can_verify_account",
        # ADR 0056: whether this machine takes the creator's own data requests.
        "can_request_data",
        # ADR 0054: whether this address has been confirmed, and whether asking again is possible
        # here. Facts about the account and this machine — neither gates anything.
        "email_verified",
        "can_verify_email",
    }


def test_every_account_gets_the_same_bucket(member: User) -> None:
    make_sites(enabled=55)
    other = add_member(make_organisation("Bravo Creator", Organisation.Segment.ENTERPRISE), "bravo")
    homeless = User.objects.create_user("no-organisation", password="secret-pass-123")

    answers = {
        client_for(user).get(f"{APP}/me").json()["monitored_sources"]
        for user in (member, other, homeless)
    }

    assert answers == {"50_plus"}
