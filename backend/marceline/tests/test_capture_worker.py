"""Browser capture (ADR 0016): the deny is a property of the visit, not of the queue.

Every test here asserts on the *browser*: a page that is blocked, under review, gated by its
score, never scored, removed, on a disabled site, or disallowed by robots.txt must not cause a
single browser visit — and not even a robots.txt request in the deny cases. The worker is driven
directly, bypassing the queue, so these fail even if a future change hands it a row the queue
would never have given it.

No real site and no real browser: the runner is a fake that answers the way Chromium would.
`test_capture_browser.py` runs the same worker against a real browser and a synthetic local page.
"""

import io
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import httpx
import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from catalog.models import Site
from core.http import PoliteClient
from crawler.tests.test_blocking import COOKIE_GATE, SELF_DECLARATION_FORM, YOTI_PAGE
from index.media_fetch import media_denial
from index.models import FrameHash, Media, Page
from index.services import SAFETY_BLOCK_REASON, SAFETY_GATE_REASON, SAFETY_REVIEW_REASON
from marceline.capture.click import NOT_OPTED_IN, ElementDescription
from marceline.capture.diagnosis import NO_PLAYER_AFTER_CLICK
from marceline.capture.guard import (
    CAPTURE_DISABLED_REASON,
    REMOVED_REASON,
    SITE_DISABLED_REASON,
    CaptureDenied,
    capture_page,
)
from marceline.capture.plan import CapturePlan
from marceline.capture.requests import ROBOTS_DISALLOWED, BrowserRequest
from marceline.capture.runner import (
    CapturedFrame,
    CaptureOutcome,
    CaptureRequest,
    CaptureStats,
    Decision,
    Loaded,
    RequestCheck,
    VideoState,
)
from marceline.capture.worker import CaptureReport, CaptureWorker, claim_candidate, refusal_report
from matching import media as queue
from matching.models import BrowserCaptureCandidate
from matching.tests.images import synthetic_image, to_bytes
from matching.timeline import Ordering, frame_position

ALLOW_ALL_ROBOTS = "User-agent: *\nAllow: /\n"
PAGE_URL = "https://example.test/videos/clip-1"
STATS = CaptureStats(
    wall_ms=4200, sample_ms=3100, bytes=5_000_000, requests=42, browser="chromium test"
)
Status = BrowserCaptureCandidate.Status


# --- doubles ---------------------------------------------------------------------------------


class Transport:
    """A loopback HTTP transport for robots.txt that records every request it is asked for."""

    def __init__(
        self, robots: str = ALLOW_ALL_ROBOTS, by_host: dict[str, httpx.Response] | None = None
    ) -> None:
        self.requested: list[str] = []
        self.robots = robots
        self.by_host = by_host or {}
        self.before_robots: Callable[[], None] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            if self.before_robots is not None:
                self.before_robots()
            if request.url.host in self.by_host:
                return self.by_host[request.url.host]
            return httpx.Response(200, text=self.robots)
        return httpx.Response(404)

    def client(self) -> PoliteClient:
        return PoliteClient(
            "MarcelineBot/0.1",
            max_retries=0,
            transport=httpx.MockTransport(self.handler),
            sleep=lambda _: None,
        )


@dataclass
class FakeBrowser:
    """Answers like a real browser and records every page it was pointed at."""

    loaded: Loaded
    visited: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    identical_frames: bool = False
    before_frames: Callable[[], None] | None = None
    # Requests the page makes while loading; each is put to the worker's policy first.
    page_requests: tuple[BrowserRequest, ...] = ()
    sent: list[str] = field(default_factory=list)
    requests_seen: list[CaptureRequest] = field(default_factory=list)
    # The page as it looks after the one allowed play click (issue #78); the indexes the worker
    # asked to be clicked land in `clicks`, which is what "one click per visit" is asserted on.
    after_click: Loaded | None = None
    clicks: list[int] = field(default_factory=list)

    def capture(
        self,
        request: CaptureRequest,
        decide: Callable[[Loaded], Decision],
        check: RequestCheck,
    ) -> CaptureOutcome:
        self.visited.append(request.url)
        self.requests_seen.append(request)
        for page_request in self.page_requests:
            if check(page_request) is None:
                self.sent.append(page_request.url)
        loaded = self.loaded
        decision = decide(loaded)
        self.decisions.append(decision)
        if decision.click_index is not None:
            # Exactly what `runner._talk` does: click once, look again, ask once more. No loop.
            self.clicks.append(decision.click_index)
            loaded = self.after_click or self.loaded
            decision = decide(loaded)
            self.decisions.append(decision)
        if decision.abort_reason:
            return CaptureOutcome(loaded=loaded, stats=STATS, skip_reason=decision.abort_reason)
        if self.before_frames is not None:
            self.before_frames()
        frames = tuple(
            CapturedFrame(
                position, to_bytes(synthetic_image(0 if self.identical_frames else index))
            )
            for index, position in enumerate(decision.positions)
        )
        return CaptureOutcome(loaded=loaded, frames=frames, stats=STATS)


_LINK = re.compile(r"<a\s[^>]*href", re.IGNORECASE)


def playable(
    *,
    html: str = "<html><body><video src='/clip.mp4'></video></body></html>",
    url: str = "https://example.test/videos/clip-1",
    status: int = 200,
    duration_s: float | None = 600.0,
    video_found: bool = True,
    drm: bool = False,
    rendered: tuple[int, int] = (1280, 720),
    videos: tuple[VideoState, ...] = (),
    play_control: bool = False,
    click_candidates: tuple[ElementDescription, ...] = (),
    click_performed: bool = False,
    # Defaults to the count over `html` itself, i.e. what a whole, untruncated document would give
    # `capture.mjs`. A test proving the truncation bug (issue #78) passes a different number to
    # simulate the whole document disagreeing with what a truncated `html` string would count.
    link_count: int | None = None,
) -> Loaded:
    return Loaded(
        status=status,
        url=url,
        title="clip",
        html=html,
        link_count=link_count if link_count is not None else len(_LINK.findall(html.lower())),
        video_found=video_found,
        duration_s=duration_s,
        drm=drm,
        width=1280,
        height=720,
        rendered=rendered,
        videos=videos,
        play_control=play_control,
        click_candidates=click_candidates,
        click_performed=click_performed,
    )


