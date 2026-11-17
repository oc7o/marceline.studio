from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from catalog import request_accounting
from catalog.models import Creator, Site
from core.models import JobRun
from crawler.models import CrawlTask, SourceCandidate
from index.models import Media, Page
from marceline.review.m2_summary import M2ParseError, m2_summary, parse_m2_lines
from matching.models import Cluster, ClusterMember, Reference, ReferenceLeak, ReferenceMatch
from matching.references import STRICT_MAX_DISTANCE

OVERVIEW = "/api/v1/review/overview"


def make_match(reference: Reference, page: Page, **fields: Any) -> ReferenceMatch:
    values: dict[str, Any] = {
        "best_distance": 10,
        "matched_frames": 3,
        "page_frames": 5,
        "best_reference_timestamp_s": 12.0,
        "evidence_frames": 2,
        "strength": "strong",
        **fields,
    }
    return ReferenceMatch.objects.create(reference=reference, page=page, **values)


def test_requires_staff(client: Client, user_client: Client) -> None:
    assert client.get(OVERVIEW).status_code == 401
    assert user_client.get(OVERVIEW).status_code == 403


def test_site_numbers(staff_client: Client, site: Site) -> None:
    now = timezone.now()
    old = now - timedelta(hours=30)
    Site.objects.create(domain="disabled.test", enabled=False)
    fresh = Page.objects.create(site=site, url="https://example.test/a", availability="playable")
    stale = Page.objects.create(site=site, url="https://example.test/b", availability="removed")
    Page.objects.create(site=site, url="https://example.test/c")
    Page.objects.filter(pk=stale.pk).update(first_seen=old)

    def task(slug: str, status: str, **fields: Any) -> CrawlTask:
        return CrawlTask.objects.create(
            site=site, url=f"https://example.test/t/{slug}", kind="detail", status=status, **fields
        )

    task("p1", "pending")
    task("p2", "pending")
    task("d1", "done", fetched_at=now - timedelta(minutes=5))
    old_done = task("d2", "done", fetched_at=now - timedelta(hours=2))
    task("f1", "failed", fetched_at=old)
    CrawlTask.objects.filter(pk=old_done.pk).update(updated_at=now - timedelta(hours=2))

    Media.objects.create(page=fresh, kind="poster", url="https://cdn.test/1.jpg")
    Media.objects.create(
        page=fresh, kind="frame", url="https://cdn.test/2.jpg", status="done", fetched_at=now
    )
    Media.objects.create(
        page=stale, kind="poster", url="https://cdn.test/3.jpg", status="done", fetched_at=old
    )
    Media.objects.create(page=stale, kind="frame", url="https://cdn.test/4.jpg", status="skipped")
    Media.objects.create(
        page=stale, kind="frame", url="https://cdn.test/5.jpg", status="robots_blocked"
    )
    # Real requests (issue #74): origin pages and robots.txt; refusals, media and old hours not.
    hour = request_accounting.hour_floor(now)
    day_before = hour - timedelta(hours=30)
    request_accounting.upsert_counts(
        {
            (hour, site.pk, "example.test", "origin", "page", "page", "2xx"): [1, 0],
            (hour, site.pk, "example.test", "origin", "robots", "robots", "4xx"): [1, 0],
            (hour, site.pk, "example.test", "origin", "page", "page", "refused"): [4, 0],
            (hour, site.pk, "example.test", "origin", "media", "media", "2xx"): [6, 0],
            (hour, site.pk, "cdn.test", "cdn", "media", "media", "2xx"): [9, 0],
            (day_before, site.pk, "example.test", "origin", "page", "page", "2xx"): [5, 0],
        }
    )

    response = staff_client.get(OVERVIEW)

    assert response.status_code == 200
    [row] = response.json()["sites"]
    assert row == {
        "id": site.pk,
        "domain": "example.test",
        "name": "Example",
        "pages_total": 3,
        "pages_new_24h": 2,
        "tasks_pending": 2,
        "tasks_done_1h": 1,
        "tasks_failed_1h": 1,
        "media_pending": 1,
        "media_done_1h": 1,
        "media_skipped": 2,  # robots_blocked counts as skipped
        "availability": {"playable": 1, "removed": 1, "unknown": 1},
        "requests_24h": 2,
        "daily_request_budget": 1000,
    }


