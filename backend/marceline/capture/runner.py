"""Drive the vendored Playwright browser as a subprocess (ADR 0016).

**Why a Node subprocess and not a Python browser library.** `frontend/parity` already vendors
Playwright and a working Chromium for the browser-parity harness (issue #28), including the
root-free NSS fallback its README documents. Adding `playwright` to `backend/pyproject.toml`
would mean a second copy of the same driver and a second browser download in the backend image,
for a feature that is off by default. So the backend ships a small Node script instead
(`capture.mjs`), resolves Playwright from the harness install, and speaks JSON over a pipe. No new
Python package, and `uv sync` still installs nothing that can open a browser.

Frames come back as base64 PNG in memory and are dropped after hashing. Nothing here writes a
frame, a screenshot or a video anywhere.

**Every request the page makes is asked for first** (ADR 0016 amendment 2026-09-17): the script
holds it and sends a `check` message; the `RequestCheck` the worker passes answers it
(`marceline.capture.requests.RequestPolicy`: robots.txt per host, embed allowlist). Nothing the
policy has not seen leaves the browser.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
from base64 import b64decode
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from django.conf import settings

from marceline.capture.click import ElementDescription, parse_candidates
from marceline.capture.plan import CapturePlan
from marceline.capture.requests import BLOCKED_RESOURCE_TYPES, BrowserRequest

logger = logging.getLogger(__name__)

SCRIPT = Path(__file__).with_name("capture.mjs")
# The parity harness install (frontend/parity/README.md): `npm install` there, once per machine.
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_PLAYWRIGHT_DIR = REPO_ROOT / "frontend" / "parity" / "node_modules"
# Root-free NSS fallback for Chromium on a bare Debian host (frontend/parity/README.md). Used
# only when it exists; with libnss3/libnspr4 installed system-wide nothing here applies.
NSS_FALLBACK = REPO_ROOT / "frontend" / "parity" / ".libs" / "root" / "usr" / "lib"
DEFAULT_VIEWPORT = (1280, 720)
# Slack on top of the plan's own watchdog before the subprocess is killed outright.
KILL_MARGIN_S = 30.0
STDERR_LINES_KEPT = 50
# Browser engines `capture.mjs` can launch (BROWSER_CAPTURE_ENGINE, docs/research/
# browser-capture-no-video.md): `chromium` is Playwright's bundled Chromium (the headless shell),
# `firefox` Playwright's Firefox build, `chrome` Google Chrome through Playwright's `chrome`
# channel or BROWSER_CAPTURE_EXECUTABLE.
#
# Chromium is the default. Measured on a synthetic H.264 clip, its screenshot of a seeked video is
# 3 bits (median) from ffmpeg's frame at that position, Chrome's the same, Firefox's 18; all three
# play H.264/AAC. Chromium also needs the least memory (about 510 MiB against 750 for Firefox and
# 1,100 for Chrome) and nothing beyond `npx playwright install chromium`.
ENGINES = ("chromium", "firefox", "chrome")
DEFAULT_ENGINE = "chromium"


class BrowserUnavailableError(Exception):
    """The browser could not be started at all: a configuration problem, not a page problem."""


@dataclass(frozen=True)
class CaptureRequest:
    url: str
    user_agent: str
    plan: CapturePlan
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    engine: str = DEFAULT_ENGINE
    executable_path: str = ""
    # Per-site operator switches, both default-off (ADR 0016 amendment 2026-09-18, issue #78).
    # `allow_play_click`: the browser may offer the page's own play control to the policy module
    # (`catalog.Site.capture_play_click`). `cookies`: the approved age self-declaration of
    # `Site.age_confirmation`, already validated — never a session, login or anti-bot cookie.
    allow_play_click: bool = False
    cookies: tuple[tuple[str, str], ...] = ()
    cookie_domain: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "userAgent": self.user_agent,
            "plan": self.plan.as_json(),
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "engine": self.engine,
            "executablePath": self.executable_path,
            "blockedResourceTypes": sorted(BLOCKED_RESOURCE_TYPES),
            "allowPlayClick": self.allow_play_click,
            "cookies": [
                {
                    "name": name,
                    "value": value,
                    "domain": f".{self.cookie_domain.lstrip('.')}",
                    "path": "/",
                }
                for name, value in self.cookies
                if self.cookie_domain
            ],
        }


@dataclass(frozen=True)
class VideoState:
    """One `<video>` element as the browser saw it: text-only state, never a pixel."""

    ready_state: int = 0
    network_state: int = 0
    error_code: int | None = None
    duration_s: float | None = None
    rendered: tuple[int, int] = (0, 0)
    src_kind: str = "none"  # none, blob (MediaSource), hls, dash, mp4, webm, other

    @classmethod
    def from_json(cls, message: Mapping[str, Any]) -> "VideoState":
        duration = message.get("duration")
        error = message.get("error")
        return cls(
            ready_state=int(message.get("readyState") or 0),
            network_state=int(message.get("networkState") or 0),
            error_code=int(error) if isinstance(error, (int, float)) else None,
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
            rendered=_size(message.get("rendered")),
            src_kind=str(message.get("srcKind") or "none"),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "ready_state": self.ready_state,
            "network_state": self.network_state,
            "error_code": self.error_code,
            "duration_s": self.duration_s,
            "rendered": list(self.rendered),
            "src_kind": self.src_kind,
        }


def _size(value: Any) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0] or 0), int(value[1] or 0)
    return 0, 0


@dataclass(frozen=True)
class Loaded:
    """What the browser found after navigating, before anything is sampled."""

    status: int
    url: str
    title: str
    html: str
    # `<a href>` count over the whole page, counted in `capture.mjs` before `html` is truncated to
    # its 50,000-character cap (issue #78). `detect_block`/`classify_age_gate` use this instead of
    # recounting on the truncated string, which undercounts and can misclassify an ordinary page as
    # an age gate.
    link_count: int
    video_found: bool
    duration_s: float | None
    drm: bool
    width: int | None = None
    height: int | None = None
    # The size the chosen player is drawn at, in CSS pixels: what an element screenshot gets.
    rendered: tuple[int, int] = (0, 0)
    # Every <video> in every frame, whether the page shows a play control without a player, and
    # which player libraries it loaded: the text-only evidence behind a `no_video` skip.
    videos: tuple[VideoState, ...] = ()
    play_control: bool = False
    players: tuple[str, ...] = ()
    # The elements the browser offered as possible play controls (issue #78). Only populated
    # when the site is opted in and the page gave no player by itself; the pure policy module
    # `marceline.capture.click` decides whether any of them may be clicked.
    click_candidates: tuple[ElementDescription, ...] = ()
    # True on the *second* `loaded` of a visit: the one allowed click was really performed.
    click_performed: bool = False

    @classmethod
    def from_json(cls, message: Mapping[str, Any]) -> "Loaded":
        video = message.get("video") or {}
        duration = video.get("duration")
        return cls(
            status=int(message.get("status") or 0),
            url=str(message.get("url") or ""),
            title=str(message.get("title") or ""),
            html=str(message.get("html") or ""),
            link_count=int(message.get("linkCount") or 0),
            video_found=bool(video.get("found")),
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
            drm=bool(video.get("drm")),
            width=video.get("width"),
            height=video.get("height"),
            rendered=_size(video.get("rendered")),
            videos=tuple(VideoState.from_json(item) for item in message.get("videos") or []),
            play_control=bool(message.get("playControl")),
            players=tuple(str(name) for name in message.get("players") or []),
            click_candidates=parse_candidates(message.get("clickCandidates")),
            click_performed=bool(message.get("clickPerformed")),
        )


@dataclass(frozen=True)
class Decision:
    """What to do with the loaded page: sample these positions, click once, or leave and say why.

    `click_index` is the one interaction this design allows (ADR 0016 amendment 2026-09-18): the
    index of an element the browser offered in `Loaded.click_candidates`, chosen by the pure
    policy of `marceline.capture.click`. A visit can produce it at most once — `_talk` below has
    no loop, so a second click cannot be asked for even by a caller that wanted to.
    """

    positions: tuple[float, ...] = ()
    abort_reason: str = ""
    click_index: int | None = None

    @classmethod
    def abort(cls, reason: str) -> "Decision":
        return cls(abort_reason=reason)

    @classmethod
    def capture(cls, positions: list[float]) -> "Decision":
        return cls(positions=tuple(positions))

    @classmethod
    def click(cls, index: int) -> "Decision":
        return cls(click_index=index)

    def as_json(self) -> dict[str, Any]:
        if self.click_index is not None:
            return {"action": "click", "index": self.click_index}
        if self.abort_reason or not self.positions:
            return {"action": "abort", "reason": self.abort_reason or "no positions"}
        return {"action": "capture", "positions": list(self.positions)}


@dataclass(frozen=True)
class CapturedFrame:
    """One sampled frame. `png` lives in memory only and is dropped after hashing."""

    position_s: float
    png: bytes


@dataclass(frozen=True)
class CaptureStats:
    """What one capture cost. Recorded per candidate so the price stays visible."""

    wall_ms: int = 0
    sample_ms: int = 0
    bytes: int = 0
    requests: int = 0
    aborted_requests: int = 0
    browser: str = ""

    @classmethod
    def from_json(cls, message: Mapping[str, Any]) -> "CaptureStats":
        return cls(
            wall_ms=int(message.get("wallMs") or 0),
            sample_ms=int(message.get("sampleMs") or 0),
            bytes=int(message.get("bytes") or 0),
            requests=int(message.get("requests") or 0),
            aborted_requests=int(message.get("abortedRequests") or 0),
            browser=str(message.get("browser") or ""),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "wall_ms": self.wall_ms,
            "sample_ms": self.sample_ms,
            "bytes": self.bytes,
            "requests": self.requests,
            "aborted_requests": self.aborted_requests,
            "browser": self.browser,
        }


@dataclass(frozen=True)
class CaptureOutcome:
    loaded: Loaded | None = None
    frames: tuple[CapturedFrame, ...] = ()
    stats: CaptureStats = field(default_factory=CaptureStats)
    skip_reason: str = ""


# Asked before every request the page makes: None lets it through, a reason refuses it.
RequestCheck = Callable[[BrowserRequest], str | None]


class BrowserRunner(Protocol):
    """Anything that can visit a page and bring frames back. Tests substitute a fake."""

    def capture(
        self,
        request: CaptureRequest,
        decide: Callable[[Loaded], Decision],
        check: RequestCheck,
    ) -> CaptureOutcome: ...


def playwright_dir() -> Path:
    configured = str(getattr(settings, "BROWSER_CAPTURE_NODE_MODULES", "") or "")
    return Path(configured) if configured else DEFAULT_PLAYWRIGHT_DIR


def browsers_dir() -> Path:
    """Where Playwright keeps its browser builds (PLAYWRIGHT_BROWSERS_PATH, else its default)."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    return Path(configured) if configured else Path.home() / ".cache" / "ms-playwright"


