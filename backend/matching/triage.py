"""Pure triage for a future headless-browser frame capture (ADR 0016). No ORM, no clock.

A browser visit is slow and heavy, so only uncertain cases where more frames
can change a decision are queued. Each source gives a reason code and a priority component;
`combine_priority` turns the components of one page into one number. Hard exclusions never
queue a page. DB collection lives in `matching.capture_triage`.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit

from matching.duration import duration_tolerance

# Reason codes (stored in BrowserCaptureCandidate.reason).
REVIEW_BAND_MATCH = "review_band_match"
CONFIRMED_LEAK_WITHOUT_FRAMES = "confirmed_leak_without_frames"
LINKED_CLUSTER_MEMBER = "linked_cluster_member"
EXACT_DURATION_SINGLE_THUMB = "exact_duration_single_thumb"
METADATA_CANDIDATE = "metadata_candidate"

# Source weights. An undecided, near-high match is the case capture decides best, so it leads;
# a human-confirmed leak without frames gains evidence (weak -> strong) but no decision.
REVIEW_MATCH_WEIGHT = 1.0
REVIEW_MATCH_FLOOR = 0.6  # share of the weight at confidence 0; the rest scales to the threshold
REVIEW_EXACT_DURATION_BONUS = 0.1  # page duration within EXACT_DURATION_S of the reference
CONFIRMED_LEAK_WEIGHT = 0.7
LINKED_CLUSTER_WEIGHT = 0.6
EXACT_DURATION_WEIGHT = 0.4
# The third signal (ADR 0040 amendment 2026-09-18): the page's own stored text is consistent with
# an original. It is the only source that rests on nothing of ours — no picture, no human, no
# cluster — so it ranks last, below the exact-duration coincidence that is a special case of it.
# Corroborated: the page shares a cluster or mirror group with a page confirmed for the same
# original, which is a second, independent signal saying "look here". Still below a review-band
# match: better aim, never a stronger claim.
METADATA_CANDIDATE_WEIGHT = 0.3
METADATA_CORROBORATED_WEIGHT = 0.55
# Combination: the strongest component leads, every further source adds a share of its own.
SECONDARY_SOURCE_SHARE = 0.25
# Fewer quality-eligible frame hashes -> more to gain: weight * (1 - frames / FEW_FRAME_HASHES).
FRAME_POVERTY_WEIGHT = 0.15

# Thresholds.
EXACT_DURATION_S = 1.5
FEW_FRAME_HASHES = 10  # "too few" for confirmed leaks (research: 10 samples ~85 % at p = 0.3)
SINGLE_THUMB_MAX_FRAMES = 1
CONFIRMED_HIGH_MIN_EVIDENCE = 3  # confirmed + band high + this many evidence frames: no gain
PRIORITY_DIGITS = 4

# Exclusion codes, in the order they are reported (the first page-level one counts).
SAFETY_NOT_CLEAR = "safety_not_clear"
SAFETY_MATCHES = "safety_matches"
REMOVED = "removed"
SITE_DISABLED = "site_disabled"
SITE_CAPTURE_DISABLED = "site_capture_disabled"  # operator switch (issue #78), not site-disabled
PAGE_INACTIVE = "page_inactive"  # deactivated as untargeted (issue #71): kept, never captured
AGE_VERIFICATION = "age_verification_provider"
AGE_GATE_UNAPPROVED = "age_gate_unapproved"
PRIVATE_PAGE = "private_or_login_page"
MEDIA_PENDING = "media_pending"  # the cheap HTTP media path runs first
ALREADY_CAPTURED = "already_captured"
# Metadata-source exclusions (ADR 0040 amendment): not page-level rules, because the other four
# sources rest on our own evidence and carry their own justification onto a page. A metadata
# candidate rests on the site's own text, so it must clear both of these before it aims a browser.
MEDIA_GATE = "media_gate"  # ADR 0018: the ordinary media path would refuse this page a poster
ENOUGH_FRAMES = "enough_frames"  # PDQ has had a proper look here and said nothing
PAIR_ALREADY_PROPOSED = "pair_already_proposed"  # another signal owns this pair
REJECTED_PAIR = "rejected_pair"
CONFIRMED_HIGH = "confirmed_high"
PAGE_EXCLUSIONS = (
    SAFETY_NOT_CLEAR,
    SAFETY_MATCHES,
    REMOVED,
    SITE_DISABLED,
    SITE_CAPTURE_DISABLED,
    PAGE_INACTIVE,
    AGE_VERIFICATION,
    AGE_GATE_UNAPPROVED,
    PRIVATE_PAGE,
    MEDIA_PENDING,
    ALREADY_CAPTURED,
)
PAIR_EXCLUSIONS = (REJECTED_PAIR, CONFIRMED_HIGH)
METADATA_EXCLUSIONS = (MEDIA_GATE, ENOUGH_FRAMES, PAIR_ALREADY_PROPOSED)

# Crawler gate labels (crawler/blocking.py; matching may not import crawler, so repeated).
GATE_SELF_DECLARATION = "age_self_declaration"
GATE_VERIFICATION_PROVIDER = "age_verification_provider"
# Login/account paths (a superset of crawler.blocking.LOGIN_PATH_PREFIXES).
PRIVATE_PATH_PREFIXES = (
    "/login",
    "/signin",
    "/sign-in",
    "/signup",
    "/sign-up",
    "/register",
    "/auth/",
    "/account",
    "/my/",
    "/members",
    "/private",
    "/premium",
)
PRIVATE_EXTRACTION_FLAGS = ("private", "login_required", "members_only", "premium")


@dataclass(frozen=True)
class Source:
    """One piece of evidence that a capture could help, for one (page, reference) pair."""

    reason: str
    reference_id: int | None
    component: float
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "reference_id": self.reference_id,
            "component": round(self.component, PRIORITY_DIGITS),
            **self.details,
        }


def exact_duration(page_s: float | None, reference_s: float | None) -> bool:
    if page_s is None or reference_s is None:
        return False
    return abs(page_s - reference_s) <= EXACT_DURATION_S


def review_match_component(confidence: float, high_threshold: float, exact: bool) -> float:
    """Rises with closeness of the confidence to the high threshold and with an exact duration."""
    closeness = min(max(confidence, 0.0) / high_threshold, 1.0) if high_threshold > 0 else 1.0
    base = REVIEW_MATCH_WEIGHT * (REVIEW_MATCH_FLOOR + (1 - REVIEW_MATCH_FLOOR) * closeness)
    return base + (REVIEW_EXACT_DURATION_BONUS if exact else 0.0)


def frame_poverty(eligible_frames: int) -> float:
    return FRAME_POVERTY_WEIGHT * max(0.0, 1 - max(eligible_frames, 0) / FEW_FRAME_HASHES)


def combine_priority(components: Iterable[float], eligible_frames: int) -> float:
    """Leading component + SECONDARY_SOURCE_SHARE * the others + frame poverty (0 without any)."""
    ordered = sorted((value for value in components if value > 0), reverse=True)
    if not ordered:
        return 0.0
    total = ordered[0] + SECONDARY_SOURCE_SHARE * sum(ordered[1:]) + frame_poverty(eligible_frames)
    return round(total, PRIORITY_DIGITS)


def leading(sources: Sequence[Source]) -> Source:
    """Highest component; ties go to the reason order above, then the smaller reference id."""
    order = (
        REVIEW_BAND_MATCH,
        CONFIRMED_LEAK_WITHOUT_FRAMES,
        LINKED_CLUSTER_MEMBER,
        EXACT_DURATION_SINGLE_THUMB,
        METADATA_CANDIDATE,
    )
    return min(
        sources,
        key=lambda item: (
            -round(item.component, PRIORITY_DIGITS),
            order.index(item.reason) if item.reason in order else len(order),
            item.reference_id or 0,
        ),
    )


@dataclass(frozen=True)
class PageFacts:
    safety_status: str
    safety_matches: object
    availability: str
    site_enabled: bool
    site_age_approved: bool
    url: str
    raw_extraction: object
    page_gate: str = ""  # crawler gate label last recorded for this page's URL
    site_gate: str = ""  # a gate label recorded anywhere on the site
    media_pending: bool = False  # media still pending/running in the HTTP media worker
    page_active: bool = True  # targeted indexing (issue #71)
    site_capture_enabled: bool = True  # per-site capture switch (ADR 0016 amendment, issue #78)


def private_page(url: str, raw_extraction: object) -> bool:
    """Login, account or members-only pages; never visited (guard, none are indexed today)."""
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return True
    if any(path.startswith(prefix) for prefix in PRIVATE_PATH_PREFIXES):
        return True
    return isinstance(raw_extraction, dict) and any(
        raw_extraction.get(flag) is True for flag in PRIVATE_EXTRACTION_FLAGS
    )


def page_exclusions(facts: PageFacts) -> list[str]:
    """Every hard page-level exclusion that applies, in PAGE_EXCLUSIONS order (without capture)."""
    reasons: list[str] = []
    if facts.safety_status != "clear":
        reasons.append(SAFETY_NOT_CLEAR)
    if facts.safety_matches:
        reasons.append(SAFETY_MATCHES)
    if facts.availability == "removed":
        reasons.append(REMOVED)
    if not facts.site_enabled:
        reasons.append(SITE_DISABLED)
    if not facts.site_capture_enabled:
        reasons.append(SITE_CAPTURE_DISABLED)
    if not facts.page_active:
        reasons.append(PAGE_INACTIVE)
    gates = (facts.page_gate, facts.site_gate)
    if any(gate.startswith(GATE_VERIFICATION_PROVIDER) for gate in gates):
        reasons.append(AGE_VERIFICATION)
    if not facts.site_age_approved and any(
        gate.startswith(GATE_SELF_DECLARATION) for gate in gates
    ):
        reasons.append(AGE_GATE_UNAPPROVED)
    if private_page(facts.url, facts.raw_extraction):
        reasons.append(PRIVATE_PAGE)
    if facts.media_pending:
        reasons.append(MEDIA_PENDING)
    return reasons


def metadata_exclusion(
    *, media_gate_allows: bool, eligible_frames: int, pair_proposed: bool
) -> str | None:
    """Why this metadata candidate may not aim a capture, or None (ADR 0040 amendment).

    `media_gate_allows` is `index.services.page_media_allowed` for the page: a browser pulls far
    more of a page than a poster fetch does, so it can never be allowed where a poster is not.
    The capture guard re-reads the same rule immediately before the browser opens; this is the
    earlier of the two checks, and neither is the other's excuse.
    """
    if not media_gate_allows:
        return MEDIA_GATE
    if eligible_frames >= FEW_FRAME_HASHES:
        return ENOUGH_FRAMES
    if pair_proposed:
        return PAIR_ALREADY_PROPOSED
    return None


def pair_exclusion(
    *, rejected: bool, confirmed: bool, band: str | None, evidence_frames: int
) -> str | None:
    """Human-rejected pairs never count; confirmed high-band pairs with enough evidence gain
    nothing from more frames."""
    if rejected:
        return REJECTED_PAIR
    if confirmed and band == "high" and evidence_frames >= CONFIRMED_HIGH_MIN_EVIDENCE:
        return CONFIRMED_HIGH
    return None


# --- Scan once: the URL/media basis of a capture -------------------------------------------------


def normalize_url(url: str) -> str:
    """host without www + path, lowercase host, no scheme, query or fragment."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return f"{host}{parsed.path.rstrip('/')}"


