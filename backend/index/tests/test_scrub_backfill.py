"""The scrub at deactivation, made durable and caught up (issue #148).

`scrub_deactivated_pages` only ever ran for the pages one `apply_activity` deactivated, so every
page deactivated before it shipped kept the title, tags and performer credits that
`docs/legal/verzeichnis-verarbeitungstaetigkeiten.md` (V06) and `docs/legal/crawl-position.md` § 2
both state are deleted at deactivation. These tests cover the catch-up pass, the invariant `make
status` prints, and the one thing the backfill may never do: rewrite a safety-flagged page's text.
Invented titles and names only.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog.models import Creator, Site
from index.models import Page
from index.retention import (
    backfill_scrub,
    scrub_candidates,
    scrub_deactivated_pages,
    scrub_gap,
)

pytestmark = pytest.mark.django_db

UNTARGETED = Page.InactiveReason.UNTARGETED
CREDITS = {
    "performers": ["Nora Vale"],
    "dropped_performers": [{"name": "Dana Quill", "reason": "noise_word"}],
    "duration_s": 42,
    "external_links": ["https://mirror.test/x"],
}


def unscrubbed_page(
    site: Site,
    slug: str,
    *,
    days_ago: int = 3,
    active: bool = False,
    safety_status: str = Page.SafetyStatus.CLEAR,
) -> Page:
    """A page that holds all three scrubbable fields, deactivated `days_ago` unless `active`."""
    page = Page.objects.create(
        site=site,
        url=f"https://{site.domain}/{slug}",
        title=f"a video {slug}",
        tags=["outdoor", slug],
        raw_extraction=dict(CREDITS),
        safety_status=safety_status,
        active=active,
        inactive_reason="" if active else UNTARGETED,
    )
    if not active:
        moment = timezone.now() - timedelta(days=days_ago)
        Page.objects.filter(pk=page.pk).update(active_changed_at=moment, deactivated_at=moment)
    return Page.objects.get(pk=page.pk)


def holds_text(page: Page) -> bool:
    fresh = Page.objects.get(pk=page.pk)
    return bool(fresh.title) or bool(fresh.tags) or "performers" in fresh.raw_extraction


@pytest.mark.parametrize("status", [Page.SafetyStatus.REVIEW, Page.SafetyStatus.BLOCKED])
def test_the_scrub_never_rewrites_a_safety_flagged_pages_stored_text(
    site: Site, status: str
) -> None:
    """Root CLAUDE.md and docs/suppression.md: on a flagged page the text *is* the safety record.

    `marceline.targets.keep_rules.safety` keeps such a page active, so the deactivation path never
    hands one in — but the primitive refuses it by itself, for every caller including the backfill.
    """
    page = unscrubbed_page(site, "flagged", safety_status=status)

    assert scrub_deactivated_pages([page.pk]) == 0
    fresh = Page.objects.get(pk=page.pk)
    assert (fresh.title, fresh.tags) == (page.title, page.tags)
    assert fresh.raw_extraction == CREDITS


def test_candidates_are_only_inactive_untargeted_clear_pages_that_still_hold_text(
    site: Site,
) -> None:
    wanted = unscrubbed_page(site, "stock")
    unscrubbed_page(site, "active-with-text", active=True)
    unscrubbed_page(site, "flagged", safety_status=Page.SafetyStatus.BLOCKED)
    already = unscrubbed_page(site, "already-scrubbed")
    scrub_deactivated_pages([already.pk])

    assert list(scrub_candidates().values_list("pk", flat=True)) == [wanted.pk]


def test_a_dry_run_counts_per_day_and_per_reason_and_writes_nothing(site: Site) -> None:
    today = unscrubbed_page(site, "today", days_ago=0)
    older = unscrubbed_page(site, "older", days_ago=2)
    flagged = unscrubbed_page(site, "flagged", safety_status=Page.SafetyStatus.REVIEW)

    report = backfill_scrub()

    assert (report.dry_run, report.candidates, report.scrubbed) == (True, 2, 0)
    assert report.remaining == 2
    assert report.safety_flagged == 1
    assert report.by_reason == {UNTARGETED: 3}  # the flagged one is counted, not scrubbed
    # Read the days back from the rows rather than recomputing them, so a run at midnight or in
    # another year does not turn this into a time bomb (TIME_ZONE is UTC, like `TruncDate`).
    expected: dict[str, int] = {}
    for page in (today, older):
        day = Page.objects.get(pk=page.pk).deactivated_at
        assert day is not None
        expected[day.date().isoformat()] = expected.get(day.date().isoformat(), 0) + 1
    assert report.by_day == expected
    assert holds_text(today) and holds_text(older) and holds_text(flagged)


def test_apply_scrubs_the_three_fields_and_keeps_what_a_reactivation_needs(site: Site) -> None:
    creator = Creator.objects.create(name="Nora Vale")
    page = unscrubbed_page(site, "stock")
    page.creators.add(creator)

    report = backfill_scrub(dry_run=False)

    assert (report.scrubbed, report.remaining) == (1, 0)
    fresh = Page.objects.get(pk=page.pk)
    assert (fresh.title, fresh.tags) == ("", [])
    assert fresh.raw_extraction == {"duration_s": 42, "external_links": ["https://mirror.test/x"]}
    # Kept, exactly as at deactivation: the URL, the clock and the creator link the `linked`
    # reactivation rule reads (ADR 0029 amendment).
    assert (fresh.url, fresh.active, fresh.inactive_reason) == (page.url, False, UNTARGETED)
    assert fresh.deactivated_at == page.deactivated_at
    assert list(fresh.creators.all()) == [creator]


def test_a_limited_run_reports_the_rest_and_a_second_run_finishes_it(site: Site) -> None:
    pages = [unscrubbed_page(site, f"stock-{n}") for n in range(5)]

    first = backfill_scrub(dry_run=False, batch_size=2, limit=3)
    assert (first.candidates, first.scrubbed, first.remaining) == (5, 3, 2)

    second = backfill_scrub(dry_run=False, batch_size=2)
    assert (second.candidates, second.scrubbed, second.remaining) == (2, 2, 0)
    assert not any(holds_text(page) for page in pages)

    # Idempotent: a third run finds nothing left to do.
    assert backfill_scrub(dry_run=False).scrubbed == 0


def test_apply_never_touches_an_active_page_or_a_flagged_one(site: Site) -> None:
    active = unscrubbed_page(site, "active", active=True)
    flagged = unscrubbed_page(site, "flagged", safety_status=Page.SafetyStatus.BLOCKED)

    report = backfill_scrub(dry_run=False)

    assert (report.candidates, report.scrubbed) == (0, 0)
    assert holds_text(active) and holds_text(flagged)


def test_the_invariant_is_zero_once_the_backfill_has_run(site: Site) -> None:
    unscrubbed_page(site, "stock")
    unscrubbed_page(site, "flagged", safety_status=Page.SafetyStatus.REVIEW)

    before = scrub_gap()
    assert (before.inactive, before.holding_text, before.safety_flagged) == (2, 1, 1)
    assert not before.clean

    backfill_scrub(dry_run=False)

    after = scrub_gap()
    assert (after.inactive, after.holding_text, after.safety_flagged) == (2, 0, 1)
    assert after.clean


def test_the_command_defaults_to_a_dry_run(site: Site) -> None:
    page = unscrubbed_page(site, "stock")
    out = StringIO()

    call_command("scrub_inactive_pages", stdout=out)

    printed = out.getvalue()
    assert "dry run, nothing written" in printed
    assert "candidates: 1 page(s)" in printed
    assert "by deactivation day:" in printed
    assert holds_text(page)


def test_the_command_applies_and_reports_the_flagged_ones_it_left(site: Site) -> None:
    page = unscrubbed_page(site, "stock")
    unscrubbed_page(site, "flagged", safety_status=Page.SafetyStatus.BLOCKED)
    out = StringIO()

    call_command("scrub_inactive_pages", apply=True, stdout=out)

    printed = out.getvalue()
    assert "scrubbed: 1 page(s)" in printed
    assert "safety-flagged: 1 page(s)" in printed
    assert "left after this run: 0 page(s)" in printed
    assert not holds_text(page)
