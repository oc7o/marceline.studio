from datetime import UTC, datetime, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching import triage
from matching.capture_triage import (
    MAX_CAPTURE_ATTEMPTS,
    GateSignals,
    RefreshResult,
    collect_candidates,
    next_capture_batch,
    refresh_candidates,
)
from matching.models import (
    BrowserCaptureCandidate,
    Cluster,
    ClusterMember,
    Reference,
    ReferenceLeak,
    ReferenceMatch,
)

IN_WINDOW = datetime(2026, 9, 16, 2, 0, tzinfo=UTC)
CAPTURE_ON = {"BROWSER_CAPTURE_ENABLED": True, "BROWSER_CAPTURE_WINDOW_UTC": "01:00-05:00"}
CHECKED = GateSignals()


# --- helpers ---------------------------------------------------------------------------------


@pytest.fixture
def creator(db: None) -> Creator:
    return Creator.objects.create(name="Synthetic Artist")


@pytest.fixture
def reference(creator: Creator) -> Reference:
    return Reference.objects.create(
        creator=creator, title="Original", duration_s=1000, sha256="a" * 64, frame_fps=1
    )


def make_page(
    site: Site,
    slug: str,
    *,
    duration: float | None = None,
    frames: int = 1,
    creator: Creator | None = None,
    media_status: str = "done",
    **fields: Any,
) -> Page:
    page = Page.objects.create(
        site=site,
        url=f"https://{site.domain}/videos/{slug}",
        title=slug,
        raw_extraction={"duration_s": duration} if duration is not None else {},
        **fields,
    )
    media = Media.objects.create(
        page=page, kind="poster", url=f"https://{site.domain}/{slug}.jpg", status=media_status
    )
    FrameHash.objects.bulk_create(
        FrameHash(media=media, frame_index=index, pdq=bytes(32), quality=100)
        for index in range(frames)
    )
    if creator is not None:
        page.creators.add(creator)
    return page


def make_match(reference: Reference, page: Page, **fields: Any) -> ReferenceMatch:
    values: dict[str, Any] = {
        "best_distance": 20,
        "matched_frames": 1,
        "page_frames": 1,
        "best_reference_timestamp_s": 10,
        "evidence_frames": 1,
        "confidence": 0.5,
        "band": "review",
        "strength": "weak",
        "status": "proposed",
    }
    values.update(fields)
    return ReferenceMatch.objects.create(reference=reference, page=page, **values)


def refresh(**options: Any) -> RefreshResult:
    options.setdefault("gate_signals", CHECKED)
    return refresh_candidates(timezone.now(), **options)


def queued() -> dict[int, BrowserCaptureCandidate]:
    return {row.page_id: row for row in BrowserCaptureCandidate.objects.filter(status="queued")}


def reasons(page: Page) -> list[str]:
    row = BrowserCaptureCandidate.objects.get(page=page, status="queued")
    return [source["reason"] for source in row.signals["sources"]]


# --- pure scoring ----------------------------------------------------------------------------


def test_review_component_rises_with_confidence_and_exact_duration() -> None:
    far = triage.review_match_component(0.40, 0.75, exact=False)
    near = triage.review_match_component(0.70, 0.75, exact=False)
    exact = triage.review_match_component(0.70, 0.75, exact=True)
    assert far < near < exact
    assert triage.review_match_component(0.0, 0.75, exact=False) == triage.REVIEW_MATCH_FLOOR
    assert triage.review_match_component(0.9, 0.75, exact=False) == triage.REVIEW_MATCH_WEIGHT
    assert triage.exact_duration(1001.5, 1000) and not triage.exact_duration(1001.6, 1000)
    assert not triage.exact_duration(None, 1000)