def click_to_play(candidates: tuple[ElementDescription, ...]) -> Loaded:
    """A page that draws a poster and a play control and has no `<video>` at all yet."""
    return playable(
        html="<html><body><div id='media-player'></div></body></html>",
        video_found=False,
        duration_s=None,
        play_control=True,
        click_candidates=candidates,
    )


def play_button(**fields: object) -> ElementDescription:
    """A big-play button over the page's own player; tests override one field at a time."""
    described: dict[str, object] = {
        "tag": "button",
        "class_name": "vjs-big-play-button",
        "role": "button",
        "rendered": (120, 80),
        "visible": True,
        "inside_player": True,
        "frame_allowed": True,
        "ancestors": ("div.player-container",),
    }
    described.update(fields)
    return ElementDescription(**described)  # type: ignore[arg-type]


# --- fixtures --------------------------------------------------------------------------------


@pytest.fixture
def page(make_page: Callable[[str], Page]) -> Page:
    return make_page("videos/clip-1")


@pytest.fixture
def candidate(page: Page, site: Site) -> BrowserCaptureCandidate:
    return BrowserCaptureCandidate.objects.create(
        page=page,
        site=site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.2,
        signals={"gate_checked": True, "basis": {"url": "example.test/videos/clip-1"}},
    )


def make_worker(
    browser: FakeBrowser, transport: Transport, *, max_frames: int = 4
) -> CaptureWorker:
    return CaptureWorker(
        browser,
        transport.client(),
        user_agent="MarcelineBot/0.1",
        plan=CapturePlan(max_frames=max_frames),
        sleep=lambda _: None,
    )


def process(worker: CaptureWorker, candidate: BrowserCaptureCandidate) -> BrowserCaptureCandidate:
    claimed = claim_candidate(candidate.pk)
    assert claimed is not None
    worker.process(claimed, CaptureReport())
    candidate.refresh_from_db()
    return candidate


def window(now: datetime, *, inside: bool) -> str:
    offset = timedelta(hours=0 if inside else 4)
    start, end = now + offset - timedelta(hours=2), now + offset + timedelta(hours=2)
    return f"{start:%H:%M}-{end:%H:%M}"


# --- the deny --------------------------------------------------------------------------------


def _update(page: Page, **fields: object) -> None:
    Page.objects.filter(pk=page.pk).update(**fields)


DENIALS = (
    pytest.param(
        lambda page, site: _update(page, safety_status=Page.SafetyStatus.BLOCKED),
        SAFETY_BLOCK_REASON,
        id="blocked",
    ),
    pytest.param(
        lambda page, site: _update(page, safety_status=Page.SafetyStatus.REVIEW),
        SAFETY_REVIEW_REASON,
        id="review",
    ),
    pytest.param(
        lambda page, site: _update(page, safety_score=0.5), SAFETY_GATE_REASON, id="gated"
    ),
    pytest.param(
        lambda page, site: _update(page, safety_score=None), SAFETY_GATE_REASON, id="never-scored"
    ),
    pytest.param(
        lambda page, site: _update(page, availability=Page.Availability.REMOVED),
        REMOVED_REASON,
        id="removed",
    ),
    pytest.param(
        lambda page, site: Site.objects.filter(pk=site.pk).update(enabled=False),
        SITE_DISABLED_REASON,
        id="site-disabled",
    ),
    pytest.param(
        lambda page, site: Site.objects.filter(pk=site.pk).update(capture_enabled=False),
        CAPTURE_DISABLED_REASON,
        id="capture-disabled",
    ),
)


@pytest.mark.parametrize(("deny", "reason"), DENIALS)
def test_a_denied_page_never_starts_a_browser(
    page: Page,
    site: Site,
    candidate: BrowserCaptureCandidate,
    deny: Callable[[Page, Site], None],
    reason: str,
) -> None:
    deny(page, site)
    browser, transport = FakeBrowser(playable()), Transport()

    process(make_worker(browser, transport), candidate)

    assert browser.visited == []
    assert transport.requested == []  # not even robots.txt is worth a request here
    assert candidate.status == Status.SKIPPED
    assert reason in candidate.last_error
    assert not FrameHash.objects.exists()
    # No capture_run: triage may queue the page again once the reason disappears.
    assert candidate.capture_run == {}
    # But the refusal is reconstructable on its own field (issue #78 amendment): page id + rule,
    # never the page's text or URL.
    assert candidate.refusal_detail == {
        "kind": "capture_denied",
        "page_id": page.pk,
        "rule": reason,
    }
    assert page.url not in str(candidate.refusal_detail)


def test_a_metadata_candidate_is_no_way_around_the_gate(
    page: Page, site: Site, candidate: BrowserCaptureCandidate
) -> None:
    """ADR 0040 amendment: the third signal aims captures, so it meets the deny like everyone else.

    Its input is the pirate site's own text about its own upload — a duration, a credit, a title —
    which may buy a *look* and never a permission. Triage refuses such a page before it is queued;
    this is the second, binding refusal, taken inside the call that would open the browser.
    """
    BrowserCaptureCandidate.objects.filter(pk=candidate.pk).update(
        reason=BrowserCaptureCandidate.Reason.METADATA_CANDIDATE, priority=0.3
    )
    _update(page, safety_score=0.5)  # clear, but not clearly safe: no media may be fetched
    browser, transport = FakeBrowser(playable()), Transport()

    process(make_worker(browser, transport), candidate)

    assert browser.visited == []
    assert transport.requested == []
    assert SAFETY_GATE_REASON in candidate.last_error
    assert not FrameHash.objects.exists()


