"""Safety rule actions of 2026-09-17 end to end: incidents, the customer release queue, the API,
and `safety_scan --dry-run` (docs/safety.md)."""

import json
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from catalog.models import Creator, Site, WatchlistEntry
from crawler import services
from crawler.models import CrawlTask
from crawler.sites.base import MediaSpec, PageData, ParseResult
from index.models import FrameHash, Media, Page, SafetyIncident, SafetyRuleDecision
from index.services import page_media_allowed
from marceline import safety_rules
from marceline.safety_incidents import sync_incidents
from marceline.tests.app_helpers import make_organisation, own
from matching.creators import set_creators
from matching.models import Reference

pytestmark = pytest.mark.django_db

BLOCKED = Page.SafetyStatus.BLOCKED
CLEAR = Page.SafetyStatus.CLEAR
Release = Page.SafetyRelease
Kind = SafetyRuleDecision.Kind
QUEUE_API = "/api/v1/review/safety/customer-release"
FLAGGED_TERM_TITLE = "Alice Customer flagged term uniform scene with a long title"

Capture = Callable[..., Any]


@pytest.fixture
def customer(db: None) -> Creator:
    """Alice is a rights holder of an organisation-owned original; Bob only co-performs there."""
    alice = Creator.objects.create(name="Alice Customer")
    bob = Creator.objects.create(name="Bob Costar")
    reference = Reference.objects.create(
        creator=alice, title="original", sha256="a" * 64, frame_fps=1
    )
    set_creators(reference, [alice, bob])
    own(reference, make_organisation("Studio"))
    return alice


def store(
    site: Site,
    capture: Capture,
    title: str,
    *,
    slug: str = "v1",
    tags: tuple[str, ...] = ("vlog",),
    performers: tuple[str, ...] = (),
    media: bool = False,
) -> Page:
    url = f"https://example.test/{slug}"
    task, _ = CrawlTask.objects.get_or_create(
        url=url, defaults={"site": site, "kind": CrawlTask.Kind.DETAIL}
    )
    result = ParseResult(
        page=PageData(url=url, title=title, tags=tags, performers=performers),
        media=(MediaSpec(Media.Kind.POSTER, f"https://cdn.example.test/{slug}.jpg"),)
        if media
        else (),
    )
    with capture(execute=True):
        page = services.store_parse_result(task, result)
    assert page is not None
    page.refresh_from_db()
    return page


def post(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


# --- incidents ---------------------------------------------------------------------------------


def test_lolita_blocks_and_opens_one_incident(
    site: Site, django_capture_on_commit_callbacks: Capture
) -> None:
    page = store(site, django_capture_on_commit_callbacks, "Lolita style dress up video")

    assert page.safety_status == BLOCKED
    [incident] = SafetyIncident.objects.all()
    assert incident.status == SafetyIncident.Status.OPEN
    assert (incident.category, incident.term) == ("minor", "lolita")
    assert list(incident.incident_pages.values_list("page_id", flat=True)) == [page.pk]
    assert Kind.AUTO_BLOCK_AND_INCIDENT in page.safety_decisions.values_list("kind", flat=True)

    store(site, django_capture_on_commit_callbacks, "Lolita style dress up video")  # re-crawl
    assert SafetyIncident.objects.count() == 1
    assert incident.incident_pages.count() == 1


def test_non_consent_auto_block_opens_no_incident(
    site: Site, django_capture_on_commit_callbacks: Capture
) -> None:
    page = store(site, django_capture_on_commit_callbacks, "Forced office video")

    assert page.safety_status == BLOCKED
    assert not SafetyIncident.objects.exists()
    report = sync_incidents()
    assert (report.created, report.not_reportable) == (0, 1)


def test_manual_blocks_still_become_incidents(site: Site) -> None:
    Page.objects.create(
        site=site, url="https://example.test/manual", title="t", safety_status=BLOCKED
    )
    assert sync_incidents().created == 1


# --- customer release queue --------------------------------------------------------------------


def test_flagged_term_crediting_a_customer_is_queued(
    site: Site, customer: Creator, django_capture_on_commit_callbacks: Capture
) -> None:
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE)

    assert page.safety_status == BLOCKED
    assert page.safety_release == Release.QUEUED
    assert page.safety_release_creator == customer
    assert not page.creators.exists()
    assert not SafetyIncident.objects.exists()
    kinds = set(page.safety_decisions.values_list("kind", flat=True))
    assert kinds == {Kind.RELEASE_HOLD, Kind.QUEUED}


