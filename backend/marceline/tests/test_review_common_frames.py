"""Studio labeling queue for common-frame groups (issue #16, ADR 0015)."""

import json
from typing import Any

import pytest
from django.test import Client

from catalog.models import Site
from index.models import FrameHash, Media, Page
from matching.media_hash import PREVIEW_FPS
from matching.models import CommonFrameGroup, CommonFrameMember

pytestmark = pytest.mark.django_db

BASE = "/api/v1/review/common-frames"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(domain="pirate.example.test")


def make_page(
    site: Site, slug: str, *, safety_status: str = "clear", safety_score: float | None = 1.0
) -> Page:
    page = Page.objects.create(
        site=site,
        url=f"https://pirate.example.test/{slug}",
        title=slug,
        safety_status=safety_status,
        safety_score=safety_score,
    )
    Media.objects.create(page=page, kind=Media.Kind.POSTER, url=f"https://cdn.test/{slug}.jpg")
    return page


def make_group(
    *,
    page_ids: list[int],
    status: str = CommonFrameGroup.Status.PROPOSED,
    kind: str = CommonFrameGroup.Kind.UNLABELED,
    page_count: int | None = None,
    site_count: int = 2,
) -> CommonFrameGroup:
    return CommonFrameGroup.objects.create(
        representative_hash=b"\x01" * 32,
        max_distance=8,
        member_frame_count=len(page_ids),
        page_count=page_count if page_count is not None else len(page_ids),
        site_count=site_count,
        sample_page_ids=page_ids,
        status=status,
        kind=kind,
    )


def make_member(
    group: CommonFrameGroup,
    page: Page,
    *,
    media_kind: str = Media.Kind.FRAME,
    url: str = "https://cdn.test/x_2.jpg",
    frame_index: int = 0,
    distance: int = 3,
) -> CommonFrameMember:
    """A page frame that is this group's member — `matching.models.CommonFrameMember` plus the
    `index.FrameHash`/`index.Media` row it points at, which the queue's test fixtures otherwise
    never create (the router used to read only `CommonFrameGroup.sample_page_ids`)."""
    media = Media.objects.create(page=page, kind=media_kind, url=url)
    frame = FrameHash.objects.create(
        media=media, frame_index=frame_index, pdq=b"\x00" * 32, quality=90
    )
    return CommonFrameMember.objects.create(group=group, frame=frame, page=page, distance=distance)


def test_queue_default_shows_only_proposed_groups(staff_client: Client, site: Site) -> None:
    page_a = make_page(site, "a")
    proposed = make_group(page_ids=[page_a.pk])
    make_group(page_ids=[page_a.pk], status=CommonFrameGroup.Status.CONFIRMED)

    response = staff_client.get(BASE)

    assert response.status_code == 200, response.content
    body = response.json()
    assert [item["id"] for item in body["items"]] == [proposed.pk]
    assert body["truncated"] is False


def test_queue_show_all_orders_proposed_then_confirmed_then_rejected(
    staff_client: Client, site: Site
) -> None:
    page_a = make_page(site, "a")
    rejected = make_group(page_ids=[page_a.pk], status=CommonFrameGroup.Status.REJECTED)
    confirmed = make_group(page_ids=[page_a.pk], status=CommonFrameGroup.Status.CONFIRMED)
    proposed = make_group(page_ids=[page_a.pk], status=CommonFrameGroup.Status.PROPOSED)

    response = staff_client.get(BASE, {"show": "all"})

    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [proposed.pk, confirmed.pk, rejected.pk]


def test_queue_lists_sample_pages_with_media(staff_client: Client, site: Site) -> None:
    page_a = make_page(site, "a")
    page_b = make_page(site, "b")
    group = make_group(page_ids=[page_a.pk, page_b.pk])

    response = staff_client.get(BASE)

    [item] = response.json()["items"]
    assert item["id"] == group.pk
    assert item["kind"] == "unlabeled"
    assert item["status"] == "proposed"
    assert item["discounting"] is False
    pages = {row["page"]["id"]: row for row in item["sample_pages"]}
    assert set(pages) == {page_a.pk, page_b.pk}
    assert pages[page_a.pk]["page"]["title"] == "a"
    assert len(pages[page_a.pk]["media"]) == 1
    assert pages[page_a.pk]["media_gated"] is False


