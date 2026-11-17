"""The capture worker against a real headless browser and synthetic local pages (ADR 0016).

**Never a real site.** The videos are ffmpeg `testsrc2` clips generated at run time and the pages
are a few lines of hand-written HTML, all served by temporary HTTP servers on 127.0.0.1 and
127.0.0.2. No outside host is contacted, and the clips are deleted with the temporary directory.

The pages reproduce, one each, what the first real trial ran into (issue #78,
docs/research/browser-capture-no-video.md): an H.264/AAC stream, a player that is only laid out
by its stylesheet, a player embedded from another host whose robots.txt disallows the embed, and
a click-to-play poster with no player behind it.

Skipped unless the Playwright harness is installed (`frontend/parity`: `npm install &&
npx playwright install chromium`), so `make check` never needs a browser download. In a git
worktree point BROWSER_CAPTURE_NODE_MODULES at the main checkout's harness install.

Run it on its own to see what one capture costs:
    cd backend && uv run pytest marceline/tests/test_capture_browser.py -s
"""

import shutil
import statistics
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from django.test import override_settings

from catalog.models import Site
from core.http import PoliteClient
from index.models import FrameHash, Media, Page
from marceline.capture.plan import CapturePlan
from marceline.capture.runner import (
    DEFAULT_ENGINE,
    CaptureRequest,
    Decision,
    NodePlaywrightRunner,
    browser_available,
    engine_installed,
)
from marceline.capture.worker import CaptureReport, CaptureWorker, _downscale, claim_candidate
from matching.frames import iter_video_frames
from matching.models import BrowserCaptureCandidate
from matching.pdq import hamming_distance, hash_image, open_image
from matching.references import STRICT_MAX_DISTANCE

pytestmark = pytest.mark.skipif(
    not browser_available() or shutil.which("ffmpeg") is None,
    reason="needs ffmpeg and the Playwright harness (frontend/parity: npm install)",
)

CLIP_SECONDS = 60
EMBED_HOST = "127.0.0.2"
PAGES = {
    # The original synthetic page: a VP8/WebM clip in a bare <video>.
    "/videos/clip-1": '<video id="v" src="/clip.webm" preload="auto" muted playsinline></video>',
    # H.264/AAC in MP4, the codec pair of almost every real video site.
    "/videos/h264": '<video src="/clip.mp4" preload="auto" muted playsinline></video>',
    # Drawn 15 px tall unless its stylesheet loads, like the trial's only "captured" player.
    "/videos/styled": (
        '<link rel="stylesheet" href="/player.css">'
        '<video src="/clip.webm" preload="auto" muted playsinline style="height:15px"></video>'
    ),
    # A poster and a play button; the player would only be created by a click.
    "/videos/click": '<div id="media-player"><div class="play-icon"></div></div>',
    # A player container the policy may click, behind which nothing is ever built: one click, and
    # then the visit ends. This is also what a pre-roll ad looks like from our side.
    "/videos/clicknoplay": (
        '<div id="media-player" style="width:640px;height:360px">'
        '<div class="play-icon" style="width:120px;height:80px"></div></div>'
    ),
    # The same, but the click really builds the player — the shape the operator approved one
    # click for (ADR 0016 amendment 2026-09-18). The ad furniture beside it must stay untouched.
    "/videos/clickplay": (
        '<button class="play-download">Download</button>'
        '<button class="play-now" title="Close ad" style="width:300px;height:200px"></button>'
        '<div id="media-player" style="width:640px;height:360px">'
        '<div class="play-icon" style="width:120px;height:80px"></div></div>'
        "<script>document.getElementById('media-player').addEventListener('click', function () {"
        "  var v = document.createElement('video');"
        "  v.src = '/clip.webm'; v.muted = true;"
        "  v.setAttribute('playsinline', ''); v.setAttribute('preload', 'auto');"
        "  v.style.width = '640px'; v.style.height = '360px';"
        "  document.body.appendChild(v);"
        "});</script>"
    ),
    # The player lives in an iframe on another host, whose robots.txt disallows /embed/.
    "/videos/embedded": '<iframe width="640" height="360" src="{embed}/embed/1"></iframe>',
    # A clip whose every moment looks different, to check which moment a screenshot shows.
    "/videos/seek": '<video src="/seek" preload="auto" muted playsinline></video>',
}
PLAYER_CSS = b"video { width: 640px; height: 360px !important; }"
SEEK_FPS = 25.0
SEEK_SECONDS = 18
MATCH_DISTANCE = STRICT_MAX_DISTANCE  # the strict match threshold, whatever it is set to