def capture_basis(url: str, duration_s: float | None, media_keys: Iterable[str]) -> dict[str, Any]:
    return {
        "url": normalize_url(url),
        "duration_s": duration_s,
        "media_keys": sorted(set(media_keys)),
    }


def changed_substantially(captured: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    """Why a captured page may be captured again, or None (scan once).

    - `url_changed`: the normalized URL differs;
    - `duration_changed`: both durations known and further apart than the cluster tolerance
      (max(30 s, 3 %)): another video under the same page;
    - `media_replaced`: both media key sets known and disjoint (every poster/frame/clip replaced).
    A captured row without a stored basis never counts as changed.
    """
    if not captured:
        return None
    if captured.get("url") and captured.get("url") != current.get("url"):
        return "url_changed"
    old, new = captured.get("duration_s"), current.get("duration_s")
    if (
        isinstance(old, (int, float))
        and isinstance(new, (int, float))
        and abs(old - new) > duration_tolerance(max(old, new))
    ):
        return "duration_changed"
    old_keys, new_keys = set(captured.get("media_keys") or ()), set(current.get("media_keys") or ())
    if old_keys and new_keys and not old_keys & new_keys:
        return "media_replaced"
    return None


# --- Budgets and window ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureWindow:
    start: time
    end: time

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_window(value: str) -> CaptureWindow:
    """`HH:MM-HH:MM` in UTC; the end may be past midnight (`22:00-02:00`)."""
    try:
        start_text, end_text = (part.strip() for part in value.split("-"))
        start, end = time.fromisoformat(start_text), time.fromisoformat(end_text)
    except ValueError as exc:
        raise ValueError(f"capture window must look like 01:00-05:00, got {value!r}") from exc
    if start == end:
        raise ValueError(f"capture window must not be empty, got {value!r}")
    return CaptureWindow(
        start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0)
    )


