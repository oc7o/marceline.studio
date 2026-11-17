"""Staff queue for refused customer upload claims (issue #29 amendment, operator 2026-09-17):
list, resolve, dismiss. `marceline/tests/test_app_fingerprints.py` covers how a claim is created
and classified; this file covers the queue built on top of it."""

import json
from typing import Any

import pytest
from django.test import Client

from catalog.models import Creator
from marceline.models import RefusedClaim
from marceline.tests.app_helpers import add_member, make_organisation
from matching.models import Reference

pytestmark = pytest.mark.django_db

BASE = "/api/v1/review/claims"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


@pytest.fixture
def studio(db: None) -> Any:
    return make_organisation("Studio Name")


@pytest.fixture
def foreign_reference(db: None) -> Reference:
    creator = Creator.objects.create(name="Someone Else")
    return Reference.objects.create(
        creator=creator, title="Their original", duration_s=10, sha256="cd" * 32, frame_fps=1
    )


def make_claim(
    studio: Any,
    foreign_reference: Reference,
    *,
    reason: str = RefusedClaim.Reason.OWNED_BY_OTHER,
    status: str = RefusedClaim.Status.OPEN,
    requested_by: Any = None,
) -> RefusedClaim:
    return RefusedClaim.objects.create(
        organisation=studio,
        sha256=foreign_reference.sha256,
        reason=reason,
        reference_ids=[foreign_reference.pk],
        requested_by=requested_by,
        status=status,
    )


def test_queue_lists_open_claims_by_default(
    staff_client: Client, studio: Any, foreign_reference: Reference
) -> None:
    member = add_member(studio, "member@studio.test")
    claim = make_claim(studio, foreign_reference, requested_by=member)
    make_claim(studio, foreign_reference, status=RefusedClaim.Status.RESOLVED)

    response = staff_client.get(BASE)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["truncated"] is False
    [item] = body["items"]
    assert item["id"] == claim.pk
    assert item["organisation_name"] == studio.name
    assert item["sha256"] == foreign_reference.sha256
    assert item["reason"] == "owned_by_other"
    assert item["references"] == [
        {"id": foreign_reference.pk, "title": "Their original", "creator_name": "Someone Else"}
    ]
    assert item["requested_by"] == "member@studio.test"
    assert item["status"] == "open"


def test_queue_status_filter_all_includes_resolved_and_dismissed(
    staff_client: Client, studio: Any, foreign_reference: Reference
) -> None:
    make_claim(studio, foreign_reference, status=RefusedClaim.Status.RESOLVED)
    make_claim(studio, foreign_reference, status=RefusedClaim.Status.DISMISSED)

    response = staff_client.get(BASE, {"status": "all"})

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2


def test_queue_unknown_status_filter_is_422(staff_client: Client) -> None:
    assert staff_client.get(BASE, {"status": "nonsense"}).status_code == 422


def test_resolve_records_note_and_who(
    staff_client: Client, studio: Any, foreign_reference: Reference
) -> None:
    claim = make_claim(studio, foreign_reference)

    response = post_json(staff_client, f"{BASE}/{claim.pk}/resolve", {"note": "rights sorted out"})

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "resolved"
    assert body["note"] == "rights sorted out"
    assert body["resolved_by"] == "reviewer"
    claim.refresh_from_db()
    assert claim.status == RefusedClaim.Status.RESOLVED
    assert claim.resolved_at is not None


def test_dismiss_records_note(
    staff_client: Client, studio: Any, foreign_reference: Reference
) -> None:
    claim = make_claim(studio, foreign_reference)

    response = post_json(staff_client, f"{BASE}/{claim.pk}/dismiss", {"note": "false alarm"})

    assert response.status_code == 200, response.content
    assert response.json()["status"] == "dismissed"
    claim.refresh_from_db()
    assert claim.status == RefusedClaim.Status.DISMISSED


def test_resolve_a_settled_claim_is_a_conflict(
    staff_client: Client, studio: Any, foreign_reference: Reference
) -> None:
    claim = make_claim(studio, foreign_reference, status=RefusedClaim.Status.RESOLVED)

    response = post_json(staff_client, f"{BASE}/{claim.pk}/resolve", {"note": "again?"})

    assert response.status_code == 409


def test_resolve_unknown_claim_is_404(staff_client: Client) -> None:
    assert post_json(staff_client, f"{BASE}/999999/resolve", {"note": ""}).status_code == 404