SEEK_CODECS = {
    "vp8": ("webm", ("-c:v", "libvpx", "-b:v", "1M", "-deadline", "realtime", "-cpu-used", "8")),
    "h264": ("mp4", ("-c:v", "libx264", "-preset", "ultrafast", "-movflags", "+faststart")),
}


def _encode_seek_clip(path: Path, codec: str) -> bytes:
    subprocess.run(
        [
            *("ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi"),
            *("-i", f"mandelbrot=size=320x180:rate={SEEK_FPS:g}"),
            *("-t", str(SEEK_SECONDS), "-pix_fmt", "yuv420p", "-g", "25"),
            *SEEK_CODECS[codec][1],
            str(path),
        ],
        check=True,
        timeout=300,
    )
    return path.read_bytes()


def _encode(path: Path, *args: str) -> bytes:
    subprocess.run(
        [
            *("ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi"),
            *("-i", f"testsrc2=size=320x240:rate=10:duration={CLIP_SECONDS}"),
            *args,
            str(path),
        ],
        check=True,
        timeout=300,
    )
    return path.read_bytes()


@pytest.fixture(scope="module")
def clips() -> Iterator[dict[str, bytes]]:
    """Synthetic clips. Deterministic pixels, no real or pirated media, ever."""
    with tempfile.TemporaryDirectory() as tmp:
        webm = _encode(
            Path(tmp) / "clip.webm",
            *("-pix_fmt", "yuv420p", "-c:v", "libvpx", "-b:v", "500k"),
            *("-deadline", "realtime", "-cpu-used", "8", "-g", "10"),
        )
        encoders = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=True
        ).stdout
        mp4 = b""
        if "libx264" in encoders:
            mp4 = _encode(
                Path(tmp) / "clip.mp4",
                *("-f", "lavfi", "-i", f"sine=frequency=440:duration={CLIP_SECONDS}"),
                *("-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-g", "10"),
                *("-c:a", "aac", "-movflags", "+faststart"),
            )
        yield {"/clip.webm": webm, "/clip.mp4": mp4}