def window_bounds(now: datetime, window: CaptureWindow) -> tuple[datetime, datetime, bool]:
    """(start, end, inside) of the window containing `now` (UTC), else of the next one."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in (-1, 0, 1):
        start = day + timedelta(days=offset, hours=window.start.hour, minutes=window.start.minute)
        length = (
            timedelta(hours=window.end.hour, minutes=window.end.minute)
            - timedelta(hours=window.start.hour, minutes=window.start.minute)
        ) % timedelta(days=1)
        end = start + length
        if start <= now < end:
            return start, end, True
        if now < start:
            return start, end, False
    raise AssertionError("unreachable: a window starts within two days")  # pragma: no cover


@dataclass(frozen=True)
class QueuedItem:
    candidate_id: int
    site_id: int


def pick_batch(
    queued: Sequence[QueuedItem],
    *,
    total_budget: int,
    per_site_budget: int,
    max_concurrency: int,
    used_total: int,
    used_per_site: Mapping[int, int],
) -> list[int]:
    """Candidate ids in `queued` order (best first) within the remaining budgets."""
    remaining = min(max(total_budget - used_total, 0), max(max_concurrency, 0))
    per_site = dict(used_per_site)
    picked: list[int] = []
    for item in queued:
        if len(picked) >= remaining:
            break
        if per_site.get(item.site_id, 0) >= per_site_budget:
            continue
        per_site[item.site_id] = per_site.get(item.site_id, 0) + 1
        picked.append(item.candidate_id)
    return picked