def test_a_page_denied_after_the_claim_still_starts_no_browser(
    page: Page, candidate: BrowserCaptureCandidate
) -> None:
    """The worker holds a stale, clear copy of the page: the guard re-reads the database."""
    browser, transport = FakeBrowser(playable()), Transport()
    claimed = claim_candidate(candidate.pk)
    assert claimed is not None
    _update(page, safety_status=Page.SafetyStatus.BLOCKED)  # changed behind the worker's back

    make_worker(browser, transport).process(claimed, CaptureReport())

    candidate.refresh_from_db()
    assert browser.visited == []
    assert SAFETY_BLOCK_REASON in candidate.last_error


def test_the_deny_is_re_read_immediately_before_the_browser(
    page: Page, candidate: BrowserCaptureCandidate
) -> None:
    """Blocked *after* the queue check and the fail-fast, while robots.txt is being read.

    Only a check taken inside the call that starts the browser can catch this, which is exactly
    what `index/media_fetch.py` does for image fetches.
    """
    browser, transport = FakeBrowser(playable()), Transport()
    transport.before_robots = lambda: _update(page, safety_status=Page.SafetyStatus.BLOCKED)

    process(make_worker(browser, transport), candidate)

    assert transport.requested  # robots.txt was read, so the fail-fast had already passed
    assert browser.visited == []
    assert SAFETY_BLOCK_REASON in candidate.last_error
    assert candidate.refusal_detail == {
        "kind": "capture_denied",
        "page_id": page.pk,
        "rule": SAFETY_BLOCK_REASON,
    }


def test_capture_page_is_the_only_door_and_it_refuses(page: Page) -> None:
    browser = FakeBrowser(playable())
    _update(page, safety_status=Page.SafetyStatus.BLOCKED)

    with pytest.raises(CaptureDenied) as denied:
        capture_page(
            browser,
            page_id=page.pk,
            request=CaptureRequest(page.url, "MarcelineBot/0.1", CapturePlan()),
            decide=lambda loaded: Decision.capture([1.0]),
            check=lambda request: None,
        )

    assert denied.value.reason == SAFETY_BLOCK_REASON
    assert browser.visited == []


# --- crawler policy --------------------------------------------------------------------------


def test_robots_disallow_stops_the_capture(page: Page, candidate: BrowserCaptureCandidate) -> None:
    browser = FakeBrowser(playable())
    transport = Transport(robots="User-agent: *\nDisallow: /videos/\n")

    process(make_worker(browser, transport), candidate)

    assert browser.visited == []
    assert candidate.status == Status.SKIPPED
    assert "robots.txt" in candidate.last_error
    assert candidate.capture_run == {}  # policy, not the page: re-queueable
    # Reconstructable on its own field, never in capture_run (issue #78 amendment).
    assert candidate.refusal_detail == {
        "kind": ROBOTS_DISALLOWED,
        "host": "example.test",
        "path": "/videos/clip-1",
    }
    assert page.url not in str(candidate.refusal_detail)


def test_robots_disallow_shapes_an_id_looking_path_and_drops_the_full_url(
    make_page: Callable[[str], Page], site: Site
) -> None:
    """The refusal is reconstructable by host and path shape only — never the page's own URL."""
    token_page = make_page("videos/9f1c2b7a91e0")
    token_candidate = BrowserCaptureCandidate.objects.create(
        page=token_page,
        site=site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        signals={"gate_checked": True, "basis": {"url": token_page.url}},
    )
    browser = FakeBrowser(playable())
    transport = Transport(robots="User-agent: *\nDisallow: /videos/\n")

    process(make_worker(browser, transport), token_candidate)

    assert token_candidate.refusal_detail == {
        "kind": ROBOTS_DISALLOWED,
        "host": "example.test",
        "path": "/videos/*",
    }
    stored = json.dumps(token_candidate.refusal_detail)
    assert "http" not in stored
    assert token_page.url not in stored
    assert "9f1c2b7a91e0" not in stored  # the id-looking segment itself never reaches storage


def test_robots_unavailable_logs_no_url_but_keeps_it_in_the_database(
    candidate: BrowserCaptureCandidate, caplog: pytest.LogCaptureFixture
) -> None:
    """A `RobotsUnavailableError` quotes the robots.txt origin it could not fetch
    (`robots.txt for https://example.test: HTTP 503`); `candidate.last_error` keeps that in full,
    but the WARNING line must not — once a Sentry DSN is set it would ship as an event (issue:
    sanitise exception messages centrally, 2026-09-18)."""
    browser = FakeBrowser(playable())
    transport = Transport(by_host={"example.test": httpx.Response(503)})

    with caplog.at_level(logging.WARNING):
        process(make_worker(browser, transport), candidate)

    assert browser.visited == []
    assert candidate.status == Status.FAILED
    assert "https://example.test" in candidate.last_error
    assert "https://example.test" not in caplog.text
    assert "<url>" in caplog.text
    assert f"page {candidate.page_id}" in caplog.text


def test_the_site_delay_is_honoured_before_the_browser_opens(
    candidate: BrowserCaptureCandidate, site: Site
) -> None:
    Site.objects.filter(pk=site.pk).update(crawl_delay_s=7.0)
    slept: list[float] = []
    browser, transport = FakeBrowser(playable()), Transport()
    worker = CaptureWorker(
        browser,
        transport.client(),
        user_agent="MarcelineBot/0.1",
        plan=CapturePlan(max_frames=2),
        sleep=slept.append,
        clock=lambda: 0.0,
    )

    process(worker, candidate)

    assert browser.visited  # it did visit
    assert slept and max(slept) >= 7.0  # but only after waiting out the site's delay