class Handler(BaseHTTPRequestHandler):
    """Serves robots.txt, the player pages and the clips, with the Range support a player needs.

    On the embed host robots.txt disallows `/embed/`, so the browser must never ask for it.
    """

    clips: ClassVar[dict[str, bytes]] = {}
    embed: ClassVar[str] = ""
    seek_type: ClassVar[str] = "video/webm"
    seen: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        host = self.headers.get("Host", "")
        type(self).seen.append(f"{host}{self.path}")
        if self.path == "/robots.txt":
            rule = "Disallow: /embed/" if host.startswith(EMBED_HOST) else "Allow: /"
            self._body(f"User-agent: *\n{rule}\n".encode(), "text/plain")
        elif self.path in PAGES:
            html = PAGES[self.path].replace("{embed}", type(self).embed)
            self._body(f"<!doctype html><html><body>{html}</body></html>".encode(), "text/html")
        elif self.path == "/embed/1":
            self._body(b"<video src='/clip.webm' muted></video>", "text/html")
        elif self.path == "/player.css":
            self._body(PLAYER_CSS, "text/css")
        elif self.path in type(self).clips:
            self._clip(type(self).clips[self.path])
        else:
            self.send_error(404)

    def _content_type(self) -> str:
        if self.path == "/seek":
            return type(self).seek_type
        return "video/mp4" if self.path.endswith("mp4") else "video/webm"

    def _body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _clip(self, data: bytes) -> None:
        start, end = 0, len(data) - 1
        header = self.headers.get("Range", "")
        partial = header.startswith("bytes=")
        if partial:
            first, _, last = header.removeprefix("bytes=").partition("-")
            start = int(first or 0)
            end = int(last) if last else end
        chunk = data[start : end + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", self._content_type())
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        self.wfile.write(chunk)


def _serve(address: str) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer((address, 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://{address}:{httpd.server_address[1]}"


@pytest.fixture
def server(clips: dict[str, bytes]) -> Iterator[str]:
    Handler.clips = clips
    Handler.seen = []
    page_server, base = _serve("127.0.0.1")
    embed_server, Handler.embed = _serve(EMBED_HOST)
    try:
        yield base
    finally:
        for httpd in (page_server, embed_server):
            httpd.shutdown()
            httpd.server_close()


@pytest.fixture
def site(db: None) -> Site:
    # The embed host is an enabled site, so only its robots.txt can refuse the embed.
    Site.objects.create(domain=EMBED_HOST, name="Loopback embed host", crawl_delay_s=0.0)
    return Site.objects.create(domain="127.0.0.1", name="Loopback", crawl_delay_s=0.0)


def make_candidate(
    site: Site, server: str, path: str = "/videos/clip-1"
) -> BrowserCaptureCandidate:
    page = Page.objects.create(
        site=site, url=f"{server}{path}", title="synthetic clip", safety_score=1.0
    )
    return BrowserCaptureCandidate.objects.create(
        page=page,
        site=site,
        reason=BrowserCaptureCandidate.Reason.REVIEW_BAND_MATCH,
        priority=1.0,
        signals={"gate_checked": True},
    )


@pytest.fixture
def candidate(site: Site, server: str) -> BrowserCaptureCandidate:
    return make_candidate(site, server)


def run_capture(
    candidate: BrowserCaptureCandidate, *, max_frames: int = 24
) -> tuple[BrowserCaptureCandidate, CaptureReport]:
    report = CaptureReport()
    with PoliteClient("MarcelineBot/0.1", max_retries=0) as client:
        worker = CaptureWorker(
            NodePlaywrightRunner(),
            client,
            user_agent="MarcelineBot/0.1",
            plan=CapturePlan(max_frames=max_frames, max_seconds=180.0, timeout_s=300.0),
            engine=DEFAULT_ENGINE,
        )
        claimed = claim_candidate(candidate.pk)
        assert claimed is not None
        worker.process(claimed, report)
    candidate.refresh_from_db()
    return candidate, report


def test_a_real_browser_samples_the_player_and_stores_only_hashes(
    candidate: BrowserCaptureCandidate,
) -> None:
    candidate, report = run_capture(candidate)

    assert candidate.status == BrowserCaptureCandidate.Status.CAPTURED, candidate.last_error
    media = Media.objects.get(page=candidate.page, kind=Media.Kind.CAPTURE)
    hashes = FrameHash.objects.filter(media=media)
    assert hashes.count() >= 5
    assert all(len(bytes(row.pdq)) == 32 for row in hashes)
    assert media.eligible_at is None and media.sha256 == ""

    cost = candidate.capture_run["cost"]
    print(
        f"\nONE CAPTURE: {cost['wall_ms'] / 1000:.1f} s wall "
        f"({cost['sample_ms'] / 1000:.1f} s sampling), "
        f"{cost['bytes'] / 1_048_576:.2f} MiB over {cost['requests']} requests "
        f"({cost['aborted_requests']} blocked), "
        f"{candidate.capture_run['frames_sampled']} frames sampled, "
        f"{candidate.capture_run['frames_stored']} hashes stored, {cost['browser']}"
    )
    assert cost["wall_ms"] > 0
    assert report.visits == 1


def test_an_h264_aac_stream_is_captured(site: Site, server: str) -> None:
    if not Handler.clips["/clip.mp4"]:
        pytest.skip("ffmpeg has no libx264 encoder")
    candidate, _ = run_capture(make_candidate(site, server, "/videos/h264"), max_frames=4)

    assert candidate.status == BrowserCaptureCandidate.Status.CAPTURED, candidate.last_error
    assert FrameHash.objects.filter(media__page=candidate.page).exists()


def test_a_player_laid_out_by_its_stylesheet_is_captured_full_size(site: Site, server: str) -> None:
    candidate, _ = run_capture(make_candidate(site, server, "/videos/styled"), max_frames=4)

    assert candidate.status == BrowserCaptureCandidate.Status.CAPTURED, candidate.last_error
    assert candidate.capture_run["video"]["rendered"] == [640, 360]


def test_an_embed_its_host_disallows_is_never_requested(site: Site, server: str) -> None:
    candidate, _ = run_capture(make_candidate(site, server, "/videos/embedded"), max_frames=4)

    assert candidate.status == BrowserCaptureCandidate.Status.SKIPPED
    assert candidate.last_error == "capture: no_video: embedded player refused (robots.txt)"
    assert f"{Handler.embed.removeprefix('http://')}/robots.txt" in Handler.seen
    assert not any("/embed/" in path for path in Handler.seen)
    assert not any("clip" in path for path in Handler.seen)


def test_a_click_to_play_poster_is_left_alone_with_its_reason(site: Site, server: str) -> None:
    candidate, _ = run_capture(make_candidate(site, server, "/videos/click"), max_frames=4)

    assert candidate.status == BrowserCaptureCandidate.Status.SKIPPED
    assert candidate.last_error == "capture: no_video: click_to_play"
    assert candidate.capture_run["diagnostics"]["play_control"] is True
    # Without the per-site opt-in nothing is ever clicked, whatever the page offers.
    assert candidate.capture_run["diagnostics"]["click"]["chosen"] is False


def test_the_one_play_click_builds_the_player_in_a_real_browser(site: Site, server: str) -> None:
    """The end-to-end proof of the ADR 0016 amendment of 2026-09-18, through real Chromium.

    The page carries a download button and a "Close ad" button beside its player; both are named
    with a `play` class on purpose, so this also proves that the element the policy picked is the
    player container and not the furniture around it.
    """
    Site.objects.filter(pk=site.pk).update(capture_play_click=True)
    candidate, _ = run_capture(make_candidate(site, server, "/videos/clickplay"), max_frames=4)

    assert candidate.status == BrowserCaptureCandidate.Status.CAPTURED, candidate.last_error
    assert FrameHash.objects.filter(media__page=candidate.page).exists()
    click = candidate.capture_run["diagnostics"]["click"]
    assert click["allowed"] is True and click["performed"] is True
    assert click["video_after_click"] is True
    assert click["kind"] == "player_container"
    assert click["shape"] == "div.media.player"


def test_a_page_that_gives_no_player_after_the_click_is_not_clicked_again(
    site: Site, server: str
) -> None:
    """One click per visit. Nothing is behind this player, so the visit ends after the click —
    which is also how a pre-roll ad ends, since no "skip" control may ever be clicked."""
    Site.objects.filter(pk=site.pk).update(capture_play_click=True)
    candidate, _ = run_capture(make_candidate(site, server, "/videos/clicknoplay"), max_frames=4)

    assert candidate.status == BrowserCaptureCandidate.Status.SKIPPED
    assert candidate.last_error == "capture: no_video: no player after the play click"
    assert candidate.capture_run["diagnostics"]["click"]["performed"] is True
    assert candidate.capture_run["diagnostics"]["click"]["video_after_click"] is False


SEEK_POSITIONS = [3.0, 6.0, 9.0, 12.0, 15.0]


@pytest.mark.parametrize(
    ("engine", "codec"),
    [
        pytest.param("firefox", "h264", id="firefox-h264"),
        pytest.param("firefox", "vp8", id="firefox-vp8"),
        pytest.param("chromium", "vp8", id="chromium-vp8"),
        pytest.param("chromium", "h264", id="chromium-h264"),
    ],
)
def test_captured_frames_are_distinct_pictures_of_the_video_evenly_spaced(
    server: str, engine: str, codec: str
) -> None:
    """What a capture must deliver (issue #78, operator 2026-09-17): every screenshot is a real
    picture *of this video*, the pictures are different from each other, they come in the order
    they were sampled, and they are spaced about as evenly as asked for.

    **The position the player reports is metadata, not evidence**, and a leak is often an excerpt,
    so nothing here asserts "the frame at t=X". The measurement is the nearest frame of the clip
    (by PDQ) and the offset that implies — the same shape of evidence `matching.timeline` scores:
    a run of frames at a roughly constant offset.

    The clip is ffmpeg's `mandelbrot` zoom: every moment looks different and changes smoothly, so
    a stale screenshot (the same picture every time) or a drifting one shows up in the offsets.
    """
    if not engine_installed(engine):
        pytest.skip(f"Playwright's {engine} build is not installed")
    if codec == "h264" and not Handler.clips["/clip.mp4"]:
        pytest.skip("ffmpeg has no libx264 encoder")
    extension = SEEK_CODECS[codec][0]
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / f"seek.{extension}"
        Handler.clips = {**Handler.clips, "/seek": _encode_seek_clip(clip, codec)}
        grid = [hash_image(image).hash for _, image in iter_video_frames(clip, fps=SEEK_FPS)]
    Handler.seek_type = f"video/{extension}"

    outcome = NodePlaywrightRunner().capture(
        CaptureRequest(
            url=f"{server}/videos/seek",
            user_agent="MarcelineBot/0.1",
            plan=CapturePlan(max_frames=len(SEEK_POSITIONS)),
            engine=engine,
        ),
        lambda loaded: Decision.capture(SEEK_POSITIONS),
        lambda request: None,
    )

    assert len(outcome.frames) == len(SEEK_POSITIONS), outcome.skip_reason
    nearest = [
        min(
            (hamming_distance(hash_image(_downscale(open_image(frame.png))).hash, hashed), index)
            for index, hashed in enumerate(grid)
        )
        for frame in outcome.frames
    ]
    distances = [distance for distance, _ in nearest]
    matched = [index for _, index in nearest]
    offsets = [
        index / SEEK_FPS - frame.position_s
        for index, frame in zip(matched, outcome.frames, strict=True)
    ]

    # Each screenshot is a picture of this video…
    assert statistics.median(distances) <= MATCH_DISTANCE, distances
    # …a different one each time, in sampling order (a stale player repeats one picture)…
    assert matched == sorted(set(matched)), matched
    # …and the offsets between what was asked for and what arrived stay within a second, so the
    # spacing the plan asked for survives (a playing video drifts instead).
    assert max(offsets) - min(offsets) <= 1.0, offsets


def test_the_frame_cap_is_what_the_browser_actually_does(
    candidate: BrowserCaptureCandidate,
) -> None:
    candidate, _ = run_capture(candidate, max_frames=4)

    assert candidate.capture_run["frames_sampled"] <= 4


def test_a_denied_page_is_never_opened_by_the_real_browser(
    candidate: BrowserCaptureCandidate,
) -> None:
    """The end-to-end proof: the local server records every request it is asked for."""
    Page.objects.filter(pk=candidate.page_id).update(safety_status=Page.SafetyStatus.BLOCKED)
    Handler.seen = []

    candidate, _ = run_capture(candidate)

    assert Handler.seen == []  # the browser was never started, so nothing was requested
    assert candidate.status == BrowserCaptureCandidate.Status.SKIPPED
    assert "safety block" in candidate.last_error
    assert not FrameHash.objects.exists()


def test_nothing_is_written_to_disk_by_a_capture(
    candidate: BrowserCaptureCandidate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frames stay in memory: the hard rule, checked against the process's own temp directory.

    The temp directory is this test's own: the shared one also holds the frame directories of the
    sampling tests running in parallel, which come and go between two looks at it.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with override_settings():
        run_capture(candidate, max_frames=4)

    assert list(tmp_path.iterdir()) == []
