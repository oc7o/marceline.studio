"""Browser capture worker (ADR 0016; the operator enabled capture on 2026-09-16).

Takes candidates the existing triage queued — and only those — opens each page in a headless
browser, samples frames from the player and stores **PDQ hashes only**. No frame, screenshot or
video is ever written to disk or to the database.

The order of operations is the safety design, and it is the reason this file is short:

1. `next_capture_batch` offers only queued, gate-checked candidates inside the window and within
   the daily budgets (ADR 0016). A queue filter is not a guarantee.
2. The row is claimed with `SELECT … FOR UPDATE SKIP LOCKED`, so two workers cannot take one page.
3. robots.txt is read for the page URL and the per-site delay is honoured. A browser must not
   become a way around the crawler's own policy.
4. `marceline.capture.guard.capture_page` re-reads the page's safety state from the database
   **immediately before** the browser is pointed at the URL, exactly as `index/media_fetch.py`
   does for image fetches, and refuses without starting a browser.
5. The loaded page is judged by the crawler's own detector (`crawler.blocking.detect_block`). A
   block, challenge, login page or age gate is abandoned with a reason — never worked around.
"""

import logging
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from PIL import Image

from catalog.models import Site, SiteHost
from catalog.request_accounting import ROBOTS
from catalog.services import record_robots_crawl_delay
from catalog.sources import AgeConfirmationError, parse_age_confirmation
from core.http import PoliteClient, RequestLabel
from core.log_scrub import scrub_urls
from core.robots import InvalidRobotsUrlError
from crawler.blocking import detect_block
from crawler.robots import RobotsCache, RobotsUnavailableError, effective_delay_s
from index.models import Media, Page
from marceline.capture.click import ClickPolicy
from marceline.capture.diagnosis import NO_PLAYER_AFTER_CLICK, no_video_reason, too_small
from marceline.capture.guard import (
    CaptureDenied,
    capture_denial,
    capture_denial_detail,
    capture_page,
)
from marceline.capture.plan import (
    DUPLICATE_DISTANCE,
    CapturePlan,
    CapturePlanError,
    sample_positions,
)
from marceline.capture.requests import (
    ROBOTS_DISALLOWED,
    RequestPolicy,
    embed_host_allowed,
    page_robots_refusal,
)
from marceline.capture.runner import (
    DEFAULT_ENGINE,
    BrowserRunner,
    BrowserUnavailableError,
    CapturedFrame,
    CaptureOutcome,
    CaptureRequest,
    Decision,
    Loaded,
)
from matching.capture_triage import budget_view, capture_config, next_capture_batch
from matching.frames import FRAME_MAX_WIDTH, drop_near_duplicates, hash_frames
from matching.media import store_hashes
from matching.models import BrowserCaptureCandidate
from matching.pdq import MIN_QUALITY, HashedFrame, MediaDecodeError, open_image

logger = logging.getLogger(__name__)

# The capture media row's URL: the page's own URL plus this fragment, so it can never collide with
# a real media URL and is unmistakable in the admin. It is never requested (`eligible_at` is NULL).
CAPTURE_URL_FRAGMENT = "#browser-capture"
# Session-level advisory lock: one capture worker per database (scheduler uses 73428200).
LOCK_KEY = 73428300
ERROR_MAX_LENGTH = 5000

# Skip reasons that mean "this page has been looked at, don't come back for the same video"
# (ADR 0016 "Scan once": DRM, block, no video, ad wall). They are stored with a `capture_run`.
SCANNED_ONCE_PREFIXES = ("drm", "no_video", "no_frames", "no_quality_frames", "blocked:", "HTTP ")


@dataclass
class _HostRefusalTotal:
    count: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    paths: set[str] = field(default_factory=set)