CHALLENGES = (
    pytest.param(
        playable(status=403, html="<html><body><div class='g-recaptcha'></div></body></html>"),
        "challenge or CAPTCHA",
        id="captcha",
    ),
    pytest.param(playable(url="https://example.test/login"), "login page", id="login"),
    pytest.param(
        playable(
            html="<html><body><script src='https://agegate.example/a.js'></script></body></html>"
        ),
        "age_verification_provider",
        id="age-verification",
    ),
    pytest.param(
        playable(html="<html><body><h1>Are you 18 or older?</h1></body></html>"),
        "age_self_declaration",
        id="age-self-declaration",
    ),
)


@pytest.mark.parametrize(("loaded", "expected"), CHALLENGES)
def test_a_challenged_page_is_abandoned_with_a_reason(
    candidate: BrowserCaptureCandidate, loaded: Loaded, expected: str
) -> None:
    """No login, CAPTCHA, paywall or age-gate bypass: the crawler's own detector decides."""
    browser, transport = FakeBrowser(loaded), Transport()

    process(make_worker(browser, transport), candidate)

    assert browser.decisions[0].abort_reason.startswith("blocked:")
    assert expected in candidate.last_error
    assert candidate.status == Status.SKIPPED
    assert not FrameHash.objects.exists()


@pytest.mark.parametrize(
    ("loaded", "expected"),
    [
        pytest.param(playable(drm=True), "drm", id="drm"),
        pytest.param(playable(video_found=False), "no_video", id="no-video"),
        pytest.param(playable(duration_s=None), "no_video", id="unknown-duration"),
    ],
)
def test_an_unusable_player_is_recorded_and_left_alone(
    candidate: BrowserCaptureCandidate, loaded: Loaded, expected: str
) -> None:
    browser, transport = FakeBrowser(loaded), Transport()

    process(make_worker(browser, transport), candidate)

    assert candidate.status == Status.SKIPPED
    assert expected in candidate.last_error
    assert not FrameHash.objects.exists()
    # Scanned once (ADR 0016): the page was looked at, so triage does not re-queue this video.
    assert candidate.capture_run["skip_reason"].startswith(expected)


# --- every request the page makes (issue #78, ADR 0016 amendment 2026-09-17) ------------------


def test_every_page_request_passes_its_hosts_robots_and_the_embed_allowlist(
    candidate: BrowserCaptureCandidate,
) -> None:
    """The browser sends only what the crawler and media worker would send themselves."""
    Site.objects.create(domain="embed.test", name="Embed host")
    transport = Transport(
        robots="User-agent: *\nDisallow: /get/\n",
        by_host={
            "embed.test": httpx.Response(200, text="User-agent: *\nDisallow: /embed/\n"),
            "cdn.example.net": httpx.Response(403),  # 4xx robots.txt allows everything
            "down.example.net": httpx.Response(503),  # unreadable robots.txt refuses
        },
    )
    browser = FakeBrowser(
        playable(),
        page_requests=(
            BrowserRequest("https://example.test/js/player.js", "script"),
            BrowserRequest("https://example.test/get/player/1", "xhr"),
            BrowserRequest("https://cdn.example.net/hls/1.m3u8", "media"),
            BrowserRequest("https://down.example.net/seg1.ts", "xhr"),
            BrowserRequest("https://embed.test/embed/1", "document", subframe=True),
            BrowserRequest("https://embed.test/watch/1", "document", subframe=True),
            BrowserRequest("https://ads.example.org/banner", "document", subframe=True),
            BrowserRequest("https://example.test/poster.jpg", "image"),
        ),
    )

    process(make_worker(browser, transport), candidate)

    assert browser.sent == [
        "https://example.test/js/player.js",
        "https://cdn.example.net/hls/1.m3u8",
        "https://embed.test/watch/1",
    ]
    # An embed host that is not allowlisted is refused without even reading its robots.txt,
    # and a refused resource type costs nothing either.
    assert not any("ads.example.org" in url for url in transport.requested)
    refused = candidate.capture_run["diagnostics"]["refused"]
    assert refused == {
        "embed host not allowlisted / subframe": 1,
        "robots.txt / subframe": 1,
        "robots.txt / xhr": 1,
        "robots.txt unavailable / xhr": 1,
    }
    # Reconstructable per host (issue #78 amendment): which host, why, how many, what shape of
    # path — never a URL. This is what makes "which host blocked us" answerable after the fact.
    refused_hosts = candidate.capture_run["diagnostics"]["refused_hosts"]
    assert refused_hosts == [
        {
            "host": "ads.example.org",
            "reason": "embed host not allowlisted",
            "count": 1,
            "paths": ["/banner"],
        },
        {
            "host": "down.example.net",
            "reason": "robots.txt unavailable",
            "count": 1,
            "paths": ["/seg1.ts"],
        },
        {
            "host": "embed.test",
            "reason": "robots.txt",
            "count": 1,
            "paths": ["/embed/*"],
        },
        {
            "host": "example.test",
            "reason": "robots.txt",
            "count": 1,
            "paths": ["/get/player/*"],
        },
    ]
    assert "http" not in json.dumps(refused_hosts)


