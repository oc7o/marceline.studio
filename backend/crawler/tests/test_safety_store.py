import logging
from typing import Any

import pytest

from catalog.models import Creator, Site
from crawler import services
from crawler.models import CrawlTask
from crawler.sites.base import MediaSpec, PageData, ParseResult, TaskSpec
from index.models import FrameHash, Media, Page
from index.services import SAFETY_BLOCK_REASON, SAFETY_GATE_REASON, gate_min_score

DETAIL = CrawlTask.Kind.DETAIL
LISTING = CrawlTask.Kind.LISTING
POSTER = Media.Kind.POSTER
VIDEO_URL = "https://site01.example/video/1/x/"
CARD_POSTER = "https://cdn.test/card.jpg"
DETAIL_POSTER = "https://cdn.test/full.jpg"
RELATED_URL = "https://site01.example/video/2/y/"


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


def detail_task(site: Site) -> CrawlTask:
    return CrawlTask.objects.create(
        site=site,
        url=VIDEO_URL,
        kind=DETAIL,
        context={"media": [{"kind": "poster", "url": CARD_POSTER}]},
    )


def detail_result(title: str, **page: Any) -> ParseResult:
    return ParseResult(
        page=PageData(url=VIDEO_URL, title=title, extra={"keywords": ["k"]}, **page),
        links=(TaskSpec(RELATED_URL, DETAIL, 10),),
        media=(MediaSpec(POSTER, DETAIL_POSTER),),
    )


def test_clear_page_is_stored_as_before(site01: Site) -> None:
    page = services.store_parse_result(detail_task(site01), detail_result("Lena Hartwell scene"))

    assert page is not None
    assert page.safety_status == Page.SafetyStatus.CLEAR
    assert page.safety_matches == [] and page.safety_flagged_at is None
    assert set(page.media.values_list("url", flat=True)) == {CARD_POSTER, DETAIL_POSTER}
    assert CrawlTask.objects.filter(url=RELATED_URL).exists()


def test_blocked_page_keeps_no_media_links_or_creators(
    site01: Site, caplog: pytest.LogCaptureFixture
) -> None:
    Creator.objects.create(name="Lena Hartwell")
    task = detail_task(site01)

    with caplog.at_level(logging.WARNING):
        page = services.store_parse_result(
            task, detail_result("Lena Hartwell", tags=("jailbait",), performers=("Lena Hartwell",))
        )

    assert page is not None
    page.refresh_from_db()
    assert page.safety_status == Page.SafetyStatus.BLOCKED
    assert page.safety_matches == [
        {"term": "jailbait", "category": "minor", "action": "auto_block_and_incident"}
    ]
    assert page.safety_flagged_at is not None
    assert page.url == VIDEO_URL
    assert "keywords" not in page.raw_extraction
    assert not Media.objects.exists()
    assert not page.creators.exists()
    assert list(CrawlTask.objects.values_list("url", flat=True)) == [VIDEO_URL]
    [record] = [r for r in caplog.records if "SAFETY" in r.getMessage()]
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert message == f"SAFETY BLOCK page={page.pk} site={site01.pk} categories=minor"
    assert VIDEO_URL not in caplog.text


def test_page_blocked_on_recrawl_loses_media_and_hashes(site01: Site) -> None:
    task = detail_task(site01)
    page = services.store_parse_result(task, detail_result("Scene"))
    assert page is not None
    done, pending = page.media.order_by("pk")[0], None
    Media.objects.filter(pk=done.pk).update(status=Media.Status.DONE)
    FrameHash.objects.create(media=done, frame_index=0, pdq=b"\x00" * 32, quality=90)
    pending = Media.objects.create(page=page, kind=Media.Kind.PREVIEW, url="https://cdn.test/p")

    services.store_parse_result(task, detail_result("Scene underage"))

    assert not FrameHash.objects.exists()
    assert set(Media.objects.values_list("status", "last_error")) == {
        (Media.Status.SKIPPED, SAFETY_BLOCK_REASON)
    }
    assert pending.pk in Media.objects.values_list("pk", flat=True)