def test_combine_priority_leads_with_the_strongest_component() -> None:
    assert triage.combine_priority([], 0) == 0
    single = triage.combine_priority([0.7], triage.FEW_FRAME_HASHES)
    assert single == 0.7
    assert triage.combine_priority([0.4, 0.7], triage.FEW_FRAME_HASHES) == pytest.approx(
        0.7 + triage.SECONDARY_SOURCE_SHARE * 0.4
    )
    assert triage.combine_priority([0.7], 0) == pytest.approx(0.7 + triage.FRAME_POVERTY_WEIGHT)
    sources = [
        triage.Source(triage.LINKED_CLUSTER_MEMBER, 2, 0.6),
        triage.Source(triage.CONFIRMED_LEAK_WITHOUT_FRAMES, 1, 0.6),
    ]
    assert triage.leading(sources).reason == triage.CONFIRMED_LEAK_WITHOUT_FRAMES


def facts(**overrides: Any) -> triage.PageFacts:
    values: dict[str, Any] = {
        "safety_status": "clear",
        "safety_matches": [],
        "availability": "playable",
        "site_enabled": True,
        "site_age_approved": False,
        "url": "https://example.test/videos/1",
        "raw_extraction": {},
    }
    values.update(overrides)
    return triage.PageFacts(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, []),
        ({"safety_status": "review"}, [triage.SAFETY_NOT_CLEAR]),
        ({"safety_status": "blocked"}, [triage.SAFETY_NOT_CLEAR]),
        ({"safety_matches": [{"term": "x"}]}, [triage.SAFETY_MATCHES]),
        ({"availability": "removed"}, [triage.REMOVED]),
        ({"site_enabled": False}, [triage.SITE_DISABLED]),
        ({"site_capture_enabled": False}, [triage.SITE_CAPTURE_DISABLED]),
        ({"page_active": False}, [triage.PAGE_INACTIVE]),
        ({"site_gate": "age_self_declaration"}, [triage.AGE_GATE_UNAPPROVED]),
        ({"site_gate": "age_self_declaration", "site_age_approved": True}, []),
        (
            {"page_gate": "age_verification_provider", "site_age_approved": True},
            [triage.AGE_VERIFICATION],
        ),
        ({"url": "https://example.test/login?next=/videos/1"}, [triage.PRIVATE_PAGE]),
        ({"raw_extraction": {"login_required": True}}, [triage.PRIVATE_PAGE]),
        ({"media_pending": True}, [triage.MEDIA_PENDING]),
        (
            {"safety_status": "review", "availability": "removed"},
            [triage.SAFETY_NOT_CLEAR, triage.REMOVED],
        ),
    ],
)
def test_page_exclusions(overrides: dict[str, Any], expected: list[str]) -> None:
    assert triage.page_exclusions(facts(**overrides)) == expected


def test_pair_exclusion() -> None:
    assert triage.pair_exclusion(rejected=True, confirmed=True, band="high", evidence_frames=9) == (
        triage.REJECTED_PAIR
    )
    enough = triage.CONFIRMED_HIGH_MIN_EVIDENCE
    assert triage.pair_exclusion(
        rejected=False, confirmed=True, band="high", evidence_frames=enough
    ) == (triage.CONFIRMED_HIGH)
    assert (
        triage.pair_exclusion(
            rejected=False, confirmed=True, band="high", evidence_frames=enough - 1
        )
        is None
    )
    assert (
        triage.pair_exclusion(rejected=False, confirmed=False, band="high", evidence_frames=9)
        is None
    )
    assert (
        triage.pair_exclusion(rejected=False, confirmed=True, band="review", evidence_frames=9)
        is None
    )


