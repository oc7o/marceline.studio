"""The customer's own data export (ADR 0045 amendment 2026-09-18, issue #85):
`GET /api/v1/app/export`.

Own organisation only, rate-limited, dev-only, and stamped/logged rather than stored.
"""

import hashlib
import json
import logging

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.app import upload_throttle
from marceline.deletion import services
from marceline.deletion.models import OrganisationEnding
from marceline.models import UploadAttempt
from marceline.tests.app_helpers import (
    APP,
    add_member,
    client_for,
    make_organisation,
    make_reference,
)
from matching.models import Reference, ReferenceFrameHash


@pytest.fixture
def alpha(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def owner_a(alpha: Organisation) -> User:
    return add_member(alpha, "alpha-owner")


@pytest.fixture
def reference_a(owner_a: User, alpha: Organisation) -> Reference:
    reference = make_reference(owner_a, title="Alpha original")
    ReferenceFrameHash.objects.create(
        reference=reference, frame_index=0, timestamp_s=0.0, pdq=bytes([1]) * 32, quality=100
    )
    return reference


@pytest.fixture
def owner_b(db: None) -> User:
    return add_member(make_organisation("Bravo Creator"), "bravo-owner")


def test_the_owner_downloads_their_own_export(
    owner_a: User, alpha: Organisation, reference_a: Reference
) -> None:
    response = client_for(owner_a).get(f"{APP}/export")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert response["Content-Disposition"].startswith('attachment; filename="marceline-export-')
    assert response["Content-Disposition"].endswith('.json"')
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in response

    document = json.loads(response.content)
    assert document["format"] == "marceline.customer-export"
    assert document["organisation"]["id"] == alpha.public_id
    [entry] = document["fingerprints"]["originals"]
    assert entry["sha256"] == reference_a.sha256


def test_another_organisations_data_never_appears(
    owner_a: User, owner_b: User, reference_a: Reference
) -> None:
    other = make_reference(owner_b, title="Bravo original")
    ReferenceFrameHash.objects.create(
        reference=other, frame_index=0, timestamp_s=0.0, pdq=bytes([2]) * 32, quality=100
    )

    document = json.loads(client_for(owner_a).get(f"{APP}/export").content)

    sha256s = {entry["sha256"] for entry in document["fingerprints"]["originals"]}
    assert sha256s == {reference_a.sha256}
    assert other.sha256 not in sha256s


def test_no_staff_only_field_leaks_into_the_export(owner_a: User, reference_a: Reference) -> None:
    """The export is `export_document`, and that in turn is `marceline.export.render` — the same
    customer views. No confidence, band, distance or frame count ever reaches a customer schema
    (`backend/marceline/CLAUDE.md`)."""
    body = client_for(owner_a).get(f"{APP}/export").content.decode()

    for banned in ("confidence", '"band"', "distance", "evidence_frame_count"):
        assert banned not in body, banned


def test_staff_and_accounts_without_organisation_get_404(
    staff_client: Client, user_client: Client
) -> None:
    for client in (staff_client, user_client):
        response = client.get(f"{APP}/export")
        assert response.status_code == 404
        assert response.json()["detail"] == "Organisation not found"


def test_anonymous_requests_are_refused(client: Client) -> None:
    assert client.get(f"{APP}/export").status_code == 401


def test_a_second_export_within_the_hour_is_rate_limited(
    owner_a: User, alpha: Organisation, reference_a: Reference
) -> None:
    first = client_for(owner_a).get(f"{APP}/export")
    second = client_for(owner_a).get(f"{APP}/export")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "too_many_requests"
    assert 3000 <= int(second["Retry-After"]) <= 3600
    assert (
        UploadAttempt.objects.filter(organisation=alpha, kind=upload_throttle.Kind.EXPORT).count()
        == 1
    )


def test_another_organisation_has_its_own_budget(
    owner_a: User, owner_b: User, reference_a: Reference
) -> None:
    assert client_for(owner_a).get(f"{APP}/export").status_code == 200
    assert client_for(owner_b).get(f"{APP}/export").status_code == 200


def test_production_does_not_answer_the_export(
    settings: Settings, owner_a: User, reference_a: Reference
) -> None:
    settings.MARCELINE_ROLE = "prod"
    response = client_for(owner_a).get(f"{APP}/export")
    assert response.status_code == 404
    assert reference_a.sha256.encode() not in response.content


def test_exporting_stamps_a_pending_ending(
    owner_a: User, alpha: Organisation, reference_a: Reference
) -> None:
    ending, _ = services.end_pilot(alpha, ended_at=timezone.now())
    assert ending.exported_at is None

    client_for(owner_a).get(f"{APP}/export")

    ending.refresh_from_db()
    assert ending.exported_at is not None


def test_exporting_with_no_pending_ending_creates_no_mark(
    owner_a: User, reference_a: Reference
) -> None:
    client_for(owner_a).get(f"{APP}/export")

    assert not OrganisationEnding.objects.exists()


def test_the_export_is_logged_without_storing_the_document(
    owner_a: User, alpha: Organisation, reference_a: Reference, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="marceline.app.export"):
        response = client_for(owner_a).get(f"{APP}/export")

    digest = hashlib.sha256(response.content).hexdigest()
    [record] = [r for r in caplog.records if r.name == "marceline.app.export"]
    message = record.getMessage()
    assert str(alpha.pk) in message
    assert str(len(response.content)) in message
    assert digest in message


def test_me_reports_export_availability(owner_a: User, settings: Settings) -> None:
    client = client_for(owner_a)

    assert client.get(f"{APP}/me").json()["can_export_data"] is True

    settings.MARCELINE_ROLE = "prod"
    assert client.get(f"{APP}/me").json()["can_export_data"] is False


def test_me_reports_no_export_without_an_organisation(user_client: Client) -> None:
    assert user_client.get(f"{APP}/me").json()["can_export_data"] is False