def test_safety_flagged_member_page_shown_without_media(staff_client: Client, site: Site) -> None:
    """The gate is server-side (`media_refs`), never a client-side check (issue #16)."""
    clear_page = make_page(site, "clear")
    flagged_page = make_page(site, "flagged", safety_status="review", safety_score=None)
    blocked_page = make_page(site, "blocked", safety_status="blocked", safety_score=None)
    make_group(page_ids=[clear_page.pk, flagged_page.pk, blocked_page.pk])

    response = staff_client.get(BASE)

    [item] = response.json()["items"]
    pages = {row["page"]["id"]: row for row in item["sample_pages"]}
    assert len(pages[clear_page.pk]["media"]) == 1
    assert pages[flagged_page.pk]["media"] == []
    assert pages[blocked_page.pk]["media"] == []
    # The page itself (title, url, safety_status) is still shown, exactly as the cluster queue
    # shows a safety-flagged member — only the image request is withheld.
    assert pages[flagged_page.pk]["page"]["safety_status"] == "review"
    assert pages[blocked_page.pk]["page"]["safety_status"] == "blocked"
    assert pages[clear_page.pk]["media_gated"] is False
    assert pages[flagged_page.pk]["media_gated"] is True


def test_gated_member_page_says_why_it_shows_no_image(staff_client: Client, site: Site) -> None:
    """A page clear but scored below the gate has images that must not be served (ADR 0018).

    The operator could not judge one group on 2026-09-18 because every sample page sat below the
    gate and the screen only said "No page images" — which also means "this page has none".
    """
    gated = make_page(site, "gated", safety_score=0.7)
    fine = make_page(site, "fine")
    make_group(page_ids=[gated.pk, fine.pk])

    response = staff_client.get(BASE)

    pages = {row["page"]["id"]: row for row in response.json()["items"][0]["sample_pages"]}
    assert pages[gated.pk]["page"]["safety_status"] == "clear"
    assert pages[gated.pk]["media"] == []
    assert pages[gated.pk]["media_gated"] is True
    assert pages[fine.pk]["media_gated"] is False


def test_member_frames_report_position_distance_and_the_page_frame_count(
    staff_client: Client, site: Site
) -> None:
    """The operator's next complaint (2026-09-18): "I couldn't review the group because I don't
    know which frames are the same." A hover-frame member's URL number is its position; the
    page's total frame count is every FrameHash it holds, not just this group's member."""
    page_a = make_page(site, "a")
    group = make_group(page_ids=[page_a.pk])
    make_member(
        group, page_a, media_kind=Media.Kind.FRAME, url="https://cdn.test/a_3.jpg", distance=5
    )
    # A second, unrelated frame on the same page: only the frame count changes.
    Media.objects.create(page=page_a, kind=Media.Kind.FRAME, url="https://cdn.test/a_7.jpg")
    other_media = Media.objects.get(url="https://cdn.test/a_7.jpg")
    FrameHash.objects.create(media=other_media, pdq=b"\x01" * 32, quality=90)

    response = staff_client.get(BASE)

    [item] = response.json()["items"]
    [row] = [p for p in item["sample_pages"] if p["page"]["id"] == page_a.pk]
    # make_page's own poster has no FrameHash; only the member's frame and the one added above.
    assert row["frame_count"] == 2
    [frame] = row["member_frames"]
    assert frame["frame_index"] == 0
    assert frame["position"] == 3  # parsed from "..._3.jpg" (matching.timeline.frame_position)
    assert frame["distance"] == 5
    assert frame["showable"] is True
    assert frame["kind"] == "frame"
    assert frame["timestamp_s"] is None  # a hover thumbnail's spacing is not a known rate


def test_preview_clip_member_is_not_showable_but_has_an_exact_timestamp(
    staff_client: Client, site: Site
) -> None:
    """A preview clip is sampled at a fixed, known rate (matching.media_hash.PREVIEW_FPS), so its
    sample position converts to real seconds; the passthrough can only ever show the whole clip,
    never this one frame, so `showable` must say so rather than let the screen try and fail."""
    page_a = make_page(site, "a")
    group = make_group(page_ids=[page_a.pk])
    make_member(
        group,
        page_a,
        media_kind=Media.Kind.PREVIEW,
        url="https://cdn.test/a.mp4",
        frame_index=4,
        distance=2,
    )

    response = staff_client.get(BASE)

    [item] = response.json()["items"]
    [row] = [p for p in item["sample_pages"] if p["page"]["id"] == page_a.pk]
    [frame] = row["member_frames"]
    assert frame["kind"] == "preview"
    assert frame["showable"] is False
    assert frame["position"] == 4
    assert frame["timestamp_s"] == 4 / PREVIEW_FPS
    # The member's own media never gets a MediaRefOut: the passthrough would 404 on it anyway.
    # (page_a's default poster from make_page is the only thing `media` can show here.)
    assert frame["media_id"] not in {ref["id"] for ref in row["media"]}