def test_changed_substantially() -> None:
    basis = triage.capture_basis("https://www.Example.test/videos/1/?t=1", 1000, ["a", "b"])
    assert basis["url"] == "example.test/videos/1"
    same = triage.capture_basis("http://example.test/videos/1", 1020, ["b", "c"])
    assert triage.changed_substantially(basis, same) is None
    assert triage.changed_substantially({}, same) is None
    moved = triage.capture_basis("https://example.test/videos/2", 1000, ["a"])
    assert triage.changed_substantially(basis, moved) == "url_changed"
    longer = triage.capture_basis("https://example.test/videos/1", 1100, ["a"])
    assert triage.changed_substantially(basis, longer) == "duration_changed"
    unknown = triage.capture_basis("https://example.test/videos/1", None, [])
    assert triage.changed_substantially(basis, unknown) is None
    replaced = triage.capture_basis("https://example.test/videos/1", 1000, ["x"])
    assert triage.changed_substantially(basis, replaced) == "media_replaced"


def test_window_parsing_and_bounds() -> None:
    window = triage.parse_window("01:00-05:00")
    assert (window.start, window.end) == (time(1), time(5))
    assert str(window) == "01:00-05:00"
    day = datetime(2026, 9, 16, tzinfo=UTC)
    assert triage.window_bounds(day.replace(hour=2), window) == (
        day.replace(hour=1),
        day.replace(hour=5),
        True,
    )
    assert triage.window_bounds(day.replace(hour=0, minute=30), window)[2] is False
    start, _, inside = triage.window_bounds(day.replace(hour=5), window)
    assert (start, inside) == (day + timedelta(days=1, hours=1), False)

    overnight = triage.parse_window("22:00-02:00")
    assert triage.window_bounds(day.replace(hour=1), overnight) == (
        day - timedelta(hours=2),
        day + timedelta(hours=2),
        True,
    )
    assert triage.window_bounds(day.replace(hour=23), overnight)[2] is True
    for invalid in ("", "01:00", "01:00-01:00", "1-x"):
        with pytest.raises(ValueError, match="capture window"):
            triage.parse_window(invalid)


def test_pick_batch_respects_total_site_and_concurrency_budgets() -> None:
    items = [triage.QueuedItem(pk, site) for pk, site in ((1, 10), (2, 10), (3, 20), (4, 30))]
    common: dict[str, Any] = {"per_site_budget": 1, "used_total": 0, "used_per_site": {}}
    assert triage.pick_batch(items, total_budget=20, max_concurrency=1, **common) == [1]
    assert triage.pick_batch(items, total_budget=20, max_concurrency=9, **common) == [1, 3, 4]
    assert triage.pick_batch(
        items,
        total_budget=3,
        per_site_budget=5,
        max_concurrency=9,
        used_total=1,
        used_per_site={10: 1},
    ) == [1, 2]
    assert (
        triage.pick_batch(
            items,
            total_budget=20,
            per_site_budget=1,
            max_concurrency=9,
            used_total=0,
            used_per_site={10: 1, 20: 1, 30: 1},
        )
        == []
    )


# --- sources ---------------------------------------------------------------------------------


def test_review_band_match_source(site: Site, reference: Reference) -> None:
    review = make_page(site, "review", duration=5000)
    weak_low = make_page(site, "weak-low", duration=5000)
    exact = make_page(site, "exact", duration=1000.5)
    high = make_page(site, "high", duration=5000)
    strong_low = make_page(site, "strong-low", duration=5000)
    make_match(reference, review, confidence=0.6)
    make_match(reference, weak_low, confidence=0.2, band="low")
    make_match(reference, exact, confidence=0.6)
    make_match(reference, high, confidence=0.9, band="high", strength="strong", evidence_frames=4)
    make_match(reference, strong_low, confidence=0.3, band="low", strength="strong")

    refresh()

    rows = queued()
    assert set(rows) == {review.pk, weak_low.pk, exact.pk}
    assert {row.reason for row in rows.values()} == {triage.REVIEW_BAND_MATCH}
    assert rows[exact.pk].priority > rows[review.pk].priority > rows[weak_low.pk].priority
    assert rows[review.pk].reference_id == reference.pk
    assert rows[exact.pk].signals["sources"][0]["exact_duration"] is True


