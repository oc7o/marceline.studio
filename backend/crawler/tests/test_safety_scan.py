from collections.abc import Callable
from datetime import datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog.models import Site
from crawler import services
from crawler.safety_score import SiteRisk
from index.models import FrameHash, Media, Page
from index.services import SAFETY_BLOCK_REASON, cancel_page_media, gate_min_score

MakePage = Callable[[str], Page]


def add_media(page: Page, name: str, status: Media.Status, *, hashed: bool = False) -> Media:
    media = Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=f"https://cdn.test/{name}", status=status
    )
    if hashed:
        FrameHash.objects.create(media=media, frame_index=0, pdq=b"\x01" * 32, quality=90)
    return media


def setup_pages(make_page: MakePage) -> tuple[Page, Page, Page]:
    clear = make_page("clear")
    clear_media = add_media(clear, "c", Media.Status.DONE, hashed=True)
    review = make_page("review")
    Page.objects.filter(pk=review.pk).update(title="Hidden cam scene")
    add_media(review, "r", Media.Status.PENDING)
    blocked = make_page("blocked")
    Page.objects.filter(pk=blocked.pk).update(raw_extraction={"keywords": ["underage"]})
    add_media(blocked, "b1", Media.Status.DONE, hashed=True)
    add_media(blocked, "b2", Media.Status.PENDING)
    assert clear_media.hashes.count() == 1
    return clear, review, blocked


def scan(*args: str) -> str:
    out = StringIO()
    call_command("safety_scan", *args, stdout=out)
    return out.getvalue()


def test_dry_run_reports_and_writes_nothing(make_page: MakePage) -> None:
    setup_pages(make_page)

    output = scan("--dry-run", "--list")

    assert "scanned 3 page(s) (dry run, nothing written)" in output
    assert "blocked: 1 page(s)\n  categories: minor=1\n  sites: example.test=1" in output
    assert "review: 1 page(s)\n  categories: non_consent=1" in output
    assert "blocked-page media to cancel: 2, frame hashes to delete: 1" in output
    assert "Hidden cam" not in output and "underage" not in output
    assert set(Page.objects.values_list("safety_status", flat=True)) == {"clear"}
    assert FrameHash.objects.count() == 2
    assert not Media.objects.filter(status=Media.Status.SKIPPED).exists()


def test_apply_sets_statuses_and_cancels_blocked_media(make_page: MakePage) -> None:
    clear, review, blocked = setup_pages(make_page)

    output = scan()

    assert "blocked-page media cancelled: 2, frame hashes deleted: 1" in output
    assert "status changes: clear->blocked=1, clear->review=1" in output
    statuses = dict(Page.objects.values_list("pk", "safety_status"))
    assert statuses == {clear.pk: "clear", review.pk: "review", blocked.pk: "blocked"}
    assert set(blocked.media.values_list("status", "last_error")) == {
        (Media.Status.SKIPPED, SAFETY_BLOCK_REASON)
    }
    assert list(FrameHash.objects.values_list("media__page", flat=True)) == [clear.pk]
    assert set(review.media.values_list("status", flat=True)) == {Media.Status.PENDING}

    again = scan()
    assert "status changes" not in again
    assert "blocked-page media cancelled: 0, frame hashes deleted: 0" in again


def test_scan_scores_pages_and_reports_the_gate_cost(make_page: MakePage) -> None:
    """`safety_scan` is also the backfill: it scores pages indexed before the gate existed."""
    clear, review, blocked = setup_pages(make_page)
    Page.objects.update(safety_score=None)

    output = scan("--gate-report")

    assert Page.objects.filter(safety_score__isnull=True).count() == 0
    assert "media gate, pages per site:" in output
    assert "TOTAL" in output
    # The clear page may be fingerprinted; the flagged ones are text-only.
    clear_score = Page.objects.get(pk=clear.pk).safety_score
    assert clear_score is not None and clear_score >= gate_min_score()
    assert Page.objects.get(pk=review.pk).safety_score == 0.0
    assert Page.objects.get(pk=blocked.pk).safety_score == 0.0
    assert "text-only reasons:" in output