@dataclass
class _ClickTotals:
    """How often the one allowed play click was used, and what came of it (issue #78).

    Counted off `capture_run.diagnostics.click` — the selector *shapes* clicked, the refusal
    reasons of visits where the click was allowed but no element qualified, and whether a player
    existed afterwards. Shapes and counts only: never an element's text, never a URL. This is the
    number that decides whether the click earns its place on a site.
    """

    offered: int = 0
    clicked: int = 0
    performed: int = 0
    with_video: int = 0
    shapes: Counter[str] = field(default_factory=Counter)
    refusals: Counter[str] = field(default_factory=Counter)

    def add(self, record: object) -> None:
        if not isinstance(record, dict) or not record.get("allowed"):
            return
        self.offered += 1
        if record.get("chosen"):
            self.clicked += 1
            self.shapes[str(record.get("shape") or "?")] += 1
            if record.get("performed"):
                self.performed += 1
            if record.get("video_after_click"):
                self.with_video += 1
        elif record.get("reason"):
            self.refusals[str(record["reason"])] += 1

    def lines(self) -> list[str]:
        if not self.offered:
            return []
        out = [
            f"Play click (ADR 0016 amendment): allowed on {self.offered} visit(s), "
            f"an element chosen on {self.clicked}, clicked on {self.performed}, "
            f"a player afterwards on {self.with_video}"
        ]
        if self.shapes:
            out.append(
                "  shapes: "
                + ", ".join(f"{shape}={count}" for shape, count in sorted(self.shapes.items()))
            )
        if self.refusals:
            out.append(
                "  no element qualified: "
                + ", ".join(f"{reason}={count}" for reason, count in sorted(self.refusals.items()))
            )
        return out


def refusal_report(site_domain: str | None = None) -> list[str]:
    """Which host blocked us: every refusal any past visit recorded, reconstructed.

    Reads `capture_run.diagnostics.refused_hosts` (requests refused mid-visit: an embed host not
    allowlisted, or a host's own robots.txt) and `refusal_detail` (a visit that never started at
    all: the page's own robots.txt, or a safety/availability deny) off `BrowserCaptureCandidate`
    rows — read-only, no browser, no network, answerable even with capture disabled. `allowed`
    marks a host already covered by the embed allowlist rule: the page's own site, any of its
    recorded hosts, or (unconditionally, for every visit) any *enabled* `catalog.Site` domain
    (`CaptureWorker._request_policy`) — so an enabled site's own domain never needs a separate
    allowlist entry.
    """
    status = BrowserCaptureCandidate.Status
    rows = BrowserCaptureCandidate.objects.exclude(status=status.QUEUED)
    if site_domain:
        rows = rows.filter(site__domain=site_domain)
    by_host: dict[str, _HostRefusalTotal] = defaultdict(_HostRefusalTotal)
    denied_rules: Counter[str] = Counter()
    denied_pages: set[int] = set()
    clicks = _ClickTotals()
    for capture_run, detail, page_id in rows.values_list(
        "capture_run", "refusal_detail", "page_id"
    ):
        clicks.add(((capture_run or {}).get("diagnostics") or {}).get("click"))
        for entry in ((capture_run or {}).get("diagnostics") or {}).get("refused_hosts", ()):
            total = by_host[entry["host"]]
            total.count += entry["count"]
            total.reasons[entry["reason"]] += entry["count"]
            total.paths.update(entry.get("paths", ()))
        detail = detail or {}
        if detail.get("kind") == ROBOTS_DISALLOWED and detail.get("host"):
            total = by_host[detail["host"]]
            total.count += 1
            total.reasons[ROBOTS_DISALLOWED] += 1
            if detail.get("path"):
                total.paths.add(detail["path"])
        elif detail.get("kind") == "capture_denied":
            denied_rules[detail.get("rule", "unknown")] += 1
            denied_pages.add(page_id)

    enabled_domains = frozenset(Site.objects.filter(enabled=True).values_list("domain", flat=True))
    site_hosts = frozenset(SiteHost.objects.values_list("host", flat=True))

    lines = [f"Capture refusals{f' ({site_domain})' if site_domain else ''}:"]
    if not by_host and not denied_rules:
        lines.append("  none recorded")
        return lines + clicks.lines()
    for host in sorted(by_host):
        total = by_host[host]
        allowed = embed_host_allowed(host, enabled_domains) or embed_host_allowed(host, site_hosts)
        reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(total.reasons.items()))
        paths = ", ".join(sorted(total.paths)) or "-"
        lines.append(
            f"  {host}: {total.count} refusal(s) [{reasons}] paths: {paths} "
            f"({'already allowlisted' if allowed else 'not allowlisted'})"
        )
    if denied_rules:
        lines.append(
            "  safety/availability denies (page id + rule; look up a page id in the admin for "
            "more): "
            + ", ".join(f"{rule}={count}" for rule, count in sorted(denied_rules.items()))
            + f" across {len(denied_pages)} page(s)"
        )
    return lines + clicks.lines()