def test_confirmed_leak_without_frames_source(site: Site, reference: Reference) -> None:
    poster = make_page(site, "poster", frames=1)
    rich = make_page(site, "rich", frames=triage.FEW_FRAME_HASHES)
    confirmed_match = make_page(site, "confirmed-match", frames=2)
    ReferenceLeak.objects.create(reference=reference, page=poster, status="confirmed")
    ReferenceLeak.objects.create(reference=reference, page=rich, status="confirmed")
    make_match(
        reference,
        confirmed_match,
        status="confirmed",
        band="high",
        strength="strong",
        evidence_frames=2,
        confidence=0.8,
    )

    refresh()

    rows = queued()
    assert set(rows) == {poster.pk, confirmed_match.pk}
    assert rows[poster.pk].reason == triage.CONFIRMED_LEAK_WITHOUT_FRAMES
    assert rows[poster.pk].signals["sources"][0]["confirmed_in"] == ["leak"]
    assert rows[confirmed_match.pk].signals["sources"][0]["confirmed_in"] == ["match"]


def test_linked_cluster_member_source(site: Site, reference: Reference) -> None:
    seed = make_page(site, "seed", frames=12)
    member = make_page(site, "member", frames=1)
    own_match = make_page(site, "own-match", frames=1)
    make_match(reference, seed, band="high", strength="strong", evidence_frames=3, confidence=0.9)
    make_match(reference, own_match, band="low", confidence=0.1)
    cluster = Cluster.objects.create()
    for page in (seed, member, own_match):
        ClusterMember.objects.create(cluster=cluster, page=page, evidence="pdq")

    refresh()

    rows = queued()
    assert set(rows) == {member.pk, own_match.pk}
    assert reasons(own_match) == [triage.REVIEW_BAND_MATCH]  # own match: no cluster source
    assert rows[member.pk].reason == triage.LINKED_CLUSTER_MEMBER
    assert rows[member.pk].signals["sources"][0] | {"component": 0} == {
        "reason": triage.LINKED_CLUSTER_MEMBER,
        "reference_id": reference.pk,
        "component": 0,
        "seed_page_id": seed.pk,
        "kind": "cluster",
    }

    # A human-rejected seed propagates nothing.
    ReferenceMatch.objects.filter(page=seed).update(status="rejected")
    result = refresh()
    assert set(queued()) == {own_match.pk}
    assert result.counts["expired"] == 1


def test_exact_duration_single_thumb_source(
    site: Site, reference: Reference, creator: Creator
) -> None:
    single = make_page(site, "single", duration=1001, frames=1, creator=creator)
    no_frames = make_page(site, "no-frames", duration=999, frames=0, creator=creator)
    make_page(site, "off-by-two", duration=1002, frames=1, creator=creator)
    make_page(site, "two-frames", duration=1000, frames=2, creator=creator)
    make_page(site, "unlinked", duration=1000, frames=1)
    matched = make_page(site, "matched", duration=1000, frames=1, creator=creator)
    make_match(reference, matched, band="low", strength="strong", evidence_frames=2)

    refresh()

    rows = queued()
    assert set(rows) == {single.pk, no_frames.pk}
    assert rows[single.pk].reason == triage.EXACT_DURATION_SINGLE_THUMB
    assert rows[single.pk].signals["sources"][0]["duration_diff_s"] == 1.0
    assert rows[no_frames.pk].priority > rows[single.pk].priority  # fewer frames, more to gain


