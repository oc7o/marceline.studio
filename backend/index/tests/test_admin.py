from collections.abc import Callable

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog.models import Creator
from index import admin
from index.admin import BLOCKED_NOTICE
from index.models import FrameHash, Media, Page, SafetyRuleDecision, SiteCreatorBreadcrumb
from index.services import SAFETY_BLOCK_REASON, SAFETY_REVIEW_REASON, cancel_page_media

MakePage = Callable[[str], Page]
CHANGELIST = "/admin/index/page/"
MEDIA_CHANGELIST = "/admin/index/media/"


def reason(page: Page, code: str) -> Page:
    """A stored score reason; the codes belong to `crawler/safety_score.py`."""
    Page.objects.filter(pk=page.pk).update(safety_score_reason=code)
    return page


def flagged(make_page: MakePage, slug: str, status: Page.SafetyStatus) -> Page:
    page = make_page(slug)
    Page.objects.filter(pk=page.pk).update(safety_status=status)
    page.refresh_from_db()
    return page


def change_data(page: Page, **overrides: str) -> dict[str, str]:
    data = {
        "site": str(page.site_id),
        "url": page.url,
        "title": page.title,
        "tags": "[]",
        "raw_extraction": "{}",
        "availability": page.availability,
        "availability_checked_at_0": "",
        "availability_checked_at_1": "",
        "uploaded_at_0": "",
        "uploaded_at_1": "",
        "safety_status": page.safety_status,
        "safety_note": page.safety_note,
        "_save": "Save",
    }
    return {**data, **overrides}


def test_mark_clear_action_releases_review_media(make_page: MakePage, admin_client: Client) -> None:
    review = flagged(make_page, "review", Page.SafetyStatus.REVIEW)
    blocked = flagged(make_page, "blocked", Page.SafetyStatus.BLOCKED)
    Media.objects.create(page=review, kind=Media.Kind.POSTER, url="https://cdn.test/1")
    Media.objects.create(
        page=review,
        kind=Media.Kind.PREVIEW,
        url="https://cdn.test/2",
        status=Media.Status.SKIPPED,
        last_error=SAFETY_REVIEW_REASON,
    )

    response = admin_client.post(
        CHANGELIST, {"action": "mark_clear", "_selected_action": [review.pk, blocked.pk]}
    )

    assert response.status_code == 302
    review.refresh_from_db()
    blocked.refresh_from_db()
    assert review.safety_status == Page.SafetyStatus.CLEAR
    assert review.safety_reviewed_at is not None
    assert blocked.safety_status == Page.SafetyStatus.BLOCKED
    assert set(review.media.values_list("status", flat=True)) == {Media.Status.PENDING}


def test_changelist_filters_by_safety_status(make_page: MakePage, admin_client: Client) -> None:
    flagged(make_page, "review-page", Page.SafetyStatus.REVIEW)
    make_page("clear-page")

    response = admin_client.get(CHANGELIST, {"safety_status__exact": "review"})

    assert response.status_code == 200
    assert b"review-page" in response.content and b"clear-page" not in response.content