def test_backfill_makes_media_of_safe_pages_eligible(make_page: MakePage) -> None:
    """Media stored before the gate existed has no eligibility, so nothing would ever fetch it.

    Only a detail-page parse grants `eligible_at`, and those pages were parsed long ago: without
    this backfill the whole queue stays inert after the migration (2026-09-16: 6,278 pending rows).
    """
    page = make_page("stored-before-the-gate")
    pending = add_media(page, "p", Media.Status.PENDING)
    done = add_media(page, "d", Media.Status.DONE)
    assert pending.eligible_at is None

    dry = scan("--dry-run")
    assert "to make eligible: 2" in dry
    pending.refresh_from_db()
    assert pending.eligible_at is None

    output = scan()

    assert "made eligible: 2" in output
    pending.refresh_from_db()
    done.refresh_from_db()
    assert pending.eligible_at is not None
    assert done.eligible_at is not None


def test_gated_page_media_never_becomes_eligible(make_page: MakePage) -> None:
    """The backfill grants eligibility from the gate's verdict, not from a page's mere existence."""
    page = make_page("gated")
    Page.objects.filter(pk=page.pk).update(title="Hidden cam scene")
    media = add_media(page, "g", Media.Status.PENDING)

    scan()

    media.refresh_from_db()
    assert media.eligible_at is None


def test_site_filter(make_page: MakePage) -> None:
    setup_pages(make_page)
    other = Site.objects.create(domain="other.test")
    Page.objects.create(site=other, url="https://other.test/x", title="jailbait")

    output = scan("--site", "other.test")

    assert "scanned 1 page(s)" in output
    assert Page.objects.get(site=other).safety_status == Page.SafetyStatus.BLOCKED
    assert not Page.objects.filter(site__domain="example.test").exclude(safety_status="clear")


def block_while_scanning(monkeypatch: pytest.MonkeyPatch, page: Page) -> None:
    """Block `page` the way a concurrent crawl would, after the scan has read it.

    `site_risk` runs once per site, after the scan's page query was evaluated and before the
    verdict is stored, so the scan holds a stale copy of the page from here on. The risk is taken
    before the block so the scan's own verdict is exactly what it was without the crawl.
    """
    real_site_risk = services.site_risk

    def crawler_blocks_meanwhile(site: Site, *, now: datetime | None = None) -> SiteRisk:
        risk = real_site_risk(site, now=now)
        Page.objects.filter(pk=page.pk).update(
            safety_status=Page.SafetyStatus.BLOCKED,
            safety_matches=[{"term": "x", "category": "minor", "action": "block"}],
            safety_score=0.0,
            safety_scored_at=timezone.now(),
        )
        cancel_page_media([page.pk])
        return risk

    monkeypatch.setattr(services, "site_risk", crawler_blocks_meanwhile)


def test_scan_never_overwrites_a_block_committed_after_it_read_the_page(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = Page.objects.create(
        site=site, url="https://example.test/p", title="An ordinary studio scene title"
    )  # never scored, so the scan has a verdict to write
    add_media(page, "p", Media.Status.PENDING)
    block_while_scanning(monkeypatch, page)

    services.scan_pages(safety_terms=[])

    page.refresh_from_db()
    assert page.safety_status == Page.SafetyStatus.BLOCKED
    assert page.safety_matches == [{"term": "x", "category": "minor", "action": "block"}]
    assert page.safety_score == 0.0
    assert set(page.media.values_list("status", "eligible_at")) == {(Media.Status.SKIPPED, None)}


def test_scan_never_releases_media_of_a_page_blocked_after_it_read_the_page(
    make_page: MakePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan's own verdict is unchanged, so it writes nothing, but the page was blocked since
    it was read: the media gate must follow the page as it is now, not as the scan saw it."""
    page = make_page("scored")
    services.scan_pages(safety_terms=[])
    assert Page.objects.get(pk=page.pk).safety_score_reason  # scored: a rescan changes nothing
    add_media(page, "later", Media.Status.PENDING)  # attached while gated: no eligibility
    block_while_scanning(monkeypatch, page)

    report = services.scan_pages(safety_terms=[])

    page.refresh_from_db()
    assert page.safety_status == Page.SafetyStatus.BLOCKED
    assert set(page.media.values_list("status", "eligible_at")) == {(Media.Status.SKIPPED, None)}
    assert (report.media_released, report.media_eligible) == (0, 0)