def test_priority_ordering_across_sources(
    site: Site, reference: Reference, creator: Creator
) -> None:
    # A page seen in production: review-band match, confirmed leak, exact duration, one poster.
    combined = make_page(site, "combined", duration=1000.4, frames=1, creator=creator)
    make_match(reference, combined, confidence=0.595)
    ReferenceLeak.objects.create(reference=reference, page=combined, status="confirmed")
    review = make_page(site, "review", duration=5000, frames=1)
    make_match(reference, review, confidence=0.475)
    confirmed = make_page(site, "confirmed", frames=5)
    ReferenceLeak.objects.create(reference=reference, page=confirmed, status="confirmed")
    duration_only = make_page(site, "duration", duration=1000, frames=1, creator=creator)

    result = collect_candidates(CHECKED)

    assert [candidate.page.pk for candidate in result.candidates] == [
        combined.pk,
        review.pk,
        confirmed.pk,
        duration_only.pk,
    ]
    top = result.candidates[0]
    assert top.lead.reason == triage.REVIEW_BAND_MATCH
    assert [source.reason for source in top.sources] == [
        triage.REVIEW_BAND_MATCH,
        triage.CONFIRMED_LEAK_WITHOUT_FRAMES,
    ]


# --- exclusions ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"safety_status": "review"}, triage.SAFETY_NOT_CLEAR),
        ({"safety_status": "blocked"}, triage.SAFETY_NOT_CLEAR),
        ({"safety_matches": [{"term": "x", "action": "review"}]}, triage.SAFETY_MATCHES),
        ({"availability": "removed"}, triage.REMOVED),
        ({"url": "https://example.test/members/video-1"}, triage.PRIVATE_PAGE),
    ],
)
def test_page_level_exclusions_are_never_queued(
    site: Site, reference: Reference, change: dict[str, Any], code: str
) -> None:
    page = make_page(site, "flagged")
    make_match(reference, page, confidence=0.7)
    ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")
    Page.objects.filter(pk=page.pk).update(**change)

    result = refresh()

    assert not BrowserCaptureCandidate.objects.exists()
    assert result.triage.skips == {code: 1}
    assert result.triage.excluded_pages == {page.pk: code}


def test_disabled_site_and_pending_media_are_excluded(site: Site, reference: Reference) -> None:
    pending = make_page(site, "pending", media_status="pending")
    make_match(reference, pending)
    other = Site.objects.create(domain="disabled.test", enabled=False)
    disabled = make_page(other, "disabled")
    make_match(reference, disabled)

    result = refresh()

    assert not BrowserCaptureCandidate.objects.exists()
    assert result.triage.skips == {triage.MEDIA_PENDING: 1, triage.SITE_DISABLED: 1}


def test_capture_disabled_site_is_excluded_but_still_crawled(
    site: Site, reference: Reference
) -> None:
    """`Site.capture_enabled = False` (issue #78) keeps a site's pages out of the capture queue
    without touching `Site.enabled` — the site is still crawled, only never visited by a browser.
    """
    excluded = Site.objects.create(domain="no-capture.test", capture_enabled=False)
    page = make_page(excluded, "no-capture")
    make_match(reference, page)

    result = refresh()

    assert excluded.enabled is True
    assert not BrowserCaptureCandidate.objects.exists()
    assert result.triage.skips == {triage.SITE_CAPTURE_DISABLED: 1}
    assert result.triage.excluded_pages == {page.pk: triage.SITE_CAPTURE_DISABLED}


def test_age_gates(site: Site, reference: Reference) -> None:
    page = make_page(site, "gated")
    make_match(reference, page)

    unapproved = GateSignals(sites={site.pk: triage.GATE_SELF_DECLARATION})
    result = refresh(gate_signals=unapproved, dry_run=True)
    assert result.triage.skips == {triage.AGE_GATE_UNAPPROVED: 1}

    site.age_confirmation = {
        "cookies": {"age_ok": "1"},
        "approved_by": "operator",
        "approved_at": "2026-09-15",
    }
    site.save()
    assert refresh(gate_signals=unapproved).counts["created"] == 1

    provider = GateSignals(pages={page.pk: "age_verification_provider"})
    result = refresh(gate_signals=provider)
    assert result.triage.skips == {triage.AGE_VERIFICATION: 1}
    row = BrowserCaptureCandidate.objects.get(page=page)
    assert (row.status, row.last_error) == ("skipped", "triage: age_verification_provider")