def plan_from_settings() -> CapturePlan:
    """The caps one capture runs under, from BROWSER_CAPTURE_* settings."""
    return CapturePlan(
        max_frames=int(getattr(settings, "BROWSER_CAPTURE_MAX_FRAMES", 24)),
        max_seconds=float(getattr(settings, "BROWSER_CAPTURE_MAX_SECONDS", 120.0)),
        max_bytes=int(getattr(settings, "BROWSER_CAPTURE_MAX_BYTES", 200 * 1024 * 1024)),
        timeout_s=float(getattr(settings, "BROWSER_CAPTURE_TIMEOUT_S", 300.0)),
    )


@contextmanager
def single_capture_lock() -> Iterator[bool]:
    """Hold a session-level Postgres advisory lock; yields False if another worker holds it."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        row = cursor.fetchone()
    acquired = bool(row and row[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])


@dataclass
class CaptureReport:
    """What a run did and what it cost. Printed by the command; asserted in tests."""

    captured: int = 0
    skipped: int = 0
    failed: int = 0
    frames: int = 0
    bytes: int = 0
    wall_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def visits(self) -> int:
        return self.captured + self.skipped + self.failed

    def lines(self) -> list[str]:
        lines = [
            f"Browser capture: {self.visits} visit(s); captured {self.captured}, "
            f"skipped {self.skipped}, failed {self.failed}; "
            f"{self.frames} frame hashes stored"
        ]
        if self.visits:
            lines.append(
                f"Cost: {self.wall_ms / 1000:.1f} s wall, {self.bytes / 1_048_576:.1f} MiB pulled "
                f"({self.wall_ms / self.visits / 1000:.1f} s and "
                f"{self.bytes / self.visits / 1_048_576:.1f} MiB per visit)"
            )
        lines.extend(self.notes)
        return lines


def claim_candidate(candidate_id: int) -> BrowserCaptureCandidate | None:
    """Take one queued row for this worker, or None if another worker got there first."""
    with transaction.atomic():
        row = (
            BrowserCaptureCandidate.objects.select_for_update(skip_locked=True)
            .select_related("page__site", "site")
            .filter(pk=candidate_id, status=BrowserCaptureCandidate.Status.QUEUED)
            .first()
        )
        if row is None:
            return None
        row.attempts += 1
        row.save(update_fields=("attempts", "updated_at"))
        return row


def _downscale(image: Image.Image) -> Image.Image:
    """Match the server's video recipe (`scale='min(512,iw)':-2`) so hashes are comparable."""
    if image.width <= FRAME_MAX_WIDTH:
        return image
    height = max(1, round(image.height * FRAME_MAX_WIDTH / image.width))
    return image.resize((FRAME_MAX_WIDTH, height), Image.Resampling.BILINEAR)


