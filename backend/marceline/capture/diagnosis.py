"""Why a loaded page gave no usable video, in words the queue can act on (issue #78).

The first trial recorded 40 of 45 visits as a bare `no_video`. The causes behind it differ in what
they mean for the next visit, so the skip reason now says which one it was — derived from
text-only browser state (`Loaded.videos`, the request policy's refusals), never from pixels.

Pure: no I/O, no ORM, no browser. Reasons starting with `no_video` are the page's doing and count
as "scanned once" (ADR 0016); `unsupported_media` is our browser's doing (a codec the engine
lacks) and stays retryable, so another engine can try again.

**Naming an un-allowlisted embed host (issue #78 amendment 2026-09-18).** `embed_refusal()`
deliberately never reports `EMBED_NOT_ALLOWLISTED` — an un-allowlisted subframe is usually an ad,
and every higher-priority reason here (a media error, a robots-refused player request, an unclicked
play control) is a better explanation when one applies. But when *none* of them do, the previous
code fell all the way through to a bare, silent `no_video`, even though a subframe host really was
refused. That is now the last resort, after `click_to_play`: still lower priority than every real
explanation, but no longer silent about the one thing this visit does know.
"""

from marceline.capture.requests import EMBED_NOT_ALLOWLISTED, ROBOTS_DISALLOWED, RequestPolicy
from marceline.capture.runner import Loaded

# HTMLMediaElement.error codes.
MEDIA_ERR_ABORTED = 1
MEDIA_ERR_NETWORK = 2
MEDIA_ERR_DECODE = 3
MEDIA_ERR_SRC_NOT_SUPPORTED = 4

# An element screenshot of a player drawn smaller than this is not a frame (the trial's only
# "capture" hashed a 1264x15 px strip, because the page's stylesheets had been refused).
MIN_RENDERED_WIDTH = 160
MIN_RENDERED_HEIGHT = 90

NO_VIDEO = "no_video"
UNSUPPORTED_MEDIA = "unsupported_media"
# The one allowed play click was spent and the page still produced no player (ADR 0016 amendment
# 2026-09-18, issue #78). The page's doing, so it counts as scanned once — there is nothing left
# to try on a second visit, and a pre-roll ad is one of the ways this reads.
NO_PLAYER_AFTER_CLICK = f"{NO_VIDEO}: no player after the play click"
# Request types a player uses to load itself or its stream. Scripts and documents are left out on
# purpose: every page pulls scripts from ad and tracker hosts, and counting a refused one as "the
# player was refused" hid the real cause on 37 pages of the first re-run (issue #78).
PLAYER_REQUEST_TYPES = ("xhr", "fetch", "media")


def no_video_reason(loaded: Loaded, policy: RequestPolicy | None = None) -> str:
    """The most specific reason the page offered no playable, measurable video."""
    if policy is not None:
        embed = policy.embed_refusal()
        if embed is not None and not loaded.video_found:
            return f"{NO_VIDEO}: embedded player refused ({embed})"
    errors = [video.error_code for video in loaded.videos if video.error_code]
    if MEDIA_ERR_SRC_NOT_SUPPORTED in errors:
        return f"{UNSUPPORTED_MEDIA}: MEDIA_ERR_SRC_NOT_SUPPORTED"
    if errors:
        return f"{NO_VIDEO}: media error {errors[0]}"
    if (
        policy is not None
        and policy.robots_refused(PLAYER_REQUEST_TYPES)
        and not any(video.duration_s for video in loaded.videos)
    ):
        # The player or its stream sits behind a path robots.txt disallows for us.
        return f"{NO_VIDEO}: player requests refused ({ROBOTS_DISALLOWED})"
    if loaded.videos:
        return f"{NO_VIDEO}: unknown duration"
    if loaded.play_control:
        # The page draws a poster and a play button; the player only exists after a click.
        return f"{NO_VIDEO}: click_to_play"
    if policy is not None and policy.refused[(EMBED_NOT_ALLOWLISTED, "subframe")]:
        # Not confidently *the* player — an ad frame looks identical — but every more specific
        # explanation above has already been ruled out, so naming it beats a bare "no_video".
        return f"{NO_VIDEO}: embedded player refused ({EMBED_NOT_ALLOWLISTED})"
    return NO_VIDEO


def too_small(loaded: Loaded) -> bool:
    """Whether the chosen player is drawn too small for a screenshot to be a frame."""
    width, height = loaded.rendered
    return width < MIN_RENDERED_WIDTH or height < MIN_RENDERED_HEIGHT