def test_rejected_pairs_are_excluded_per_reference(
    site: Site, reference: Reference, creator: Creator
) -> None:
    second = Reference.objects.create(
        creator=creator, title="Second", duration_s=2000, sha256="b" * 64, frame_fps=1
    )
    page = make_page(site, "page")
    make_match(reference, page, status="rejected")
    make_match(second, page)
    leak_only = make_page(site, "leak-only")
    ReferenceLeak.objects.create(reference=reference, page=leak_only, status="confirmed")
    make_match(reference, leak_only, status="rejected")

    result = refresh()

    rows = queued()
    assert set(rows) == {page.pk}
    assert rows[page.pk].reference_id == second.pk
    assert result.triage.skips == {triage.REJECTED_PAIR: 3}


def test_confirmed_high_pair_with_enough_evidence_is_excluded(
    site: Site, reference: Reference
) -> None:
    page = make_page(site, "done", frames=3)
    make_match(
        reference,
        page,
        status="confirmed",
        band="high",
        strength="strong",
        evidence_frames=triage.CONFIRMED_HIGH_MIN_EVIDENCE,
        confidence=0.9,
    )

    result = refresh()

    assert not BrowserCaptureCandidate.objects.exists()
    assert result.triage.skips == {triage.CONFIRMED_HIGH: 1}


def capture(row: BrowserCaptureCandidate, *, status: str = "captured") -> None:
    """What a future capture worker records: status, attempt, decision time, run summary."""
    row.status = status
    row.attempts += 1
    row.decided_at = timezone.now()
    row.capture_run = {"run": 1, "frames": 30}
    row.save()


def test_captured_page_is_scanned_once_until_it_changes(site: Site, reference: Reference) -> None:
    page = make_page(site, "page", duration=1000)
    make_match(reference, page)
    refresh()
    row = BrowserCaptureCandidate.objects.get(page=page)
    capture(row)
    captured_at = BrowserCaptureCandidate.objects.get(pk=row.pk).updated_at

    result = refresh()
    assert result.triage.skips == {triage.ALREADY_CAPTURED: 1}
    assert queued() == {}
    captured = BrowserCaptureCandidate.objects.get(pk=row.pk)
    assert (captured.status, captured.updated_at) == ("captured", captured_at)

    # Small duration drift is the same video; a different duration is another video.
    page.raw_extraction = {"duration_s": 1010}
    page.save()
    assert refresh().counts["created"] == 0
    page.raw_extraction = {"duration_s": 2000}
    page.save()
    assert refresh().counts["created"] == 1
    assert queued()[page.pk].signals["recapture"] == "duration_changed"
    assert BrowserCaptureCandidate.objects.get(pk=row.pk).status == "captured"


def test_worker_skip_counts_as_scanned_but_triage_skip_does_not(
    site: Site, reference: Reference
) -> None:
    worker = make_page(site, "drm")
    make_match(reference, worker)
    refresh()
    capture(BrowserCaptureCandidate.objects.get(page=worker), status="skipped")

    triaged = make_page(site, "flag-later")
    make_match(reference, triaged)
    refresh()
    Page.objects.filter(pk=triaged.pk).update(safety_status="review")
    refresh()
    assert BrowserCaptureCandidate.objects.get(page=triaged).status == "skipped"
    Page.objects.filter(pk=triaged.pk).update(safety_status="clear")

    result = refresh()

    assert set(queued()) == {triaged.pk}
    assert result.triage.skips == {triage.ALREADY_CAPTURED: 1}


# --- refresh ---------------------------------------------------------------------------------