def test_gated_page_still_reports_member_frame_numbers(staff_client: Client, site: Site) -> None:
    """Numbers derived from a page's own stored hash are not the pirated image; only `media`
    (the passthrough ids) is withheld by the gate."""
    gated = make_page(site, "gated", safety_score=0.7)
    group = make_group(page_ids=[gated.pk])
    make_member(group, gated, distance=9)

    response = staff_client.get(BASE)

    [item] = response.json()["items"]
    [row] = [p for p in item["sample_pages"] if p["page"]["id"] == gated.pk]
    assert row["media_gated"] is True
    assert row["media"] == []
    [frame] = row["member_frames"]
    assert frame["distance"] == 9
    assert frame["showable"] is True  # the passthrough *could* show it; the gate is why it doesn't


def test_label_confirms_a_studio_intro_and_it_discounts(staff_client: Client, site: Site) -> None:
    page_a = make_page(site, "a")
    group = make_group(page_ids=[page_a.pk])

    response = post_json(
        staff_client,
        f"{BASE}/{group.pk}/label",
        {"kind": "studio_intro", "studio_name": "Some Studio", "status": "confirmed", "label": ""},
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["kind"] == "studio_intro"
    assert body["studio_name"] == "Some Studio"
    assert body["status"] == "confirmed"
    assert body["discounting"] is True
    assert body["labeled_at"] is not None
    group.refresh_from_db()
    assert group.kind == CommonFrameGroup.Kind.STUDIO_INTRO
    assert group.status == CommonFrameGroup.Status.CONFIRMED
    assert group.studio_name == "Some Studio"
    assert group.labeled_at is not None


def test_label_not_common_never_discounts_even_when_confirmed(
    staff_client: Client, site: Site
) -> None:
    page_a = make_page(site, "a")
    group = make_group(page_ids=[page_a.pk])

    response = post_json(
        staff_client,
        f"{BASE}/{group.pk}/label",
        {"kind": "not_common", "studio_name": "", "status": "confirmed", "label": "false positive"},
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["kind"] == "not_common"
    assert body["discounting"] is False
    assert body["label"] == "false positive"


def test_label_rejected_never_discounts(staff_client: Client, site: Site) -> None:
    page_a = make_page(site, "a")
    group = make_group(page_ids=[page_a.pk])

    response = post_json(
        staff_client,
        f"{BASE}/{group.pk}/label",
        {"kind": "watermark", "studio_name": "", "status": "rejected", "label": ""},
    )

    assert response.status_code == 200, response.content
    assert response.json()["discounting"] is False


def test_label_unknown_group_is_404(staff_client: Client) -> None:
    response = post_json(
        staff_client,
        f"{BASE}/999999/label",
        {"kind": "watermark", "studio_name": "", "status": "confirmed", "label": ""},
    )

    assert response.status_code == 404


def test_label_rejects_an_unknown_kind(staff_client: Client, site: Site) -> None:
    page_a = make_page(site, "a")
    group = make_group(page_ids=[page_a.pk])

    response = post_json(
        staff_client,
        f"{BASE}/{group.pk}/label",
        {"kind": "not_a_real_kind", "studio_name": "", "status": "confirmed", "label": ""},
    )

    assert response.status_code == 422
    group.refresh_from_db()
    assert group.kind == CommonFrameGroup.Kind.UNLABELED


def test_counts_include_proposed_common_frame_groups(staff_client: Client, site: Site) -> None:
    page_a = make_page(site, "a")
    make_group(page_ids=[page_a.pk])
    make_group(page_ids=[page_a.pk], status=CommonFrameGroup.Status.CONFIRMED)

    response = staff_client.get("/api/v1/review/counts")

    assert response.status_code == 200, response.content
    assert response.json()["common_frames"] == 1