# Playwright's build directories per engine; `chrome` without an executable is a system install.
_ENGINE_BUILDS = {
    "firefox": ("firefox-*",),
    "chromium": ("chromium_headless_shell-*", "chromium-*"),
}
SYSTEM_CHROME = Path("/opt/google/chrome/chrome")


def engine_installed(engine: str, executable_path: str = "") -> bool:
    """Whether the browser for `engine` is on this machine (never downloads anything)."""
    if executable_path:
        return Path(executable_path).is_file()
    if engine == "chrome":
        return SYSTEM_CHROME.is_file()
    root = browsers_dir()
    return any(any(root.glob(pattern)) for pattern in _ENGINE_BUILDS.get(engine, ()))


def browser_available(engine: str | None = None) -> bool:
    """Whether a capture could run here at all: Node on PATH, the harness install present and
    the configured engine's browser installed."""
    engine = engine or str(getattr(settings, "BROWSER_CAPTURE_ENGINE", DEFAULT_ENGINE))
    executable = str(getattr(settings, "BROWSER_CAPTURE_EXECUTABLE", "") or "")
    return (
        shutil.which("node") is not None
        and (playwright_dir() / "playwright").is_dir()
        and engine_installed(engine, executable)
    )


def _subprocess_env(directory: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MARCELINE_PLAYWRIGHT_DIR"] = str(directory)
    if NSS_FALLBACK.is_dir():
        paths = [str(child) for child in sorted(NSS_FALLBACK.iterdir()) if child.is_dir()]
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            os.pathsep.join([*paths, existing]) if existing else os.pathsep.join(paths)
        )
    return env