def test_refresh_is_idempotent_updates_skips_and_expires(site: Site, reference: Reference) -> None:
    kept = make_page(site, "kept")
    gone = make_page(site, "gone")
    flagged = make_page(site, "flagged")
    kept_match = make_match(reference, kept, confidence=0.5)
    gone_match = make_match(reference, gone)
    make_match(reference, flagged)

    first = refresh()
    assert first.counts == {"created": 3}
    second = refresh()
    assert second.counts == {"unchanged": 3}

    ReferenceMatch.objects.filter(pk=kept_match.pk).update(confidence=0.7)
    gone_match.delete()
    Page.objects.filter(pk=flagged.pk).update(safety_status="blocked")
    before = queued()[kept.pk].priority
    third = refresh(dry_run=True)
    assert third.counts == {"updated": 1, "expired": 1, "skipped": 1}
    assert set(queued()) == {kept.pk, gone.pk, flagged.pk}  # dry run wrote nothing

    now = timezone.now()
    refresh_candidates(now, gate_signals=CHECKED)
    rows = {row.page_id: row for row in BrowserCaptureCandidate.objects.all()}
    assert rows[kept.pk].status == "queued" and rows[kept.pk].priority > before
    assert (rows[gone.pk].status, rows[gone.pk].last_error, rows[gone.pk].decided_at) == (
        "expired",
        "triage: no capture source left",
        now,
    )
    assert (rows[flagged.pk].status, rows[flagged.pk].last_error) == (
        "skipped",
        "triage: safety_not_clear",
    )
    assert refresh().counts == {"unchanged": 1}
    assert BrowserCaptureCandidate.objects.count() == 3


def test_failed_rows_are_requeued_until_max_attempts(site: Site, reference: Reference) -> None:
    page = make_page(site, "page")
    make_match(reference, page)
    refresh()
    row = BrowserCaptureCandidate.objects.get(page=page)
    for attempt in range(1, MAX_CAPTURE_ATTEMPTS + 1):
        capture(row, status="failed")
        result = refresh()
        row.refresh_from_db()
        if attempt < MAX_CAPTURE_ATTEMPTS:
            assert (result.counts["requeued"], row.status) == (1, "queued")
        else:
            assert (result.counts["unchanged"], row.status) == (1, "failed")
    assert BrowserCaptureCandidate.objects.count() == 1


def test_limit_keeps_the_best_candidates(site: Site, reference: Reference) -> None:
    best = make_page(site, "best")
    worse = make_page(site, "worse")
    make_match(reference, best, confidence=0.7)
    make_match(reference, worse, confidence=0.1, band="low")
    refresh()

    result = refresh(limit=1)

    assert result.counts == {"unchanged": 1, "expired": 1}
    assert set(queued()) == {best.pk}
    assert BrowserCaptureCandidate.objects.get(page=worse).last_error == "triage: below --limit"
    with pytest.raises(ValueError, match="positive"):
        refresh(limit=0)


def test_one_open_candidate_per_page(site: Site, reference: Reference) -> None:
    page = make_page(site, "page")
    values: dict[str, Any] = {"page": page, "site": site, "reason": triage.REVIEW_BAND_MATCH}
    BrowserCaptureCandidate.objects.create(status="captured", **values)
    BrowserCaptureCandidate.objects.create(status="queued", **values)
    for status in ("queued", "failed"):
        with transaction.atomic(), pytest.raises(IntegrityError):
            BrowserCaptureCandidate.objects.create(status=status, **values)
    BrowserCaptureCandidate.objects.create(status="expired", **values)