NO_VIDEO_CASES = (
    pytest.param(
        playable(video_found=False, duration_s=None),
        (BrowserRequest("https://embed.test/embed/1", "document", subframe=True),),
        "no_video: embedded player refused (robots.txt)",
        True,
        id="embed-refused-by-robots",
    ),
    pytest.param(
        playable(video_found=False, duration_s=None),
        (BrowserRequest("https://example.test/get/player/1", "xhr"),),
        "no_video: player requests refused (robots.txt)",
        True,
        id="player-request-refused",
    ),
    pytest.param(
        playable(video_found=False, duration_s=None, play_control=True),
        (),
        "no_video: click_to_play",
        True,
        id="click-to-play",
    ),
    pytest.param(
        # What 37 pages of the first re-run really were: an ad frame was refused, and the page
        # itself only builds its player when its play control is clicked (issue #78).
        playable(video_found=False, duration_s=None, play_control=True),
        (
            BrowserRequest("https://ads.example.org/widget", "document", subframe=True),
            BrowserRequest("https://tracker.example.org/t.js", "script"),
        ),
        "no_video: click_to_play",
        True,
        id="click-to-play-behind-an-ad-frame",
    ),
    pytest.param(
        playable(duration_s=None, videos=(VideoState(ready_state=0, error_code=3),)),
        (),
        "no_video: media error 3",
        True,
        id="decode-error",
    ),
    pytest.param(
        playable(rendered=(1264, 15)),
        (),
        "no_video: player drawn at 1264x15 px",
        True,
        id="player-not-laid-out",
    ),
    pytest.param(
        playable(video_found=False, duration_s=None, videos=(VideoState(error_code=4),)),
        (),
        "unsupported_media: MEDIA_ERR_SRC_NOT_SUPPORTED",
        False,
        id="codec-the-engine-lacks",
    ),
    pytest.param(
        # No media error, no robots-refused player request, no play control: previously a bare,
        # silent "no_video" even though a subframe host really was refused (issue #78 amendment).
        playable(video_found=False, duration_s=None),
        (BrowserRequest("https://ads.example.org/widget", "document", subframe=True),),
        "no_video: embedded player refused (embed host not allowlisted)",
        True,
        id="not-allowlisted-last-resort",
    ),
)


@pytest.mark.parametrize(("loaded", "requests", "expected", "scanned_once"), NO_VIDEO_CASES)
def test_a_missing_video_says_why(
    candidate: BrowserCaptureCandidate,
    loaded: Loaded,
    requests: tuple[BrowserRequest, ...],
    expected: str,
    scanned_once: bool,
) -> None:
    Site.objects.create(domain="embed.test", name="Embed host")
    transport = Transport(
        robots="User-agent: *\nDisallow: /get/\n",
        by_host={"embed.test": httpx.Response(200, text="User-agent: *\nDisallow: /embed/\n")},
    )
    browser = FakeBrowser(loaded, page_requests=requests)

    process(make_worker(browser, transport), candidate)

    assert candidate.status == Status.SKIPPED
    assert candidate.last_error == f"capture: {expected}"
    assert not FrameHash.objects.exists()
    if scanned_once:
        # The page's doing: recorded with its text-only evidence, and not visited again.
        assert candidate.capture_run["skip_reason"] == expected
        assert "diagnostics" in candidate.capture_run
        assert "http" not in json.dumps(candidate.capture_run["diagnostics"])
    else:
        # The engine's doing: another engine may try again, so nothing marks it scanned.
        assert candidate.capture_run == {}


def test_the_engine_comes_from_settings(candidate: BrowserCaptureCandidate) -> None:
    browser = FakeBrowser(playable())

    with override_settings(
        BROWSER_CAPTURE_ENGINE="chrome", BROWSER_CAPTURE_EXECUTABLE="/opt/chrome"
    ):
        process(make_worker(browser, Transport()), candidate)

    assert browser.requests_seen[0].engine == "chrome"
    assert browser.requests_seen[0].as_json()["executablePath"] == "/opt/chrome"
    assert candidate.capture_run["diagnostics"]["engine"] == "chrome"


# --- a successful capture --------------------------------------------------------------------


def test_a_capture_stores_hashes_and_nothing_else(
    page: Page, candidate: BrowserCaptureCandidate
) -> None:
    browser, transport = FakeBrowser(playable()), Transport()

    process(make_worker(browser, transport), candidate)

    media = Media.objects.get(page=page, kind=Media.Kind.CAPTURE)
    assert candidate.status == Status.CAPTURED
    assert media.url.endswith("#browser-capture")
    assert media.sha256 == "" and media.byte_size is None
    assert FrameHash.objects.filter(media=media).count() == 4
    # Hashes only: nothing in the record is, or could decode to, an image.
    record = json.dumps(candidate.capture_run)
    assert "png" not in record and "base64" not in record
    assert candidate.capture_run["frames_stored"] == 4
    assert candidate.capture_run["cost"]["bytes"] == STATS.bytes
    assert candidate.capture_run["cost"]["wall_ms"] == STATS.wall_ms
    assert candidate.capture_run["positions_s"] == list(browser.decisions[0].positions)


def test_a_gate_like_layout_class_with_few_links_in_the_truncated_window_is_not_blocked(
    page: Page, candidate: BrowserCaptureCandidate
) -> None:
    """issue #78: reproduces the site-12 false positive. `capture.mjs` truncates the rendered DOM
    before handing it to `detect_block`; a truncated window can show an unrelated layout class
    that coincides with an age-gate marker and, within just that window, few links. Once the
    caller reports the true whole-document link count alongside the truncated `html` (as
    `capture.mjs` now does), the visit is not aborted as `blocked: age_self_declaration`."""
    truncated_window = '<html><body><div class="age-gate-page"><a href="/x">x</a></body></html>'
    browser = FakeBrowser(playable(html=truncated_window, link_count=96))

    process(make_worker(browser, Transport()), candidate)

    assert candidate.status == Status.CAPTURED
    assert Media.objects.filter(page=page, kind=Media.Kind.CAPTURE).exists()


def test_captured_media_can_never_be_fetched(
    page: Page, candidate: BrowserCaptureCandidate
) -> None:
    process(make_worker(FakeBrowser(playable()), Transport()), candidate)

    media = Media.objects.get(page=page, kind=Media.Kind.CAPTURE)
    assert media.eligible_at is None
    assert media_denial(media.pk) == SAFETY_GATE_REASON  # the fetch layer refuses it
    assert queue.claim_media() is None  # and the media worker never offers it


