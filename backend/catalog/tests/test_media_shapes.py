"""The dead media URL shape breaker (ADR 0041, issue #82)."""

from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog import media_shapes
from catalog.models import MediaUrlShape, Site

pytestmark = pytest.mark.django_db

DEAD = "https://www.example.test/contents/videos_screenshots/29000/29060/preview.jpg"
DEAD_SHAPE = "www.example.test/contents/videos_screenshots/*/*/preview.jpg"
ALIVE = "https://www.example.test/contents/videos_screenshots/29000/29060/566x302/1.jpg"


def gone(site: Site, url: str = DEAD, times: int = 1, now: datetime | None = None) -> None:
    for _ in range(times):
        media_shapes.record(site.pk, url, media_shapes.GONE, now=now)


def ok(site: Site, url: str = DEAD, times: int = 1, now: datetime | None = None) -> None:
    for _ in range(times):
        media_shapes.record(site.pk, url, media_shapes.OK, now=now)


def row(site: Site, shape: str = DEAD_SHAPE) -> MediaUrlShape:
    found = MediaUrlShape.objects.get(site=site, shape=shape)
    return found


def test_a_url_is_counted_against_its_shape_not_itself(site: Site) -> None:
    for page_id in range(3):
        gone(
            site,
            f"https://www.example.test/contents/videos_screenshots/29000/{page_id}/preview.jpg",
        )

    assert MediaUrlShape.objects.count() == 1
    assert row(site).gone_total == 3


def test_a_shape_that_only_ever_worked_is_recorded_and_never_suppressed(
    site: Site, settings: Settings
) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    ok(site, ALIVE, times=30)

    stored = row(site, "www.example.test/contents/videos_screenshots/*/*/566x302/*.jpg")
    assert stored.ok_total == 30 and stored.gone_total == 0
    assert media_shapes.admit(site.pk, ALIVE) is None


def test_suppressed_after_the_streak_when_the_recent_share_agrees(
    site: Site, settings: Settings
) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    gone(site, times=19)

    assert media_shapes.admit(site.pk, DEAD) is None  # 19 is not yet 20

    gone(site)

    until = media_shapes.admit(site.pk, DEAD)
    assert until is not None and until > timezone.now()
    assert row(site).trips == 1


def test_a_healthy_shape_survives_a_long_run_of_404(site: Site, settings: Settings) -> None:
    """The measured case (ADR 0041 § 3): 2 % failure over 10,301 fetches, and still a run of 51.

    The streak clause alone would suppress it; the share over the recent window is what does not.
    """
    settings.MEDIA_URL_BREAKER_ENABLED = True
    ok(site, DEAD, times=200)
    gone(site, times=51)

    assert media_shapes.admit(site.pk, DEAD) is None
    assert row(site).gone_streak == 51
    assert row(site).suppressed_until is None


def test_one_success_resets_the_streak(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    gone(site, times=19)
    ok(site)
    gone(site, times=19)

    assert media_shapes.admit(site.pk, DEAD) is None
    assert row(site).gone_streak == 19


def test_a_stale_streak_starts_again(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    long_ago = timezone.now() - timedelta(days=30)
    gone(site, times=19, now=long_ago)

    gone(site, times=19)

    assert media_shapes.admit(site.pk, DEAD) is None
    assert row(site).gone_streak == 19


def test_off_by_default_it_counts_but_never_suppresses(site: Site) -> None:
    gone(site, times=50)

    assert media_shapes.admit(site.pk, DEAD) is None
    stored = row(site)
    assert stored.suppressed_until is None and stored.trips == 0
    assert media_shapes.trips_now(stored, timezone.now()) is True  # the report can say so


def test_an_open_breaker_postpones_every_row_of_that_shape(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    gone(site, times=20)
    other = "https://www.example.test/contents/videos_screenshots/1/2/preview.jpg"

    assert media_shapes.admit(site.pk, other) is not None
    # ... and leaves every other shape of the same site alone.
    assert media_shapes.admit(site.pk, ALIVE) is None


def test_suppression_is_per_site(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    other = Site.objects.create(domain="other.example", enabled=True)
    gone(site, times=20)

    assert media_shapes.admit(site.pk, DEAD) is not None
    assert media_shapes.admit(other.pk, DEAD) is None


def test_the_window_hands_out_exactly_one_probe(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    gone(site, times=20)
    later = timezone.now() + timedelta(days=1)

    assert media_shapes.admit(site.pk, DEAD, now=later) is None  # the probe goes ahead
    held = media_shapes.admit(site.pk, DEAD, now=later)
    assert held is not None and held > later  # everything behind it waits


def test_a_probe_that_succeeds_closes_the_breaker(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    gone(site, times=20)
    later = timezone.now() + timedelta(days=1)
    assert media_shapes.admit(site.pk, DEAD, now=later) is None

    ok(site, now=later)

    assert media_shapes.admit(site.pk, DEAD, now=later) is None
    stored = row(site)
    assert stored.suppressed_until is None and stored.gone_streak == 0
    assert stored.backoff_step == 0 and stored.probe_started_at is None


def test_a_probe_that_fails_backs_off_further(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    gone(site, times=20)
    first = row(site).suppressed_until
    assert first is not None

    later = timezone.now() + timedelta(days=1)
    assert media_shapes.admit(site.pk, DEAD, now=later) is None
    gone(site, now=later)

    stored = row(site)
    assert stored.backoff_step == 2 and stored.trips == 2
    assert stored.suppressed_until is not None
    assert stored.suppressed_until - later > first - timezone.now() - timedelta(minutes=1)


def test_the_backoff_is_capped(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    settings.MEDIA_SHAPE_MAX_BACKOFF_H = 72.0
    gone(site, times=20)
    now = timezone.now()
    for _ in range(10):
        now = (row(site).suppressed_until or now) + timedelta(seconds=1)
        assert media_shapes.admit(site.pk, DEAD, now=now) is None
        gone(site, now=now)

    stored = row(site)
    assert stored.suppressed_until is not None
    assert stored.suppressed_until - now == timedelta(hours=72)


def test_only_gone_and_ok_may_move_a_shape(site: Site) -> None:
    with pytest.raises(ValueError, match="only moved by"):
        media_shapes.record(site.pk, DEAD, "5xx")


def test_the_report_names_shapes_and_never_a_url(site: Site, settings: Settings) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    gone(site, times=20)

    lines = media_shapes.shape_lines(site, timezone.now())

    assert any(DEAD_SHAPE in line for line in lines)
    assert not any("29060" in line for line in lines)
    assert "1 suppressed" in lines[0]


def test_the_report_says_what_it_would_suppress_while_off(site: Site) -> None:
    gone(site, times=20)

    lines = media_shapes.shape_lines(site, timezone.now())

    assert "breaker off" in lines[0] and "1 would be" in lines[0]
    assert any("would be suppressed" in line for line in lines)


def test_the_command_prints_shapes_and_clears_one(site: Site) -> None:
    gone(site, times=20)
    out = StringIO()

    call_command("media_shapes", "--site", site.domain, stdout=out)
    assert DEAD_SHAPE in out.getvalue()

    call_command("media_shapes", "--site", site.domain, "--clear", DEAD_SHAPE, stdout=StringIO())
    assert not MediaUrlShape.objects.exists()


def test_the_command_refuses_a_clear_without_a_site() -> None:
    with pytest.raises(CommandError, match="needs --site"):
        call_command("media_shapes", "--clear", DEAD_SHAPE, stdout=StringIO())
