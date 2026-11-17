"""Staff review queue for `catalog.Creator.needs_review` rows (issue #29 follow-up, operator
2026-09-17): mark reviewed, merge into an existing creator, or record it as an alias."""

import json
from typing import Any

import pytest
from django.test import Client

from catalog.models import Creator, Site
from index.models import Page
from marceline.models import CreatorMergeLog, OriginalOwnership
from marceline.tests.app_helpers import make_organisation
from matching.models import Reference

pytestmark = pytest.mark.django_db

BASE = "/api/v1/review/creators"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


@pytest.fixture
def studio(db: None) -> Any:
    return make_organisation("Studio Name")


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(domain="pirate.example.test")


def customer_reference(
    creator: Creator, organisation: Any, *, title: str = "Beach day", sha256: str = "ab" * 32
) -> Reference:
    reference = Reference.objects.create(
        creator=creator, title=title, duration_s=10, sha256=sha256, frame_fps=1
    )
    OriginalOwnership.objects.create(
        reference=reference, organisation=organisation, assigned_via=OriginalOwnership.Via.CUSTOMER
    )
    return reference


def test_queue_lists_organisation_linked_count_and_similar_suggestions(
    staff_client: Client, studio: Any
) -> None:
    existing = Creator.objects.create(name="Sasha Meyer")
    Creator.objects.create(name="Unrelated Person")
    needs_review = Creator.objects.create(name="sasha  meyer.", needs_review=True)
    customer_reference(needs_review, studio)

    response = staff_client.get(BASE)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["truncated"] is False
    [item] = body["items"]
    assert item["id"] == needs_review.pk
    assert item["name"] == "sasha  meyer."
    assert item["organisations"] == [studio.name]
    assert item["linked_references"] == 1
    similar_ids = {row["id"] for row in item["similar"]}
    assert existing.pk in similar_ids


def test_queue_excludes_reviewed_creators(staff_client: Client) -> None:
    Creator.objects.create(name="Already Reviewed", needs_review=False)

    response = staff_client.get(BASE)

    assert response.json()["items"] == []


def test_mark_reviewed_clears_the_flag(staff_client: Client) -> None:
    creator = Creator.objects.create(name="New Person", needs_review=True)

    response = staff_client.post(f"{BASE}/{creator.pk}/mark-reviewed")

    assert response.status_code == 200, response.content
    assert response.json() == {"id": creator.pk, "needs_review": False}
    creator.refresh_from_db()
    assert creator.needs_review is False


def test_mark_reviewed_unknown_creator_is_404(staff_client: Client) -> None:
    assert staff_client.post(f"{BASE}/999999/mark-reviewed").status_code == 404


def test_merge_moves_reference_cast_links_page_links_and_aliases(
    staff_client: Client, studio: Any, site: Site
) -> None:
    target = Creator.objects.create(name="Carla Bremer", aliases=["Old Alias"])
    source = Creator.objects.create(name="Studio Nord", aliases=["Nord Studio"], needs_review=True)
    primary_ref = customer_reference(source, studio, sha256="11" * 32)
    co_ref = Reference.objects.create(
        creator=target, title="Other scene", duration_s=5, sha256="22" * 32, frame_fps=1
    )
    co_ref.creators.set([target, source])
    page = Page.objects.create(site=site, url="https://pirate.example.test/p1")
    page.creators.add(source)

    response = post_json(staff_client, f"{BASE}/{source.pk}/merge", {"target_id": target.pk})

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["source_id"] == source.pk
    assert body["target_id"] == target.pk
    assert body["as_alias"] is False
    assert body["moved_references"] == 2
    assert body["moved_pages"] == 1
    assert body["aliases_added"] == ["Nord Studio"]

    assert not Creator.objects.filter(pk=source.pk).exists()
    primary_ref.refresh_from_db()
    assert primary_ref.creator_id == target.pk
    assert set(primary_ref.creators.values_list("pk", flat=True)) == {target.pk}
    co_ref.refresh_from_db()
    assert set(co_ref.creators.values_list("pk", flat=True)) == {target.pk}
    page.refresh_from_db()
    assert list(page.creators.values_list("pk", flat=True)) == [target.pk]
    target.refresh_from_db()
    assert set(target.aliases) == {"Old Alias", "Nord Studio"}

    log = CreatorMergeLog.objects.get()
    assert log.source_creator_id == source.pk
    assert log.source_name == "Studio Nord"
    assert log.target_id == target.pk
    assert log.as_alias is False
    assert log.moved_references == 2
    assert log.moved_pages == 1


def test_alias_adds_the_source_name_itself_as_an_alias(staff_client: Client) -> None:
    target = Creator.objects.create(name="Carla Bremer")
    source = Creator.objects.create(name="Studio Nord", needs_review=True)

    response = post_json(staff_client, f"{BASE}/{source.pk}/alias", {"target_id": target.pk})

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["as_alias"] is True
    assert body["aliases_added"] == ["Studio Nord"]
    target.refresh_from_db()
    assert target.aliases == ["Studio Nord"]
    log = CreatorMergeLog.objects.get()
    assert log.as_alias is True


def test_merge_refuses_a_creator_into_itself(staff_client: Client) -> None:
    creator = Creator.objects.create(name="Solo", needs_review=True)

    response = post_json(staff_client, f"{BASE}/{creator.pk}/merge", {"target_id": creator.pk})

    assert response.status_code == 409
    assert Creator.objects.filter(pk=creator.pk).exists()


def test_merge_refuses_a_source_that_is_not_needs_review(staff_client: Client) -> None:
    reviewed = Creator.objects.create(name="Already Reviewed", needs_review=False)
    other = Creator.objects.create(name="Other")

    response = post_json(staff_client, f"{BASE}/{reviewed.pk}/merge", {"target_id": other.pk})

    assert response.status_code == 409
    assert Creator.objects.filter(pk=reviewed.pk).exists()


def test_merge_unknown_ids_are_404(staff_client: Client) -> None:
    creator = Creator.objects.create(name="Someone", needs_review=True)

    assert (
        post_json(staff_client, f"{BASE}/999999/merge", {"target_id": creator.pk}).status_code
        == 404
    )
    assert (
        post_json(staff_client, f"{BASE}/{creator.pk}/merge", {"target_id": 999999}).status_code
        == 404
    )