def test_frames_per_video_are_capped(candidate: BrowserCaptureCandidate) -> None:
    browser, transport = FakeBrowser(playable()), Transport()

    process(make_worker(browser, transport, max_frames=3), candidate)

    assert len(browser.decisions[0].positions) == 3
    assert FrameHash.objects.count() == 3


def test_a_static_scene_is_not_stored_many_times(candidate: BrowserCaptureCandidate) -> None:
    browser, transport = FakeBrowser(playable(), identical_frames=True), Transport()

    process(make_worker(browser, transport), candidate)

    assert len(browser.decisions[0].positions) == 4
    assert FrameHash.objects.count() == 1


def test_hashes_are_dropped_if_the_page_is_gated_during_the_capture(
    page: Page, candidate: BrowserCaptureCandidate
) -> None:
    """The last line of defence: `store_hashes` re-checks the gate before writing."""
    browser = FakeBrowser(playable())
    browser.before_frames = lambda: _update(page, safety_score=0.5)

    process(make_worker(browser, Transport()), candidate)

    media = Media.objects.get(page=page, kind=Media.Kind.CAPTURE)
    assert not FrameHash.objects.exists()
    assert (media.status, media.last_error) == (Media.Status.SKIPPED, SAFETY_GATE_REASON)


def test_captured_frames_get_a_clip_timeline_position() -> None:
    """Ordered samples across a video, so `verify_timeline` can score their order (ADR 0015)."""
    position = frame_position(Media.Kind.CAPTURE, "https://example.test/x", 3, 7)

    assert position is not None
    assert (position.position, position.ordering) == (3, Ordering.CLIP)


# --- the run loop: enabled, window, budget ----------------------------------------------------


def test_capture_is_disabled_by_default(candidate: BrowserCaptureCandidate) -> None:
    browser, transport = FakeBrowser(playable()), Transport()

    report = make_worker(browser, transport).run()

    assert browser.visited == []
    assert "disabled" in " ".join(report.notes)


def test_nothing_is_captured_outside_the_window(candidate: BrowserCaptureCandidate) -> None:
    browser, transport = FakeBrowser(playable()), Transport()
    now = timezone.now()

    with override_settings(
        BROWSER_CAPTURE_ENABLED=True, BROWSER_CAPTURE_WINDOW_UTC=window(now, inside=False)
    ):
        report = make_worker(browser, transport).run()

    assert browser.visited == []
    assert "outside the capture window" in " ".join(report.notes)


def test_a_queued_candidate_is_captured_inside_the_window(
    candidate: BrowserCaptureCandidate,
) -> None:
    browser, transport = FakeBrowser(playable()), Transport()
    now = timezone.now()

    with override_settings(
        BROWSER_CAPTURE_ENABLED=True, BROWSER_CAPTURE_WINDOW_UTC=window(now, inside=True)
    ):
        report = make_worker(browser, transport).run()

    candidate.refresh_from_db()
    assert (report.captured, report.frames) == (1, 4)
    assert candidate.status == Status.CAPTURED
    assert report.bytes == STATS.bytes and report.wall_ms == STATS.wall_ms


def test_a_row_without_gate_signals_is_never_visited(
    candidate: BrowserCaptureCandidate,
) -> None:
    """Gate signals come from the scheduler; a manually refreshed row is not capturable."""
    candidate.signals = {**candidate.signals, "gate_checked": False}
    candidate.save(update_fields=("signals",))
    browser, transport = FakeBrowser(playable()), Transport()
    now = timezone.now()

    with override_settings(
        BROWSER_CAPTURE_ENABLED=True, BROWSER_CAPTURE_WINDOW_UTC=window(now, inside=True)
    ):
        make_worker(browser, transport).run()

    assert browser.visited == []


def test_the_daily_budget_stops_the_run(candidate: BrowserCaptureCandidate, page: Page) -> None:
    browser, transport = FakeBrowser(playable()), Transport()
    other = Page.objects.create(
        site=candidate.site, url="https://example.test/videos/clip-2", safety_score=1.0
    )
    BrowserCaptureCandidate.objects.create(
        page=other,
        site=candidate.site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        signals={"gate_checked": True},
    )
    now = timezone.now()

    with override_settings(
        BROWSER_CAPTURE_ENABLED=True,
        BROWSER_CAPTURE_WINDOW_UTC=window(now, inside=True),
        BROWSER_CAPTURE_TOTAL_PER_DAY=1,
    ):
        report = make_worker(browser, transport).run()

    assert report.visits == 1
    assert len(browser.visited) == 1


def test_a_row_that_left_the_queue_is_never_claimed_again(
    candidate: BrowserCaptureCandidate,
) -> None:
    """`SKIP LOCKED` protects the read-modify-write and `single_capture_lock` keeps it to one
    worker; on top of that, only a still-queued row is ever offered."""
    claimed = claim_candidate(candidate.pk)
    assert claimed is not None and claimed.attempts == 1

    BrowserCaptureCandidate.objects.filter(pk=candidate.pk).update(status=Status.CAPTURED)

    assert claim_candidate(candidate.pk) is None


# --- the refusal report (issue #78 amendment 2026-09-18) --------------------------------------