def test_jobs_and_review_work(staff_client: Client, reference: Reference, site: Site) -> None:
    finished = timezone.now()
    JobRun.objects.create(
        name="match_references", last_status="failed", last_finished_at=finished, last_message="x"
    )
    page = Page.objects.create(site=site, url="https://example.test/a")
    other = Page.objects.create(site=site, url="https://example.test/b")
    ReferenceLeak.objects.create(reference=reference, page=page)
    make_match(reference, page)
    make_match(reference, other, status="confirmed")
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=page, evidence="pdq")
    Page.objects.create(site=site, url="https://example.test/flagged", safety_status="review")
    Page.objects.create(site=site, url="https://example.test/gone", safety_status="blocked")
    SourceCandidate.objects.create(domain="new.test")
    SourceCandidate.objects.create(domain="old.test", status="rejected")

    data = staff_client.get(OVERVIEW).json()

    [job] = data["jobs"]
    assert job["name"] == "match_references"
    assert job["last_status"] == "failed"
    assert job["summary"] == "x"
    assert job["last_finished_at"] is not None
    assert data["review"] == {
        "reference_pairs": 1,
        "reference_leaks": 1,
        "reference_matches": 1,
        "clusters": 1,
        "safety": 1,
        "safety_blocked": 1,
        "source_candidates": 1,
    }


def test_m2_numbers(staff_client: Client, reference: Reference, site: Site) -> None:
    strong = Page.objects.create(site=site, url="https://example.test/strong")
    missed = Page.objects.create(site=site, url="https://example.test/missed")
    wrong = Page.objects.create(site=site, url="https://example.test/wrong")
    make_match(reference, strong, status="confirmed")
    make_match(reference, wrong, status="rejected", strength="weak")
    ReferenceLeak.objects.create(reference=reference, page=missed, status="confirmed")

    m2 = staff_client.get(OVERVIEW).json()["m2"]

    assert m2 == {
        "confirmed": 2,
        "found_strong": 1,
        "found_weak": 0,
        "found_linked": 0,
        "recall_strong": 0.5,
        "recall_strong_weak": 0.5,
        "recall_strong_weak_linked": 0.5,
        "false_matches": 1,
        "reviewed_matches": 2,
        "false_match_rate": 0.5,
        "unreviewed_suggestions": 0,
        "unreviewed_matches": 0,
        "passed": False,
        "too_few_to_judge": True,
        "recall_target": 0.7,
        "false_match_limit": 0.05,
        "max_distance": STRICT_MAX_DISTANCE,
    }


def test_m2_empty(staff_client: Client, db: None) -> None:
    m2 = staff_client.get(OVERVIEW).json()["m2"]
    assert m2["confirmed"] == 0
    assert m2["recall_strong"] is None
    assert m2["false_match_rate"] is None
    assert m2["passed"] is False


def test_m2_summary_parses_linked_and_misses(db: None, site: Site) -> None:
    creator = Creator.objects.create(name="Parser Artist")
    reference = Reference.objects.create(
        creator=creator, title="Title: with colon", sha256="b" * 64, frame_fps=1
    )
    seed = Page.objects.create(site=site, url="https://example.test/seed")
    mirror = Page.objects.create(site=site, url="https://example.test/mirror")
    missed = Page.objects.create(site=site, url="https://example.test/missed")
    make_match(reference, seed, status="confirmed")
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=seed, evidence="pdq")
    ClusterMember.objects.create(cluster=cluster, page=mirror, evidence="pdq")
    for page in (mirror, missed):
        ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")

    result = m2_summary()

    numbers = result.references[reference.pk]
    assert (numbers.numbers.confirmed, numbers.numbers.strong, numbers.numbers.linked) == (3, 1, 1)
    assert [(row.page_id, row.cluster_id, row.via_page_id) for row in numbers.linked] == [
        (mirror.pk, cluster.pk, seed.pk)
    ]
    [miss] = numbers.misses
    assert miss.page_id == missed.pk
    assert miss.reason.startswith("no FrameHash rows")
    assert result.total.confirmed == 3


def test_m2_parser_rejects_unknown_lines() -> None:
    with pytest.raises(M2ParseError):
        parse_m2_lines(["M2: header", "something new"], max_distance=31)
    with pytest.raises(M2ParseError):
        parse_m2_lines(["M2: header"], max_distance=31)