def test_blocked_page_shows_notice_and_hides_media(
    make_page: MakePage, admin_client: Client
) -> None:
    blocked = flagged(make_page, "blocked", Page.SafetyStatus.BLOCKED)
    Media.objects.create(page=blocked, kind=Media.Kind.POSTER, url="https://cdn.test/hidden.jpg")

    response = admin_client.get(f"{CHANGELIST}{blocked.pk}/change/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "docs/safety.md" in content
    assert BLOCKED_NOTICE.split(":")[0] in content
    assert "cdn.test/hidden.jpg" not in content
    assert '<img src="http' not in content  # only the admin's own static icons


def test_unblocking_requires_a_reason(make_page: MakePage, admin_client: Client) -> None:
    blocked = flagged(make_page, "blocked", Page.SafetyStatus.BLOCKED)
    media = Media.objects.create(page=blocked, kind=Media.Kind.POSTER, url="https://cdn.test/1")
    FrameHash.objects.create(media=media, frame_index=0, pdq=b"\x00" * 32, quality=90)
    cancel_page_media([blocked.pk])
    url = f"{CHANGELIST}{blocked.pk}/change/"

    response = admin_client.post(url, change_data(blocked, safety_status="clear"))

    assert response.status_code == 200
    assert "Give the reason for changing a blocked page." in response.content.decode()
    blocked.refresh_from_db()
    assert blocked.safety_status == Page.SafetyStatus.BLOCKED

    response = admin_client.post(
        url, change_data(blocked, safety_status="clear", safety_note="Title false positive")
    )

    assert response.status_code == 302
    blocked.refresh_from_db()
    assert blocked.safety_status == Page.SafetyStatus.CLEAR
    assert blocked.safety_note == "Title false positive"
    assert blocked.safety_reviewed_at is not None
    media.refresh_from_db()
    assert (media.status, media.last_error) == (Media.Status.PENDING, "")


def test_reversing_an_auto_block_is_logged(make_page: MakePage, admin_client: Client) -> None:
    blocked = flagged(make_page, "forced", Page.SafetyStatus.BLOCKED)
    matches = [{"term": "forced", "category": "non_consent", "action": "auto_block"}]
    Page.objects.filter(pk=blocked.pk).update(safety_matches=matches, safety_rule_version="sr-1")

    response = admin_client.post(
        f"{CHANGELIST}{blocked.pk}/change/",
        change_data(blocked, safety_status="clear", safety_note="Studio title, cleared"),
    )

    assert response.status_code == 302
    [decision] = SafetyRuleDecision.objects.filter(page=blocked)
    assert (decision.kind, decision.status, decision.note) == (
        SafetyRuleDecision.Kind.STAFF_STATUS,
        "clear",
        "Studio title, cleared",
    )
    assert (decision.terms, decision.rule_version) == (matches, "sr-1")
    assert decision.decided_by == "admin"


def test_release_queue_pages_cannot_be_unblocked_by_hand(
    make_page: MakePage, admin_client: Client
) -> None:
    blocked = flagged(make_page, "flagged term", Page.SafetyStatus.BLOCKED)
    Page.objects.filter(pk=blocked.pk).update(
        safety_matches=[
            {"term": "flagged term", "category": "minor", "action": "customer_release_queue"}
        ]
    )

    response = admin_client.post(
        f"{CHANGELIST}{blocked.pk}/change/",
        change_data(blocked, safety_status="clear", safety_note="looks fine"),
    )

    assert response.status_code == 200
    assert "customer_release_queue rule" in response.content.decode()
    blocked.refresh_from_db()
    assert blocked.safety_status == Page.SafetyStatus.BLOCKED


def test_blocking_in_the_admin_cancels_media(make_page: MakePage, admin_client: Client) -> None:
    page = make_page("page")
    Media.objects.create(page=page, kind=Media.Kind.POSTER, url="https://cdn.test/1")
    data = change_data(
        page,
        safety_status="blocked",
        **{
            "media-TOTAL_FORMS": "1",
            "media-INITIAL_FORMS": "1",
            "media-0-id": str(page.media.get().pk),
            "media-0-page": str(page.pk),
            "media-0-kind": "poster",
            "media-0-url": "https://cdn.test/1",
            "media-0-status": "pending",
            "media-0-sha256": "",
            "media-0-width": "",
            "media-0-height": "",
            "media-0-fetched_at_0": "",
            "media-0-fetched_at_1": "",
        },
    )

    response = admin_client.post(f"{CHANGELIST}{page.pk}/change/", data)

    assert response.status_code == 302
    assert page.media.get().last_error == SAFETY_BLOCK_REASON


def test_breadcrumbs_are_read_only_and_pages_filter_by_activity(
    make_page: MakePage, admin_client: Client
) -> None:
    page = make_page("inactive")
    Page.objects.filter(pk=page.pk).update(active=False, inactive_reason="untargeted")
    now = timezone.now()
    SiteCreatorBreadcrumb.objects.create(
        site=page.site,
        creator=Creator.objects.create(name="Lena"),
        page_count=3,
        first_seen=now,
        last_seen=now,
    )

    assert admin_client.get(f"{CHANGELIST}?active__exact=0").status_code == 200
    assert admin_client.get("/admin/index/sitecreatorbreadcrumb/").status_code == 200
    assert admin_client.get("/admin/index/sitecreatorbreadcrumb/add/").status_code == 403
    assert admin_client.get("/admin/catalog/watchlistentry/add/").status_code == 200


def test_free_text_filter_caches_the_vocabulary(make_page: MakePage) -> None:
    """The filter's choices are re-read at most once per `VALUES_CACHE_TTL_S`.

    Django's default filter for this free-text field ran `SELECT DISTINCT safety_score_reason`
    over the whole table on every changelist load: a sequential scan of 111 MB, measured at 61 ms
    of the changelist's 180 ms on the development database (81,398 pages).
    """
    admin._cached_values.clear()
    reason(make_page("thin"), "thin_text")
    reason(make_page("safe"), "clearly_safe")

    first = admin.stored_values(Page, "safety_score_reason", now=1000.0)
    assert first == ("clearly_safe", "thin_text")

    reason(make_page("later"), "site_risk")
    assert (
        admin.stored_values(Page, "safety_score_reason", now=1000.0 + admin.VALUES_CACHE_TTL_S - 1)
        == first
    )
    assert admin.stored_values(
        Page, "safety_score_reason", now=1000.0 + admin.VALUES_CACHE_TTL_S
    ) == (
        "clearly_safe",
        "site_risk",
        "thin_text",
    )


def test_score_reason_filter_filters_without_rereading(
    make_page: MakePage, admin_client: Client
) -> None:
    admin._cached_values.clear()
    thin = reason(make_page("thin"), "thin_text")
    reason(make_page("safe"), "clearly_safe")
    assert admin_client.get(CHANGELIST).status_code == 200  # first load fills the cache

    with CaptureQueriesContext(connection) as queries:
        response = admin_client.get(CHANGELIST, {"safety_score_reason": "thin_text"})

    assert [page.pk for page in response.context["cl"].result_list] == [thin.pk]
    assert not [query for query in queries if "DISTINCT" in query["sql"]]


def test_media_content_type_filter_is_cached_too(
    make_page: MakePage, make_media: Callable[..., Media], admin_client: Client
) -> None:
    """`index_media` is 67 MB, and its content types come off the wire, so the same rule holds."""
    admin._cached_values.clear()
    page = make_page("with-media")
    picture = make_media(page, url="https://cdn.test/a.jpg")
    Media.objects.filter(pk=picture.pk).update(content_type="image/jpeg")
    clip = make_media(page, url="https://cdn.test/a.mp4", kind=Media.Kind.PREVIEW)
    Media.objects.filter(pk=clip.pk).update(content_type="video/mp4")
    assert admin_client.get(MEDIA_CHANGELIST).status_code == 200

    with CaptureQueriesContext(connection) as queries:
        response = admin_client.get(MEDIA_CHANGELIST, {"content_type": "video/mp4"})

    assert [row.pk for row in response.context["cl"].result_list] == [clip.pk]
    assert not [query for query in queries if "DISTINCT" in query["sql"]]
