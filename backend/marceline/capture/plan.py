"""Pure capture policy (ADR 0016): where to sample a video, how many frames, and the hard caps.

No I/O, no clock, no ORM, no browser. The browser is told what to do from here, so the sampling
rule the ADR documents is the rule that actually runs — and it is testable without a browser.

Cost control is a property of `CapturePlan`: every cap a capture runs under is one of its fields,
and the worker has no way to sample more than the plan allows.
"""

import math
from dataclasses import dataclass

# Sampling policy (docs/research/headless-video-fingerprinting.md): about one frame per minute,
# spread evenly, skipping the first and last EDGE_SKIP_S seconds, where intros, studio logos and
# black frames live. The research table puts ~10 independent samples at ~85 % for two evidence
# frames at a per-frame hit rate of 0.3; samples from one scene are correlated, so aim higher.
EDGE_SKIP_S = 15.0
FRAMES_PER_MINUTE = 1.0
MIN_FRAMES = 20
# Two samples closer together than this are the same instant: a seek that costs bandwidth and
# gains no evidence. It is also what stops a short clip from being sampled dozens of times.
MIN_SPACING_S = 0.5
# Fraction cut from each end when a video is too short to skip EDGE_SKIP_S at both ends.
SHORT_VIDEO_INSET = 0.05
# Consecutive frames within this many PDQ bits of the previous kept frame are the same shot; the
# preview-clip path uses the same number (`matching.media_worker.PREVIEW_DUPLICATE_DISTANCE`).
DUPLICATE_DISTANCE = 8

# Defaults for the caps; `marceline.settings` overrides them from BROWSER_CAPTURE_* env vars.
DEFAULT_MAX_FRAMES = 24
DEFAULT_MAX_SECONDS = 120.0
DEFAULT_MAX_BYTES = 200 * 1024 * 1024
DEFAULT_TIMEOUT_S = 300.0
POSITION_DIGITS = 3


class CapturePlanError(ValueError):
    """The plan, or the duration it is applied to, is unusable."""


@dataclass(frozen=True)
class CapturePlan:
    """Every cap one capture runs under.

    `max_seconds` is the browser's own budget for the seek-and-sample loop; `timeout_s` is the
    watchdog for the whole visit including navigation. `max_bytes` aborts further requests once
    the page has pulled that much, which is the cap the research doc's success criteria name.
    """

    max_frames: int = DEFAULT_MAX_FRAMES
    max_seconds: float = DEFAULT_MAX_SECONDS
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout_s: float = DEFAULT_TIMEOUT_S
    edge_skip_s: float = EDGE_SKIP_S
    frames_per_minute: float = FRAMES_PER_MINUTE
    min_frames: int = MIN_FRAMES

    def __post_init__(self) -> None:
        if self.max_frames < 1:
            raise CapturePlanError("max_frames must be positive")
        if self.max_seconds <= 0 or self.timeout_s <= 0:
            raise CapturePlanError("max_seconds and timeout_s must be positive")
        if self.max_bytes < 1:
            raise CapturePlanError("max_bytes must be positive")
        if self.edge_skip_s < 0:
            raise CapturePlanError("edge_skip_s must not be negative")
        if self.frames_per_minute <= 0 or self.min_frames < 1:
            raise CapturePlanError("frames_per_minute and min_frames must be positive")

    def as_json(self) -> dict[str, float | int]:
        """What the browser script is told. Keys are the script's own argument names."""
        return {
            "maxFrames": self.max_frames,
            "maxSeconds": self.max_seconds,
            "maxBytes": self.max_bytes,
            "timeoutMs": int(self.timeout_s * 1000),
        }


def sample_window(duration_s: float, plan: CapturePlan) -> tuple[float, float]:
    """(start, end) of the part of the video worth sampling, in seconds.

    Normally the video without its first and last `edge_skip_s`. A video too short for that is
    inset by a fraction of its own length instead, so a short clip still skips its very edges.
    """
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise CapturePlanError(f"unusable video duration: {duration_s!r}")
    start, end = plan.edge_skip_s, duration_s - plan.edge_skip_s
    if end - start >= MIN_SPACING_S:
        return start, end
    inset = duration_s * SHORT_VIDEO_INSET
    return inset, duration_s - inset


def frame_count(duration_s: float, plan: CapturePlan) -> int:
    """How many frames to sample: one per minute, never below `min_frames`, never above
    `max_frames`, and never so many that two samples fall within MIN_SPACING_S of each other."""
    start, end = sample_window(duration_s, plan)
    wanted = max(round(duration_s / 60.0 * plan.frames_per_minute), plan.min_frames)
    fits = int((end - start) // MIN_SPACING_S)
    return max(1, min(wanted, plan.max_frames, max(fits, 1)))


def sample_positions(duration_s: float, plan: CapturePlan) -> list[float]:
    """Evenly spread sample positions in seconds, ascending.

    Positions sit at the centre of equal slices, so no sample lands exactly on the window's edge
    and the spacing between them is uniform — which is what `matching.timeline` scores as a
    consistent clip timeline.
    """
    start, end = sample_window(duration_s, plan)
    count = frame_count(duration_s, plan)
    span = end - start
    return [round(start + (index + 0.5) * span / count, POSITION_DIGITS) for index in range(count)]
