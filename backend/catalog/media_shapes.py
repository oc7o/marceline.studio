"""The dead media URL shape breaker (ADR 0041, issue #82).

A site's theme can advertise a media URL it never generated — site17.example's KVS `preview.jpg`,
promised in `og:image` and JSON-LD for the whole catalogue and written only for the newest slice.
We then pay one request per page to re-learn the same 404, with a 246 kB HTML error page attached:
1,771 requests and 436 MB in a day, 28 % of that site's media budget. This module is the general
form: per (site, URL *shape*), stop fetching a shape that keeps answering "not here", and probe it
once per backoff window so a site that repairs its theme is never locked out.

The two entry points are used by `matching.media_worker`, the one place media is requested:

- `admit(site_id, url)` — `None` to go ahead, or the time to postpone this media row to.
- `record(site_id, url, outcome)` — after a response, with `GONE` or `OK` and nothing else.

**What may move the breaker, and what may never.** Only a settled "this is not here" counts:
HTTP 404 or 410 on a URL that is not a tokenized expiring link (`GONE`), and any 2xx (`OK`). A 5xx,
a 429, a timeout, a 403 (an authorization answer — hotlink protection, a WAF, a geo-block — not an
absence), an expired CDN token, and everything refused before the request was sent (the safety gate
of ADR 0018, robots.txt, a budget, a busy host slot) leave the state untouched. Suppressing on any
of those would silently stop us fingerprinting a working site, which is worse than the waste it
saves. The caller decides which outcome it saw; this module never guesses from a status code it
was not given.

**Suppression postpones, never discards.** A suppressed media row goes back to PENDING with
`next_attempt_at`, its attempt refunded — the same thing a used-up budget already does. Nothing has
to be re-queued by hand when a shape is rehabilitated.

**Off by default** (`MEDIA_URL_BREAKER_ENABLED`). The counters are kept either way, so
`request_report` shows what the breaker *would* suppress before it is given the budget.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from catalog.models import MediaUrlShape, Site
from core.url_shapes import url_shape

__all__ = [
    "GONE",
    "OK",
    "admit",
    "record",
    "shape_lines",
    "shape_of",
    "suppression_reason",
    "trips_now",
]

GONE = "gone"
OK = "ok"

# What a postponed media row records as its reason. `matching.media_worker` puts the shape after it.
SHAPE_SUPPRESSED = "media URL shape suppressed"


def shape_of(url: str) -> str:
    """The (host, path-pattern) shape this URL is counted against, truncated to the column."""
    return url_shape(url)[:500]


def _enabled() -> bool:
    return bool(settings.MEDIA_URL_BREAKER_ENABLED)


def _backoff(step: int) -> timedelta:
    """6 h, 12 h, 24 h, 48 h, 72 h … capped. One probe per window, so days are enough."""
    base = float(settings.MEDIA_SHAPE_BACKOFF_H)
    hours = min(base * (2 ** max(step - 1, 0)), float(settings.MEDIA_SHAPE_MAX_BACKOFF_H))
    return timedelta(hours=hours)


def _probe_timeout() -> timedelta:
    return timedelta(minutes=float(settings.MEDIA_SHAPE_PROBE_TIMEOUT_MIN))


def trips_now(row: MediaUrlShape, now: datetime) -> bool:
    """Whether this shape's counters meet the rule — both clauses, always both.

    The streak alone cannot be made safe: a healthy shape in the live index (2 % failure over
    10,301 fetches) still produced a run of 51 consecutive 404s, because the queue is ordered by
    page and not by chance. The share over the decayed window is what tells the two apart.
    """
    if row.gone_streak < int(settings.MEDIA_SHAPE_GONE_STREAK):
        return False
    if _stale_streak(row, now):
        return False
    settled = row.gone_recent + row.ok_recent
    if settled < int(settings.MEDIA_SHAPE_GONE_STREAK):
        return False
    return row.gone_recent / settled >= float(settings.MEDIA_SHAPE_GONE_SHARE)


def _stale_streak(row: MediaUrlShape, now: datetime) -> bool:
    """A streak whose last 404 is older than the TTL says nothing about the shape today."""
    ttl = timedelta(hours=float(settings.MEDIA_SHAPE_STREAK_TTL_H))
    return row.last_gone_at is None or now - row.last_gone_at > ttl


def suppression_reason(shape: str, until: datetime) -> str:
    return f"{SHAPE_SUPPRESSED} ({shape}) until {until:%Y-%m-%d %H:%M} UTC"


def admit(site_id: int, url: str, *, now: datetime | None = None) -> datetime | None:
    """None when this media may be fetched, else when to try it again.

    Closed: go ahead. Open: postponed to the end of the window. Half-open (the window has passed):
    the first caller becomes the probe and goes ahead; the others are postponed by the probe
    timeout, so a whole backlog cannot pour back onto a site that has not answered yet.
    """
    if not _enabled():
        return None
    now = now or timezone.now()
    shape = shape_of(url)
    with transaction.atomic():
        row = (
            MediaUrlShape.objects.select_for_update()
            .filter(site_id=site_id, shape=shape)
            .exclude(suppressed_until=None)
            .first()
        )
        if row is None:
            return None
        assert row.suppressed_until is not None
        if now < row.suppressed_until:
            return row.suppressed_until
        if row.probe_started_at is not None and now - row.probe_started_at < _probe_timeout():
            return row.probe_started_at + _probe_timeout()
        row.probe_started_at = now
        row.save(update_fields=("probe_started_at", "updated_at"))
        return None


def record(site_id: int, url: str, outcome: str, *, now: datetime | None = None) -> None:
    """Fold one settled answer into the shape's counters, and open or close the breaker.

    `outcome` is `GONE` (404/410, not an expiring link) or `OK` (any 2xx) — never anything else.

    Successes are written down as faithfully as failures, from the shape's first answer on. They
    are the expensive half of the table — the live index has 104,465 distinct shapes against 374
    that ever failed — and they are kept anyway, because the share clause is only a protection if
    a shape that worked for months before it broke can prove it.
    """
    if outcome not in (GONE, OK):
        raise ValueError(f"a media URL shape is only moved by {GONE} or {OK}, not {outcome!r}")
    now = now or timezone.now()
    shape = shape_of(url)
    with transaction.atomic():
        row = (
            MediaUrlShape.objects.select_for_update().filter(site_id=site_id, shape=shape).first()
        ) or MediaUrlShape(site_id=site_id, shape=shape)
        if outcome == OK:
            _closed(row, now)
        else:
            _gone(row, now, probing=row.probe_started_at is not None)
        row.save()


def _closed(row: MediaUrlShape, now: datetime) -> None:
    """A 2xx: the shape serves something. One success is enough to rehabilitate it.

    Deliberately asymmetric with the trip rule. Re-learning a 404 costs one request; staying blind
    to a shape the site has repaired costs every page behind it.
    """
    row.ok_total += 1
    row.ok_recent += 1
    row.gone_streak = 0
    row.last_ok_at = now
    row.suppressed_until = None
    row.suppressed_at = None
    row.probe_started_at = None
    row.backoff_step = 0
    _decay(row)


def _gone(row: MediaUrlShape, now: datetime, *, probing: bool) -> None:
    if _stale_streak(row, now):
        row.gone_streak = 0
    row.gone_total += 1
    row.gone_recent += 1
    row.gone_streak += 1
    row.last_gone_at = now
    _decay(row)
    if probing:
        # The half-open probe answered 404 as well: open again, one step further back.
        _open(row, now)
    elif row.suppressed_until is None and trips_now(row, now) and _enabled():
        _open(row, now)


def _open(row: MediaUrlShape, now: datetime) -> None:
    row.backoff_step = min(row.backoff_step + 1, 16)
    row.suppressed_until = now + _backoff(row.backoff_step)
    row.suppressed_at = now
    row.probe_started_at = None
    row.trips += 1


def _decay(row: MediaUrlShape) -> None:
    """Halve both recent counters once they fill the window: a rolling view in two integers."""
    window = int(settings.MEDIA_SHAPE_WINDOW)
    if row.gone_recent + row.ok_recent > window:
        row.gone_recent //= 2
        row.ok_recent //= 2


@dataclass(frozen=True)
class ShapeReport:
    """One site's shapes worth an operator's attention."""

    suppressed: list[MediaUrlShape]
    would_suppress: list[MediaUrlShape]
    wasted: int


def shape_report(site: Site, now: datetime) -> ShapeReport:
    """What the breaker is doing (or would do) on this site, for `request_report` and the admin."""
    rows = list(MediaUrlShape.objects.filter(site=site, gone_total__gt=0))
    suppressed = sorted(
        (row for row in rows if row.suppressed_until is not None), key=lambda row: row.shape
    )
    pending = sorted(
        (row for row in rows if row.suppressed_until is None and trips_now(row, now)),
        key=lambda row: row.shape,
    )
    return ShapeReport(suppressed, pending, sum(row.gone_total for row in rows))


def _shape_line(row: MediaUrlShape, now: datetime, *, state: str) -> str:
    settled = row.gone_recent + row.ok_recent
    share = row.gone_recent / settled if settled else 0.0
    return (
        f"    {row.shape}: {state}; {row.gone_total:,} gone / {row.ok_total:,} ok, "
        f"recent {share:.0%} gone of {settled}, streak {row.gone_streak}, {row.trips} trip(s)"
    )


def shape_lines(site: Site, now: datetime) -> list[str]:
    """Plain-text lines for one site: shapes only, never a URL (ADR 0041 § 6)."""
    report = shape_report(site, now)
    if not report.wasted:
        return []
    off = "" if settings.MEDIA_URL_BREAKER_ENABLED else ", breaker off"
    lines = [
        f"  media URL shapes: {report.wasted:,} gone answer(s); "
        f"{len(report.suppressed)} suppressed, {len(report.would_suppress)} would be{off}"
    ]
    for row in report.suppressed:
        assert row.suppressed_until is not None
        state = (
            f"suppressed until {row.suppressed_until:%Y-%m-%d %H:%M} UTC"
            if now < row.suppressed_until
            else "suppressed, probing"
        )
        lines.append(_shape_line(row, now, state=state))
    for row in report.would_suppress:
        lines.append(_shape_line(row, now, state="would be suppressed"))
    return lines