def test_refusal_report_answers_which_host_blocked_us(
    make_page: Callable[[str], Page], site: Site
) -> None:
    """Read-only, no browser: aggregates stored diagnostics across rows into one answer."""
    Site.objects.create(domain="embed.test", name="Embed host", enabled=True)
    page_a = make_page("videos/a")
    page_b = make_page("videos/b")
    BrowserCaptureCandidate.objects.create(
        page=page_a,
        site=site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        status=Status.SKIPPED,
        capture_run={
            "diagnostics": {
                "refused_hosts": [
                    {
                        "host": "ads.example.org",
                        "reason": "embed host not allowlisted",
                        "count": 3,
                        "paths": ["/banner", "/widget"],
                    },
                    {
                        "host": "embed.test",
                        "reason": "robots.txt",
                        "count": 1,
                        "paths": ["/embed/*"],
                    },
                ]
            }
        },
    )
    BrowserCaptureCandidate.objects.create(
        page=page_b,
        site=site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        status=Status.SKIPPED,
        refusal_detail={
            "kind": "capture_denied",
            "page_id": page_b.pk,
            "rule": SAFETY_BLOCK_REASON,
        },
    )

    lines = "\n".join(refusal_report())

    assert "ads.example.org: 3 refusal(s)" in lines
    assert "not allowlisted)" in lines
    assert "embed.test: 1 refusal(s)" in lines
    # An enabled Site's own domain is allowlisted by rule (CaptureWorker._request_policy), for
    # every visit, regardless of which site the visit belongs to.
    assert "(already allowlisted)" in lines
    assert f"{SAFETY_BLOCK_REASON}=1" in lines
    assert "across 1 page(s)" in lines
    assert "http" not in lines


def test_refusal_report_can_be_scoped_to_one_site(
    make_page: Callable[[str], Page], site: Site
) -> None:
    other_site = Site.objects.create(domain="other.test", name="Other")
    page_a = make_page("videos/a")
    page_b = Page.objects.create(
        site=other_site, url="https://other.test/videos/b", title="b", safety_score=1.0
    )
    BrowserCaptureCandidate.objects.create(
        page=page_a,
        site=site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        status=Status.SKIPPED,
        refusal_detail={"kind": ROBOTS_DISALLOWED, "host": "example.test", "path": "/videos/*"},
    )
    BrowserCaptureCandidate.objects.create(
        page=page_b,
        site=other_site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        status=Status.SKIPPED,
        refusal_detail={"kind": ROBOTS_DISALLOWED, "host": "other.test", "path": "/videos/*"},
    )

    lines = "\n".join(refusal_report(site_domain="other.test"))

    assert "other.test:" in lines
    assert "example.test:" not in lines


def test_refusal_report_says_when_nothing_is_recorded(candidate: BrowserCaptureCandidate) -> None:
    """A queued row with no decided attempt yet contributes nothing."""
    lines = refusal_report()

    assert lines == ["Capture refusals:", "  none recorded"]


def test_refusal_report_flag_needs_no_browser_or_enabled_setting(
    make_page: Callable[[str], Page], site: Site
) -> None:
    """The natural home for a read-only query: wired through `capture_worker`, not a new command."""
    page = make_page("videos/a")
    BrowserCaptureCandidate.objects.create(
        page=page,
        site=site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        status=Status.SKIPPED,
        refusal_detail={
            "kind": "capture_denied",
            "page_id": page.pk,
            "rule": SAFETY_BLOCK_REASON,
        },
    )
    out = io.StringIO()

    with override_settings(BROWSER_CAPTURE_ENABLED=False):
        call_command("capture_worker", "--refusal-report", stdout=out)

    assert f"{SAFETY_BLOCK_REASON}=1" in out.getvalue()
    assert "Browser capture is disabled" not in out.getvalue()


# --- the one allowed play click (ADR 0016 amendment 2026-09-18, issue #78) ---------------------


def opt_in(site: Site) -> None:
    """The operator's per-site approval for the single click. Off for every site by default."""
    Site.objects.filter(pk=site.pk).update(capture_play_click=True)


def click_record(candidate: BrowserCaptureCandidate) -> dict[str, object]:
    record = (candidate.capture_run.get("diagnostics") or {}).get("click") or {}
    assert isinstance(record, dict)
    return record