@pytest.mark.parametrize(
    ("title", "performers"),
    [
        ("Flagged term uniform scene with a long title", ()),  # no credit at all
        ("Flagged term uniform scene with a long title", ("Bob Costar",)),  # co-performer only
        ("Flagged term uniform scene with a long title", ("Wendy Watch",)),  # watchlist, crawled
        (f"{FLAGGED_TERM_TITLE} hidden cam", ()),  # customer credited, but another safety hit
        (f"{FLAGGED_TERM_TITLE} forced", ()),
    ],
)
def test_flagged_term_without_customer_credit_stays_blocked_unqueued(
    site: Site,
    customer: Creator,
    django_capture_on_commit_callbacks: Capture,
    title: str,
    performers: tuple[str, ...],
) -> None:
    wendy = Creator.objects.create(name="Wendy Watch")
    WatchlistEntry.objects.create(creator=wendy, crawl=True)

    page = store(site, django_capture_on_commit_callbacks, title, performers=performers)

    assert page.safety_status == BLOCKED
    assert page.safety_release == Release.NONE
    assert safety_rules.queued_count() == 0


def test_release_lets_media_through_after_the_recrawl(
    site: Site,
    customer: Creator,
    staff_client: Client,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE, media=True)
    assert not Media.objects.filter(page=page).exists()
    assert not page_media_allowed(page.safety_status, page.safety_score)

    missing = post(staff_client, f"{QUEUE_API}/release", {"ids": [page.pk], "reason": " "})
    assert missing.status_code == 422
    with django_capture_on_commit_callbacks(execute=True):
        response = post(
            staff_client, f"{QUEUE_API}/release", {"ids": [page.pk], "reason": "studio outfit"}
        )

    assert response.status_code == 200, response.content
    page.refresh_from_db()
    assert (page.safety_status, page.safety_release) == (CLEAR, Release.RELEASED)
    assert page.safety_note == "studio outfit"
    assert list(page.creators.all()) == [customer]
    decision = page.safety_decisions.get(kind=Kind.RELEASED)
    assert (decision.decided_by, decision.creator, decision.note) == (
        "reviewer",
        customer,
        "studio outfit",
    )
    assert CrawlTask.objects.get(url=page.url).status == CrawlTask.Status.PENDING  # re-crawl
    assert page_media_allowed(page.safety_status, page.safety_score)

    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE, media=True)
    assert (page.safety_status, page.safety_release) == (CLEAR, Release.RELEASED)
    assert page_media_allowed(page.safety_status, page.safety_score)
    assert Media.objects.filter(page=page, eligible_at__isnull=False).count() == 1


@pytest.mark.parametrize("extra", ["hidden cam", "forced", "lolita"])
def test_another_safety_hit_on_recrawl_revokes_the_release(
    site: Site,
    customer: Creator,
    staff_client: Client,
    django_capture_on_commit_callbacks: Capture,
    extra: str,
) -> None:
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE, media=True)
    safety_rules.release_pages([page.pk], by="staff", note="ok")
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE, media=True)
    media = Media.objects.get(page=page)
    FrameHash.objects.create(media=media, frame_index=0, pdq=b"\x01" * 32, quality=90)

    page = store(
        site, django_capture_on_commit_callbacks, f"{FLAGGED_TERM_TITLE} {extra}", media=True
    )

    assert page.safety_status == BLOCKED
    assert page.safety_release == Release.REVOKED
    assert not page_media_allowed(page.safety_status, page.safety_score)
    assert Kind.RELEASE_REVOKED in page.safety_decisions.values_list("kind", flat=True)
    assert not FrameHash.objects.filter(media__page=page).exists()
    assert safety_rules.queued_count() == 0


def test_release_refuses_when_another_term_matches_now(
    site: Site,
    customer: Creator,
    staff_client: Client,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE)
    # The stored text changed without a re-judged verdict (e.g. a term edited since the crawl).
    Page.objects.filter(pk=page.pk).update(title=f"{FLAGGED_TERM_TITLE} hidden cam")

    response = post(staff_client, f"{QUEUE_API}/release", {"ids": [page.pk], "reason": "ok"})

    assert response.status_code == 409
    assert response.json()["conflicts"][0]["reason"] == "another safety term matches"
    page.refresh_from_db()
    assert (page.safety_status, page.safety_release) == (BLOCKED, Release.QUEUED)
    assert not page.safety_decisions.filter(kind=Kind.RELEASED).exists()


def test_keep_blocked_stands_on_later_crawls(
    site: Site,
    customer: Creator,
    staff_client: Client,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE)

    response = post(staff_client, f"{QUEUE_API}/keep-blocked", {"ids": [page.pk]})

    assert response.status_code == 200
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE)
    assert (page.safety_status, page.safety_release) == (BLOCKED, Release.KEPT_BLOCKED)
    assert page.safety_reviewed_at is not None
    assert safety_rules.sync_release_queue().staff_kept_blocked == 1
    assert safety_rules.queued_count() == 0
    again = post(staff_client, f"{QUEUE_API}/release", {"ids": [page.pk], "reason": "x"})
    assert again.status_code == 409