def test_command_dry_run_prints_counts_top_and_budget(site: Site, reference: Reference) -> None:
    page = make_page(site, "page")
    make_match(reference, page)
    blocked = make_page(site, "blocked", safety_status="blocked")
    make_match(reference, blocked)
    out = StringIO()

    call_command("triage_browser_capture", "--dry-run", stdout=out)

    text = out.getvalue()
    assert not BrowserCaptureCandidate.objects.exists()
    assert "Browser capture triage (dry run): 1 candidates; created 1," in text
    assert "Reasons (leading/sources): review_band_match=1/1," in text
    assert "Exclusions: safety_not_clear=1" in text
    assert "Gate signals: not supplied" in text
    assert f"  page {page.pk} example.test review_band_match " in text
    assert "Budget: capture disabled; " in text
    assert "visits 0/20, 5 per site, concurrency 1; 0 queued" in text

    call_command("triage_browser_capture", "--limit", "5", stdout=StringIO())
    assert BrowserCaptureCandidate.objects.get().signals["gate_checked"] is False


# --- budgets and scheduling of captures ------------------------------------------------------


def queue_pages(site: Site, reference: Reference, count: int, **options: Any) -> list[Page]:
    pages = [make_page(site, f"{site.domain}-{index}") for index in range(count)]
    for index, page in enumerate(pages):
        make_match(reference, page, confidence=0.7 - index * 0.1)
    refresh(**options)
    return pages


def test_next_capture_batch_is_empty_when_disabled_or_outside_window(
    site: Site, reference: Reference
) -> None:
    queue_pages(site, reference, 1)
    assert next_capture_batch(IN_WINDOW) == []
    with override_settings(**CAPTURE_ON):
        assert len(next_capture_batch(IN_WINDOW)) == 1
        assert next_capture_batch(IN_WINDOW.replace(hour=6)) == []
    with (
        override_settings(**{**CAPTURE_ON, "BROWSER_CAPTURE_WINDOW_UTC": "nonsense"}),
        pytest.raises(ValueError, match="capture window"),
    ):
        next_capture_batch(IN_WINDOW)


@override_settings(
    **CAPTURE_ON,
    BROWSER_CAPTURE_MAX_CONCURRENCY=10,
    BROWSER_CAPTURE_PER_SITE_PER_DAY=2,
    BROWSER_CAPTURE_TOTAL_PER_DAY=3,
)
def test_next_capture_batch_respects_budgets_and_priority(site: Site, reference: Reference) -> None:
    first = queue_pages(site, reference, 3)
    other = Site.objects.create(domain="other.test")
    second = queue_pages(other, reference, 2)

    batch = next_capture_batch(IN_WINDOW)
    assert [row.page_id for row in batch] == [first[0].pk, second[0].pk, first[1].pk]

    # Two visits on example.test earlier in this window use its budget and part of the total.
    for row in (batch[0], batch[2]):
        row.attempts, row.decided_at, row.status = 1, IN_WINDOW - timedelta(minutes=30), "captured"
        row.save()
    assert [row.page_id for row in next_capture_batch(IN_WINDOW)] == [second[0].pk]
    # Visits of yesterday's window do not count.
    assert len(next_capture_batch(IN_WINDOW + timedelta(days=1))) == 3

    with override_settings(BROWSER_CAPTURE_MAX_CONCURRENCY=1):
        assert len(next_capture_batch(IN_WINDOW + timedelta(days=1))) == 1


@override_settings(**CAPTURE_ON, BROWSER_CAPTURE_MAX_CONCURRENCY=10)
def test_next_capture_batch_rechecks_safety_and_gate_check(
    site: Site, reference: Reference
) -> None:
    pages = queue_pages(site, reference, 3)
    unchecked = make_page(site, "unchecked")
    make_match(reference, unchecked, confidence=0.1, band="low")
    refresh_candidates(timezone.now(), limit=None)  # manual run: no gate signals supplied
    assert BrowserCaptureCandidate.objects.get(page=unchecked).signals["gate_checked"] is False

    Page.objects.filter(pk=pages[0].pk).update(safety_status="review")
    Page.objects.filter(pk=pages[1].pk).update(availability="removed")

    assert [row.page_id for row in next_capture_batch(IN_WINDOW)] == []
    refresh()
    assert [row.page_id for row in next_capture_batch(IN_WINDOW)] == [pages[2].pk, unchecked.pk]