class NodePlaywrightRunner:
    """Runs `capture.mjs` once per page, one browser at a time, and kills it on timeout."""

    def __init__(self, *, node: str = "node", directory: Path | None = None) -> None:
        self._node = node
        self._directory = directory or playwright_dir()

    def capture(
        self,
        request: CaptureRequest,
        decide: Callable[[Loaded], Decision],
        check: RequestCheck,
    ) -> CaptureOutcome:
        if request.engine not in ENGINES:
            raise BrowserUnavailableError(
                f"unknown browser engine {request.engine!r}; one of {', '.join(ENGINES)}"
            )
        if shutil.which(self._node) is None:
            raise BrowserUnavailableError(f"{self._node} is not on PATH")
        if not (self._directory / "playwright").is_dir():
            raise BrowserUnavailableError(
                f"Playwright is not installed in {self._directory}. Once, from frontend/parity:\n"
                "  npm install && npx playwright install chromium"
            )
        process = subprocess.Popen(
            [self._node, str(SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_subprocess_env(self._directory),
            text=True,
            bufsize=1,
        )
        errors: deque[str] = deque(maxlen=STDERR_LINES_KEPT)
        drain = threading.Thread(target=self._drain, args=(process, errors), daemon=True)
        drain.start()
        watchdog = threading.Timer(request.plan.timeout_s + KILL_MARGIN_S, process.kill)
        watchdog.start()
        try:
            return self._talk(process, request, decide, check, errors)
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()

    @staticmethod
    def _drain(process: "subprocess.Popen[str]", errors: deque[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            errors.append(line.rstrip())

    def _talk(
        self,
        process: "subprocess.Popen[str]",
        request: CaptureRequest,
        decide: Callable[[Loaded], Decision],
        check: RequestCheck,
        errors: deque[str],
    ) -> CaptureOutcome:
        assert process.stdin is not None and process.stdout is not None
        self._write(process, request.as_json())
        first = self._next_event(process, check, errors)
        if first.get("event") == "done":
            # The browser never reached the page. A launch failure is a configuration problem and
            # must not burn the queue, so it stops the run instead of failing this candidate.
            reason = str(first.get("skipReason") or "the browser produced no result")
            if reason.startswith("error:"):
                raise BrowserUnavailableError(f"{reason} ({'; '.join(errors) or 'no output'})")
            return self._outcome(first, None)
        if first.get("event") != "loaded":
            raise BrowserUnavailableError(f"unexpected browser message: {first.get('event')!r}")

        loaded = Loaded.from_json(first)
        decision = decide(loaded)
        self._write(process, decision.as_json())
        if decision.click_index is not None:
            # The one click of this visit. Straight-line code, not a loop: whatever the decider
            # answers to the page it sees *after* the click, no second click can be sent.
            after = self._next_event(process, check, errors)
            if after.get("event") != "loaded":
                return self._outcome(after, loaded)
            loaded = Loaded.from_json(after)
            self._write(process, decide(loaded).as_json())
        final = self._next_event(process, check, errors)
        return self._outcome(final, loaded)

    def _next_event(
        self, process: "subprocess.Popen[str]", check: RequestCheck, errors: deque[str]
    ) -> dict[str, Any]:
        """The next `loaded`/`done` message, answering every request check on the way.

        The browser holds each request until its answer arrives, so nothing the policy has not
        seen is ever sent.
        """
        while True:
            message = self._read(process, errors)
            if message.get("event") != "check":
                return message
            refusal = check(
                BrowserRequest(
                    url=str(message.get("url") or ""),
                    resource_type=str(message.get("type") or ""),
                    subframe=bool(message.get("subframe")),
                )
            )
            self._write(
                process, {"action": "check", "id": message.get("id"), "allow": refusal is None}
            )

    @staticmethod
    def _outcome(message: Mapping[str, Any], loaded: Loaded | None) -> CaptureOutcome:
        frames = tuple(
            CapturedFrame(float(frame["position"]), b64decode(frame["png"]))
            for frame in message.get("frames") or []
        )
        return CaptureOutcome(
            loaded=loaded,
            frames=frames,
            stats=CaptureStats.from_json(message.get("stats") or {}),
            skip_reason=str(message.get("skipReason") or ""),
        )

    @staticmethod
    def _write(process: "subprocess.Popen[str]", message: Mapping[str, Any]) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(f"{json.dumps(message)}\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise BrowserUnavailableError(f"the browser process closed early: {exc}") from exc

    @staticmethod
    def _read(process: "subprocess.Popen[str]", errors: deque[str]) -> dict[str, Any]:
        assert process.stdout is not None
        line = process.stdout.readline()
        if not line:
            raise BrowserUnavailableError(
                f"the browser process produced no output ({'; '.join(errors) or 'no stderr'})"
            )
        try:
            message: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BrowserUnavailableError(f"unreadable browser output: {exc}") from exc
        return message