def test_queue_follows_ownership(
    site: Site, customer: Creator, django_capture_on_commit_callbacks: Capture
) -> None:
    page = store(site, django_capture_on_commit_callbacks, FLAGGED_TERM_TITLE)
    assert safety_rules.queued_count() == 1

    Reference.objects.all().delete()  # the customer's original is gone
    report = safety_rules.sync_release_queue()

    assert (report.uncredited, report.dequeued) == (1, 1)
    page.refresh_from_db()
    assert (page.safety_status, page.safety_release) == (BLOCKED, Release.NONE)


def test_queue_api_is_text_only(
    site: Site,
    customer: Creator,
    staff_client: Client,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    page = store(
        site,
        django_capture_on_commit_callbacks,
        FLAGGED_TERM_TITLE,
        performers=("Alice Customer",),
        media=True,
    )
    Media.objects.create(page=page, kind=Media.Kind.POSTER, url="https://cdn.example.test/x.jpg")

    response = staff_client.get(QUEUE_API)

    assert response.status_code == 200
    body = response.content.decode()
    for forbidden in ("media", "image", "http", "url", "poster", "thumb", "cdn.example.test"):
        assert forbidden not in body.lower()
    [item] = response.json()["items"]
    assert set(item) == {
        "id",
        "site",
        "title",
        "tags",
        "performers",
        "matches",
        "customer_credits",
        "flagged_at",
        "queued_at",
    }
    assert item["performers"] == ["Alice Customer"]
    assert item["customer_credits"] == [
        {
            "creator_id": customer.pk,
            "creator_name": "Alice Customer",
            "matched_in": ["performers", "title"],
        }
    ]
    assert {match["action"] for match in item["matches"]} == {"customer_release_queue"}
    assert staff_client.get("/api/v1/review/counts").json()["safety_release"] == 1
    assert staff_client.get("/api/v1/review/safety").json()["items"] == []


def test_queue_api_needs_staff(user_client: Client) -> None:
    assert user_client.get(QUEUE_API).status_code in (401, 403)


# --- retroactive: safety_scan ------------------------------------------------------------------


def old_review_page(site: Site, slug: str, title: str, term: str, category: str) -> Page:
    """A page stored under the rules before 2026-09-17: the term was a review term."""
    page = Page.objects.create(
        site=site,
        url=f"https://example.test/{slug}",
        title=title,
        safety_status=Page.SafetyStatus.REVIEW,
        safety_matches=[{"term": term, "category": category, "action": "review"}],
        safety_flagged_at=timezone.now(),
        safety_score=0.0,
    )
    Media.objects.create(page=page, kind=Media.Kind.POSTER, url=f"https://cdn.example.test/{slug}")
    return page


def scan(*args: str) -> str:
    out = StringIO()
    call_command("safety_scan", *args, stdout=out)
    return out.getvalue()


def test_safety_scan_dry_run_counts_per_rule(site: Site, customer: Creator) -> None:
    old_review_page(site, "r1", "Rape video title", "rape", "non_consent")
    old_review_page(site, "r2", "Forced video again", "forced", "non_consent")
    old_review_page(site, "l1", "Lolita costume party clip", "lolita", "minor")
    old_review_page(site, "s1", FLAGGED_TERM_TITLE, "flagged term", "minor")
    old_review_page(site, "s2", "Flagged term outfit nobody credited", "flagged term", "minor")
    Page.objects.create(site=site, url="https://example.test/ok", title="A clean title here")

    output = scan("--dry-run")

    assert "  auto_block: 2 blocked page(s), 2 to block by this scan (non_consent=2)" in output
    assert (
        "  auto_block_and_incident: 1 blocked page(s), 1 to block by this scan (minor=1)" in output
    )
    assert "    incidents to open: 1, folded into an existing case: 0" in output
    assert (
        "  customer_release_queue: 2 blocked page(s), 2 to block by this scan (minor=2)" in output
    )
    assert (
        "    queued for staff (customer credited): 1 (new: 1); kept blocked, no customer credit: 1;"
        in output
    )
    for secret in ("Rape video", "Lolita costume", "Alice", "example.test/", "http"):
        assert secret not in output
    # Nothing written.
    assert set(Page.objects.values_list("safety_status", flat=True)) == {"review", "clear"}
    assert not SafetyIncident.objects.exists()
    assert not SafetyRuleDecision.objects.exists()
    assert not Page.objects.exclude(safety_release="").exists()

    output = scan()

    assert "  auto_block_and_incident: 1 blocked page(s), 1 blocked by this scan" in output
    assert "    incidents opened: 1" in output
    assert SafetyIncident.objects.count() == 1
    assert Page.objects.filter(safety_status=BLOCKED).count() == 5
    assert safety_rules.queued_count() == 1
    assert not Page.objects.filter(safety_status=Page.SafetyStatus.REVIEW).exists()

    again = scan()
    assert "0 blocked by this scan" in again
    assert SafetyIncident.objects.count() == 1
    assert "incidents opened: 0, folded into an existing case: 1" in again