def test_review_page_is_indexed_but_its_media_waits(
    site01: Site, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        page = services.store_parse_result(detail_task(site01), detail_result("Hidden cam scene"))

    assert page is not None
    assert page.safety_status == Page.SafetyStatus.REVIEW
    assert set(page.media.values_list("status", flat=True)) == {Media.Status.PENDING}
    assert CrawlTask.objects.filter(url=RELATED_URL).exists()
    assert f"SAFETY REVIEW page={page.pk} site={site01.pk} categories=non_consent" in caplog.text
    assert VIDEO_URL not in caplog.text
    assert "Hidden cam scene" not in caplog.text


def test_reviewed_clear_page_stays_clear_until_a_new_term_matches(site01: Site) -> None:
    task = detail_task(site01)
    page = services.store_parse_result(task, detail_result("Hidden cam scene"))
    assert page is not None
    from index.services import set_safety_status

    set_safety_status(page, Page.SafetyStatus.CLEAR)

    page = services.store_parse_result(task, detail_result("Hidden cam scene"))
    assert page is not None and page.safety_status == Page.SafetyStatus.CLEAR

    page = services.store_parse_result(task, detail_result("Hidden cam scene, drugged"))
    assert page is not None
    page.refresh_from_db()
    assert page.safety_status == Page.SafetyStatus.REVIEW
    assert page.safety_reviewed_at is None


def test_blocked_page_is_not_unblocked_by_a_recrawl(site01: Site) -> None:
    task = detail_task(site01)
    services.store_parse_result(task, detail_result("underage"))

    page = services.store_parse_result(task, detail_result("renamed"))

    assert page is not None and page.safety_status == Page.SafetyStatus.BLOCKED
    assert not Media.objects.exists()
    assert not CrawlTask.objects.filter(url=RELATED_URL).exists()


def test_a_clearly_safe_detail_page_gets_fetchable_media(site01: Site) -> None:
    page = services.store_parse_result(
        detail_task(site01), detail_result("Lena Hartwell hotel scene full")
    )

    assert page is not None
    assert page.safety_score is not None and page.safety_score >= gate_min_score()
    assert page.safety_score_reason == "clearly_safe"
    media = list(page.media.all())
    assert len(media) == 2
    assert all(row.eligible_at is not None for row in media)
    assert {row.status for row in media} == {Media.Status.PENDING}


def test_a_page_that_is_not_clearly_safe_keeps_its_media_unfetchable(site01: Site) -> None:
    # Not a term hit, so the page is indexed and stays `clear` — but its own text is not clearly
    # safe, so nothing may be fetched from it (ADR 0018).
    page = services.store_parse_result(
        detail_task(site01), detail_result("Sie ist 15 jahre alt zuhause")
    )

    assert page is not None
    assert page.safety_status == Page.SafetyStatus.CLEAR
    assert page.safety_score is not None and page.safety_score < gate_min_score()
    assert page.safety_score_reason == "age_pattern"
    assert set(page.media.values_list("status", "last_error", "eligible_at")) == {
        (Media.Status.SKIPPED, SAFETY_GATE_REASON, None)
    }


def test_listing_card_media_of_an_unscored_page_is_recorded_but_inert(site01: Site) -> None:
    """The hole issue #67 closes: a card must never make media fetchable by itself."""
    Page.objects.create(site=site01, url=VIDEO_URL, title="x")  # indexed, never scored
    listing = CrawlTask.objects.create(site=site01, url="https://site01.example/tags/a/", kind=LISTING)
    card = TaskSpec(VIDEO_URL, DETAIL, 10, (MediaSpec(POSTER, CARD_POSTER),))

    services.store_parse_result(listing, ParseResult(links=(card,)))

    media = Media.objects.get()
    assert media.url == CARD_POSTER  # the URL is still recorded
    assert media.eligible_at is None  # but it may never be requested


def test_listing_card_media_of_a_scored_page_stays_fetchable(site01: Site) -> None:
    # This page's own text was parsed and judged, so fresh card links for it are eligible.
    Page.objects.create(site=site01, url=VIDEO_URL, title="x", safety_score=1.0)
    listing = CrawlTask.objects.create(site=site01, url="https://site01.example/tags/a/", kind=LISTING)
    card = TaskSpec(VIDEO_URL, DETAIL, 10, (MediaSpec(POSTER, CARD_POSTER),))

    services.store_parse_result(listing, ParseResult(links=(card,)))

    assert Media.objects.get().eligible_at is not None


def test_a_recrawl_releases_media_once_a_page_becomes_clearly_safe(site01: Site) -> None:
    task = detail_task(site01)
    services.store_parse_result(task, detail_result("Sie ist 15 jahre alt zuhause"))
    assert not Media.objects.filter(eligible_at__isnull=False).exists()

    page = services.store_parse_result(task, detail_result("An ordinary studio scene title"))

    assert page is not None
    assert page.safety_score is not None and page.safety_score >= gate_min_score()
    media = list(page.media.all())
    assert all(row.eligible_at is not None for row in media)
    assert {row.status for row in media} == {Media.Status.PENDING}


def test_a_recrawl_makes_media_of_a_page_leaving_review_eligible(site01: Site) -> None:
    """A page under review keeps its media PENDING with no eligibility, not SKIPPED. When a
    re-crawl clears it, those rows must become eligible too, not only the skipped ones — else
    they wait for a `safety_scan` that may never come (2026-09-17: 816 rows)."""
    task = detail_task(site01)
    page = services.store_parse_result(task, detail_result("Hidden cam scene"))
    assert page is not None and page.safety_status == Page.SafetyStatus.REVIEW
    assert set(page.media.values_list("status", "eligible_at")) == {(Media.Status.PENDING, None)}

    page = services.store_parse_result(task, detail_result("An ordinary studio scene title"))

    assert page is not None and page.safety_status == Page.SafetyStatus.CLEAR
    assert page.safety_score is not None and page.safety_score >= gate_min_score()
    media = list(page.media.all())
    assert media and all(row.eligible_at is not None for row in media)
    assert {row.status for row in media} == {Media.Status.PENDING}


def test_a_recrawl_never_makes_media_of_a_page_still_under_review_eligible(site01: Site) -> None:
    task = detail_task(site01)
    services.store_parse_result(task, detail_result("Hidden cam scene"))

    page = services.store_parse_result(task, detail_result("Hidden cam scene, part two"))

    assert page is not None and page.safety_status == Page.SafetyStatus.REVIEW
    assert not page.media.filter(eligible_at__isnull=False).exists()


def test_listing_card_media_is_not_attached_to_blocked_pages(site01: Site) -> None:
    Page.objects.create(
        site=site01, url=VIDEO_URL, title="x", safety_status=Page.SafetyStatus.BLOCKED
    )
    listing = CrawlTask.objects.create(site=site01, url="https://site01.example/tags/a/", kind=LISTING)
    card = TaskSpec(VIDEO_URL, DETAIL, 10, (MediaSpec(POSTER, CARD_POSTER),))

    services.store_parse_result(listing, ParseResult(links=(card,)))

    assert not Media.objects.exists()