class CaptureWorker:
    def __init__(
        self,
        runner: BrowserRunner,
        client: PoliteClient,
        *,
        user_agent: str | None = None,
        plan: CapturePlan | None = None,
        engine: str | None = None,
        executable_path: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._user_agent = user_agent or str(settings.CRAWLER_USER_AGENT)
        self._engine = engine or str(getattr(settings, "BROWSER_CAPTURE_ENGINE", DEFAULT_ENGINE))
        self._executable_path = (
            executable_path
            if executable_path is not None
            else str(getattr(settings, "BROWSER_CAPTURE_EXECUTABLE", "") or "")
        )
        # The request policy of the visit in progress (robots.txt per host, embed allowlist).
        self._policy: RequestPolicy | None = None
        # The click budget of the visit in progress (ADR 0016 amendment 2026-09-18, issue #78).
        # A fresh `ClickPolicy` per visit is what makes "one click per visit" a property of the
        # object rather than of anyone remembering to count.
        self._click = ClickPolicy()
        # robots.txt requests are counted for the candidate's site but never refused by a budget:
        # capture has its own visit budgets (ADR 0016), and the browser's own requests do not go
        # through PoliteClient at all (issue #74).
        self._site_id: int | None = None
        self._robots = RobotsCache(
            lambda url, delay: client.get(
                url,
                min_delay_s=delay,
                label=RequestLabel(ROBOTS, site_id=self._site_id, enforce=False),
            ),
            self._user_agent,
        )
        self._plan = plan or plan_from_settings()
        self._sleep = sleep
        self._clock = clock
        self._last_visit_at: dict[str, float] = {}

    # --- the run loop ---------------------------------------------------------------------

    def run(
        self,
        *,
        site_domain: str | None = None,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> CaptureReport:
        """Capture queued candidates until the budget, the window or `limit` stops the run."""
        report = CaptureReport()
        config = capture_config()
        if not config.enabled:
            report.notes.append("browser capture is disabled (BROWSER_CAPTURE_ENABLED is false)")
            return report
        view = budget_view(now or timezone.now())
        if not view.inside:
            report.notes.append(
                f"outside the capture window {view.window_start:%H:%M}-{view.window_end:%H:%M} UTC"
            )
            return report
        while limit is None or report.visits < limit:
            batch = next_capture_batch(timezone.now(), site_domain=site_domain)
            if not batch:
                break
            progressed = False
            for candidate in batch:
                if limit is not None and report.visits >= limit:
                    break
                claimed = claim_candidate(candidate.pk)
                if claimed is None:
                    continue
                progressed = True
                try:
                    self.process(claimed, report)
                except BrowserUnavailableError as exc:
                    # A configuration problem, not a page problem: leave the row queued (it was
                    # never decided) and stop, rather than burning three attempts on every row.
                    report.notes.append(f"stopped: {exc}")
                    return report
            if not progressed:
                break
        if not report.visits and not report.notes:
            report.notes.append(
                f"nothing to capture: {view.used_total}/{config.total_per_day} visits used, "
                f"{view.queued} queued"
            )
        return report

    # --- one candidate --------------------------------------------------------------------

    def process(self, candidate: BrowserCaptureCandidate, report: CaptureReport) -> str:
        """Visit one claimed candidate. Always leaves the row in a decided state."""
        page, site = candidate.page, candidate.site
        # Cheap fail-fast so a denied page costs not even a robots.txt request. The *binding*
        # decision is taken inside `capture_page` below, against the database, immediately before
        # the browser starts (ADR 0018's rule for media fetches, applied to browser visits).
        denial = capture_denial(page.pk)
        if denial is not None:
            return self._retryable_skip(
                candidate,
                report,
                f"denied: {denial}",
                refusal_detail=capture_denial_detail(page.pk, denial),
            )
        self._site_id = site.pk
        try:
            rules = self._robots.rules_for(page.url, min_delay_s=site.crawl_delay_s)
        except RobotsUnavailableError as exc:
            return self._fail(candidate, report, f"robots.txt unavailable: {exc}")
        record_robots_crawl_delay(site, rules.crawl_delay_s)
        host = (urlsplit(page.url).hostname or "").lower()
        # The robots fetch itself is a request to this host; space the visit from it too.
        self._last_visit_at.setdefault(host, self._clock())
        if not rules.can_fetch(page.url):
            return self._retryable_skip(
                candidate,
                report,
                "disallowed by robots.txt",
                refusal_detail=page_robots_refusal(page.url),
            )
        self._wait_turn(host, effective_delay_s(site.crawl_delay_s, rules))

        play_click, cookies = self._site_options(site)
        self._click = ClickPolicy(allowed=play_click)
        request = CaptureRequest(
            url=page.url,
            user_agent=self._user_agent,
            plan=self._plan,
            engine=self._engine,
            executable_path=self._executable_path,
            allow_play_click=play_click,
            cookies=cookies,
            cookie_domain=site.domain,
        )
        self._policy = self._request_policy(site)
        try:
            # The deny is re-read from the database inside this call, immediately before the
            # browser is pointed at the URL. Nothing else may start a browser.
            outcome = capture_page(
                self._runner,
                page_id=page.pk,
                request=request,
                decide=self._decide,
                check=self._policy.refusal,
            )
        except CaptureDenied as denied:
            # Not "scanned once": a page cleared by a reviewer later may be captured after all.
            return self._retryable_skip(
                candidate,
                report,
                f"denied: {denied.reason}",
                refusal_detail=capture_denial_detail(page.pk, denied.reason),
            )

        report.wall_ms += outcome.stats.wall_ms
        report.bytes += outcome.stats.bytes
        if outcome.skip_reason:
            return self._skip(candidate, report, outcome.skip_reason, outcome)

        positions, hashed = self._hash(outcome.frames)
        if not hashed:
            return self._skip(candidate, report, "no_quality_frames", outcome)
        media = self._store(page, outcome, hashed)
        report.captured += 1
        report.frames += len(hashed)
        logger.info(
            "captured page %s (%s): %d frame hashes from %d samples, %.1f s, %.1f MiB",
            page.pk,
            site.domain,
            len(hashed),
            len(outcome.frames),
            outcome.stats.wall_ms / 1000,
            outcome.stats.bytes / 1_048_576,
        )
        self._finish(
            candidate,
            BrowserCaptureCandidate.Status.CAPTURED,
            "",
            self._record(outcome, positions=positions, stored=len(hashed), media_id=media.pk),
        )
        return "captured"

    def _site_options(self, site: Site) -> tuple[bool, tuple[tuple[str, str], ...]]:
        """The two per-site operator switches this visit runs under, read from the database now.

        `capture_play_click` (issue #78): may the browser click this site's own play control,
        once? `age_confirmation` (ADR 0007's amendment): the approved age **self-declaration**
        cookies, the same object the HTTP crawler sends — never a verification gate, which
        `catalog.sources.parse_age_confirmation` cannot express and which triage excludes anyway.
        Both are read fresh rather than from the worker's in-memory copy of the site, because both
        are operator decisions that may have changed since the row was queued.
        """
        row = (
            Site.objects.filter(pk=site.pk)
            .values_list("capture_play_click", "age_confirmation")
            .first()
        )
        if row is None:
            return False, ()
        play_click, age_confirmation = row
        try:
            confirmation = parse_age_confirmation(age_confirmation)
        except AgeConfirmationError as exc:
            # Operator-entered config, not page content, but scrubbed like the crawler does.
            logger.warning(
                "ignoring invalid age_confirmation for %s: %s", site.domain, scrub_urls(str(exc))
            )
            confirmation = None
        cookies = tuple(confirmation.cookies.items()) if confirmation else ()
        return bool(play_click), cookies

    def _decide(self, loaded: Loaded) -> Decision:
        """What to do with the loaded page. Nothing here works around anything."""
        if loaded.click_performed:
            self._click.performed = True
        block = detect_block(
            loaded.status, {}, loaded.html, loaded.url, link_count=loaded.link_count
        )
        if block:
            # Login, CAPTCHA, paywall and age gates all land here and are abandoned. The crawler's
            # detector is reused so the browser follows exactly the same policy (ADR 0007). This
            # runs *before* any click is offered, so nothing is ever clicked on a gated page.
            return Decision.abort(f"blocked: {block}")
        if loaded.status >= 400:
            return Decision.abort(f"HTTP {loaded.status}")
        if loaded.drm:
            return Decision.abort("drm")
        if not loaded.video_found or loaded.duration_s is None:
            # One click on the page's own play control, if the operator opted this site in and
            # the pure policy admits an element (`marceline.capture.click`). Never a second one:
            # once the single click is spent the page is left as it is.
            if self._click.spent:
                return Decision.abort(NO_PLAYER_AFTER_CLICK)
            target = self._click.decide(loaded.click_candidates)
            if target is not None:
                return Decision.click(target.index)
            return Decision.abort(no_video_reason(loaded, self._policy))
        self._click.video_after_click = bool(self._click.clicks_used)
        if too_small(loaded):
            width, height = loaded.rendered
            return Decision.abort(f"no_video: player drawn at {width}x{height} px")
        try:
            return Decision.capture(sample_positions(loaded.duration_s, self._plan))
        except CapturePlanError as exc:
            return Decision.abort(f"no_video: {exc}")

    def _request_policy(self, site: Site) -> RequestPolicy:
        """What the browser may request on this visit (ADR 0016 amendment 2026-09-17).

        Embedded players may come from the page's own site, any enabled site, or a host recorded
        for this site; every request must pass its host's robots.txt, fetched once per origin.
        """
        allowed = {
            site.domain,
            *SiteHost.objects.filter(site=site).values_list("host", flat=True),
            *Site.objects.filter(enabled=True).values_list("domain", flat=True),
        }

        def robots_allows(url: str) -> bool:
            try:
                rules = self._robots.rules_for(url, min_delay_s=site.crawl_delay_s)
            except InvalidRobotsUrlError:
                return False
            return rules.can_fetch(url)

        return RequestPolicy(
            allowed_embed_domains=frozenset(domain for domain in allowed if domain),
            robots_allows=robots_allows,
        )

    # --- hashing and storage --------------------------------------------------------------

    def _hash(self, frames: Sequence[CapturedFrame]) -> tuple[list[float], list[HashedFrame]]:
        """PDQ-hash the sampled frames with the existing pipeline; the bytes are dropped here.

        Returns the positions of the frames that decoded, in order, so `frame_index` (the sample
        ordinal `hash_frames` assigns) indexes straight into them.
        """
        positions: list[float] = []
        images: list[Image.Image] = []
        for frame in frames:
            try:
                images.append(_downscale(open_image(frame.png)))
            except MediaDecodeError:
                logger.warning("undecodable capture frame at %.1fs", frame.position_s)
                continue
            positions.append(frame.position_s)
        hashed = hash_frames(images, MIN_QUALITY)
        return positions, drop_near_duplicates(hashed, DUPLICATE_DISTANCE)

    def _store(self, page: Page, outcome: CaptureOutcome, hashed: Sequence[HashedFrame]) -> Media:
        """One capture media row per page, holding the frame hashes. Never fetchable."""
        media, _ = Media.objects.update_or_create(
            page=page,
            url=f"{page.url}{CAPTURE_URL_FRAGMENT}"[:2000],
            defaults={
                "kind": Media.Kind.CAPTURE,
                # NULL forever: `index.media_fetch` denies any row without eligibility, so no
                # fetch path can ever request this URL (ADR 0018).
                "eligible_at": None,
                "content_type": "",
                "byte_size": None,
                "attempts": 0,
                "next_attempt_at": None,
                "locked_at": None,
                "last_error": "",
            },
        )
        # store_hashes re-checks the page gate and drops everything if the page stopped being
        # clearly safe while the browser was working.
        store_hashes(
            media,
            list(hashed),
            sha256="",
            width=outcome.loaded.width if outcome.loaded else None,
            height=outcome.loaded.height if outcome.loaded else None,
        )
        return media

    # --- bookkeeping ----------------------------------------------------------------------

    def _record(
        self,
        outcome: CaptureOutcome,
        *,
        positions: Sequence[float] = (),
        stored: int = 0,
        media_id: int | None = None,
        skip_reason: str = "",
    ) -> dict[str, Any]:
        """The `capture_run` record: what it did and what it cost — never any image data."""
        loaded = outcome.loaded
        return {
            "run_id": uuid.uuid4().hex,
            "at": timezone.now().isoformat(),
            "media_id": media_id,
            "skip_reason": skip_reason,
            "frames_sampled": len(outcome.frames),
            "frames_stored": stored,
            # What the player was *asked* to seek to: cost and debugging metadata, never evidence.
            # Players report one position and can deliver another, and a leak is often an excerpt,
            # so no stored evidence says "the frame at t=X" (operator, 2026-09-17). Matching orders
            # capture frames by their sample ordinal (`matching.timeline`, Ordering.CLIP) and lets
            # the offset to the original fall out of a run of matching frames.
            "positions_s": list(positions),
            "video": {
                "duration_s": loaded.duration_s if loaded else None,
                "width": loaded.width if loaded else None,
                "height": loaded.height if loaded else None,
                "rendered": list(loaded.rendered) if loaded else None,
            },
            # Text-only evidence for the skip reason (issue #78): element states, player
            # libraries, and what the request policy refused. Never a URL or a pixel.
            "diagnostics": {
                "engine": self._engine,
                "videos": [video.as_json() for video in loaded.videos] if loaded else [],
                "play_control": loaded.play_control if loaded else False,
                "players": list(loaded.players) if loaded else [],
                "refused": self._policy.summary() if self._policy else {},
                # The one allowed play click (issue #78): whether it was allowed for this site,
                # chosen, performed, on what *selector shape* and what came of it. Never the
                # element's text, its ARIA label or a URL — `click.ClickPolicy.as_json`.
                "click": self._click.as_json(),
                # Per host: reason, count and path shapes — never a URL (issue #78 amendment
                # 2026-09-18). What makes "which host blocked us" answerable after the fact.
                "refused_hosts": self._policy.host_refusals() if self._policy else [],
            },
            "cost": outcome.stats.as_json(),
            "caps": self._plan.as_json(),
        }

    def _finish(
        self,
        candidate: BrowserCaptureCandidate,
        status: str,
        error: str,
        capture_run: dict[str, Any],
        *,
        refusal_detail: dict[str, Any] | None = None,
    ) -> None:
        candidate.status = status
        candidate.last_error = error[:ERROR_MAX_LENGTH]
        candidate.decided_at = timezone.now()
        candidate.capture_run = capture_run
        # Distinct from capture_run (ADR 0016's Scan once marker): a retryable skip that never
        # opened a browser stays retryable, but its host/page detail is still reconstructable.
        candidate.refusal_detail = dict(refusal_detail or {})
        candidate.save(
            update_fields=(
                "status",
                "last_error",
                "decided_at",
                "capture_run",
                "refusal_detail",
                "updated_at",
            )
        )

    def _skip(
        self,
        candidate: BrowserCaptureCandidate,
        report: CaptureReport,
        reason: str,
        outcome: CaptureOutcome,
    ) -> str:
        """A skip the page itself caused (DRM, no video, a block). Counts as scanned once."""
        scanned_once = reason.startswith(SCANNED_ONCE_PREFIXES)
        self._finish(
            candidate,
            BrowserCaptureCandidate.Status.SKIPPED,
            f"capture: {reason}",
            self._record(outcome, skip_reason=reason) if scanned_once else {},
        )
        report.skipped += 1
        logger.info("skipped page %s: %s", candidate.page_id, reason)
        return "skipped"

    def _retryable_skip(
        self,
        candidate: BrowserCaptureCandidate,
        report: CaptureReport,
        reason: str,
        *,
        refusal_detail: dict[str, Any] | None = None,
    ) -> str:
        """A skip our own policy caused (a deny, robots). No `capture_run`, so triage may queue
        the page again once the reason disappears. `refusal_detail`, when given, is stored on its
        own field so the skip stays reconstructable without affecting that re-queueing."""
        self._finish(
            candidate,
            BrowserCaptureCandidate.Status.SKIPPED,
            f"capture: {reason}",
            {},
            refusal_detail=refusal_detail,
        )
        report.skipped += 1
        logger.info("skipped page %s: %s", candidate.page_id, reason)
        return "skipped"

    def _fail(self, candidate: BrowserCaptureCandidate, report: CaptureReport, error: str) -> str:
        self._finish(candidate, BrowserCaptureCandidate.Status.FAILED, f"capture: {error}", {})
        report.failed += 1
        # `error` may wrap a `RobotsUnavailableError`, which quotes the robots.txt origin it
        # could not fetch; the row above keeps the full text, the log does not.
        logger.warning("capture failed for page %s: %s", candidate.page_id, scrub_urls(error))
        return "failed"

    def _wait_turn(self, host: str, delay_s: float) -> None:
        last = self._last_visit_at.get(host)
        if last is not None:
            wait = last + delay_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_visit_at[host] = self._clock()