def test_a_site_without_the_opt_in_is_never_clicked(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    """The default. A click-to-play page ends exactly as it did before this feature existed."""
    browser = FakeBrowser(click_to_play((play_button(),)))

    process(make_worker(browser, Transport()), candidate)

    assert browser.clicks == []
    assert browser.requests_seen[0].allow_play_click is False
    assert "no_video: click_to_play" in candidate.last_error
    assert click_record(candidate) == {
        "allowed": False,
        "chosen": False,
        "performed": False,
        "candidates": 1,
        "kind": "",
        "shape": "",
        "reason": NOT_OPTED_IN,
        "video_after_click": False,
    }


@pytest.mark.parametrize(("deny", "reason"), DENIALS)
def test_a_denied_page_is_never_clicked_either(
    page: Page,
    site: Site,
    candidate: BrowserCaptureCandidate,
    deny: Callable[[Page, Site], None],
    reason: str,
) -> None:
    """The opt-in buys a click on a page we may visit, never a visit. The guard comes first."""
    opt_in(site)
    deny(page, site)
    browser, transport = FakeBrowser(click_to_play((play_button(),))), Transport()

    process(make_worker(browser, transport), candidate)

    assert browser.visited == [] and browser.clicks == []
    assert transport.requested == []
    assert reason in candidate.last_error
    assert candidate.capture_run == {}


GATES = (
    pytest.param(SELF_DECLARATION_FORM, PAGE_URL, id="age-self-declaration-form"),
    pytest.param(COOKIE_GATE, PAGE_URL, id="age-cookie-gate"),
    pytest.param(YOTI_PAGE, PAGE_URL, id="age-verification-provider"),
    pytest.param("<html></html>", "https://example.test/login/?next=/x", id="login"),
)


@pytest.mark.parametrize(("gate", "url"), GATES)
def test_a_gate_is_abandoned_before_any_click(
    site: Site, candidate: BrowserCaptureCandidate, gate: str, url: str
) -> None:
    """The crawler's own detector judges the loaded page *before* the click is offered, so a gate,
    a login page or a challenge is abandoned with no interaction at all — with the opt-in on and
    a perfectly admissible play control sitting right there."""
    opt_in(site)
    browser = FakeBrowser(
        playable(
            html=gate,
            url=url,
            video_found=False,
            duration_s=None,
            click_candidates=(play_button(),),
        )
    )

    process(make_worker(browser, Transport()), candidate)

    assert browser.clicks == []
    assert candidate.last_error.startswith("capture: blocked:")
    assert candidate.capture_run["diagnostics"]["click"]["chosen"] is False


def test_no_element_qualifies_and_nothing_is_clicked(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    """Opted in, but the page offers only furniture: a consent button, an ad and a download."""
    opt_in(site)
    browser = FakeBrowser(
        click_to_play(
            (
                play_button(class_name="play", text="I am 18 or older"),
                play_button(class_name="play-now", title="Close ad"),
                play_button(class_name="play", text="Download"),
                play_button(tag="a", class_name="play", navigates=True),
            )
        )
    )

    process(make_worker(browser, Transport()), candidate)

    assert browser.clicks == []
    assert "no_video: click_to_play" in candidate.last_error
    record = click_record(candidate)
    assert record["allowed"] is True and record["chosen"] is False
    assert record["candidates"] == 4


def test_the_one_click_reaches_the_player_and_the_visit_captures(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    opt_in(site)
    browser = FakeBrowser(
        click_to_play((play_button(class_name="ad-overlay-play"), play_button())),
        after_click=playable(click_performed=True),
    )

    process(make_worker(browser, Transport()), candidate)

    assert browser.clicks == [1]  # the ad overlay was never a candidate
    assert browser.requests_seen[0].allow_play_click is True
    assert candidate.status == Status.CAPTURED
    assert FrameHash.objects.exists()
    record = click_record(candidate)
    assert record["chosen"] is True and record["performed"] is True
    assert record["video_after_click"] is True
    assert record["kind"] == "play_control"
    assert record["shape"] == "button[role=button].vjs.big.play"


def test_a_page_that_stays_empty_after_the_click_is_never_clicked_again(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    """One click per visit. The page after it is judged, not clicked — a pre-roll ends here."""
    opt_in(site)
    after = click_to_play((play_button(),))
    browser = FakeBrowser(
        click_to_play((play_button(),)),
        after_click=replace(after, click_performed=True),
    )

    process(make_worker(browser, Transport()), candidate)

    assert browser.clicks == [0]
    assert candidate.last_error == f"capture: {NO_PLAYER_AFTER_CLICK}"
    # The page's own doing, so the page counts as scanned once (ADR 0016).
    assert candidate.capture_run != {}
    assert click_record(candidate)["video_after_click"] is False


def test_the_click_is_never_recorded_with_page_text_or_a_url(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    opt_in(site)
    browser = FakeBrowser(
        click_to_play(
            (
                play_button(
                    element_id="media-player",
                    aria_label="Play Some Pirated Title",
                    text="Watch Some Pirated Title now",
                ),
            )
        ),
        after_click=playable(click_performed=True),
    )

    process(make_worker(browser, Transport()), candidate)

    stored = json.dumps(candidate.capture_run)
    assert "Pirated" not in stored and "Watch" not in stored
    assert "https://" not in json.dumps(click_record(candidate))


def test_the_refusal_report_counts_the_clicks(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    opt_in(site)
    browser = FakeBrowser(
        click_to_play((play_button(),)), after_click=playable(click_performed=True)
    )

    process(make_worker(browser, Transport()), candidate)
    lines = "\n".join(refusal_report())

    assert "allowed on 1 visit(s), an element chosen on 1, clicked on 1" in lines
    assert "button[role=button].vjs.big.play=1" in lines


# --- the approved age self-declaration (ADR 0007's amendment, applied to the browser) ----------


APPROVED_AGE_COOKIES = {
    "cookies": {"age_confirmed": "1"},
    "approved_by": "operator",
    "approved_at": "2026-09-18",
    "note": "self-declaration gate, approved for capture",
}


def test_the_browser_sends_no_cookie_without_an_approval(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    browser = FakeBrowser(playable())

    process(make_worker(browser, Transport()), candidate)

    assert browser.requests_seen[0].cookies == ()
    assert browser.requests_seen[0].as_json()["cookies"] == []


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(
            {"cookies": {"PHPSESSID": "x"}, "approved_by": "o", "approved_at": "2026-09-18"},
            id="session-cookie",
        ),
        pytest.param({"cookies": {"age": "1"}}, id="no-approver"),
        pytest.param({"cookies": {}}, id="empty"),
    ],
)
def test_an_invalid_or_forbidden_approval_sends_nothing(
    site: Site, candidate: BrowserCaptureCandidate, invalid: dict[str, object]
) -> None:
    """`parse_age_confirmation` is the same gatekeeper the HTTP crawler uses: a session, login or
    anti-bot cookie is not an age self-declaration and never reaches the browser."""
    Site.objects.filter(pk=site.pk).update(age_confirmation=invalid)
    browser = FakeBrowser(playable())

    process(make_worker(browser, Transport()), candidate)

    assert browser.requests_seen[0].cookies == ()


def test_an_approved_self_declaration_is_sent_for_that_domain_only(
    site: Site, candidate: BrowserCaptureCandidate
) -> None:
    Site.objects.filter(pk=site.pk).update(age_confirmation=APPROVED_AGE_COOKIES)
    browser = FakeBrowser(playable())

    process(make_worker(browser, Transport()), candidate)

    request = browser.requests_seen[0]
    assert request.cookies == (("age_confirmed", "1"),)
    assert request.as_json()["cookies"] == [
        {"name": "age_confirmed", "value": "1", "domain": ".example.test", "path": "/"}
    ]
