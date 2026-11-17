"""Retention for deactivated pages (ADR 0029 amendment 2026-09-17, issue #71)."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog.models import Creator, Site
from index.incidents import months_ago
from index.management.commands.page_retention import Command as PageRetentionCommand
from index.models import Page, SafetyIncident, SafetyIncidentPage
from index.retention import PAGE_RETENTION_MONTHS, apply_retention, scrub_deactivated_pages

pytestmark = pytest.mark.django_db

UNTARGETED = Page.InactiveReason.UNTARGETED


def deactivated_page(site: Site, slug: str, deactivated_at: object, **fields: object) -> Page:
    page = Page.objects.create(
        site=site,
        url=f"https://{site.domain}/{slug}",
        title=slug,
        active=False,
        inactive_reason=UNTARGETED,
        **fields,
    )
    Page.objects.filter(pk=page.pk).update(
        active_changed_at=deactivated_at, deactivated_at=deactivated_at
    )
    return Page.objects.get(pk=page.pk)


def attach_incident(page: Page, status: str) -> SafetyIncident:
    """A page linked to a case (e.g. a staff reversal left the page clear while the case, opened
    for someone else's evidence too, stays open) -- exactly the gap `keep_rules.safety` (which
    only ever looks at the page's *own* current `safety_status`) does not cover."""
    now = timezone.now()
    reported_at = now if status == SafetyIncident.Status.REPORTED else None
    closed_at = (
        now
        if status in (SafetyIncident.Status.CLOSED, SafetyIncident.Status.FALSE_POSITIVE)
        else None
    )
    incident = SafetyIncident.objects.create(
        status=status,
        detected_at=now,
        last_seen_at=now,
        reported_at=reported_at,
        closed_at=closed_at,
    )
    SafetyIncidentPage.objects.create(
        incident=incident,
        page=page,
        url=page.url,
        evidence=SafetyIncidentPage.Evidence.ORIGIN,
        first_seen_at=now,
        last_seen_at=now,
    )
    return incident


def test_scrub_drops_people_but_keeps_reactivation_data(site: Site) -> None:
    creator = Creator.objects.create(name="Someone")
    page = Page.objects.create(
        site=site,
        url=f"https://{site.domain}/one",
        title="a video",
        tags=["outdoor"],
        raw_extraction={
            "performers": ["Someone"],
            "dropped_performers": [{"name": "Extra", "reason": "noise_word"}],
            "duration_s": 42,
            "external_links": ["https://mirror.test/x"],
        },
    )
    page.creators.add(creator)

    result = scrub_deactivated_pages([page.pk])

    assert result == 1
    scrubbed = Page.objects.get(pk=page.pk)
    assert (scrubbed.title, scrubbed.tags) == ("", [])
    assert scrubbed.raw_extraction == {
        "duration_s": 42,
        "external_links": ["https://mirror.test/x"],
    }
    # Kept: everything a reactivation needs, including the creator link (`linked` rule).
    assert (scrubbed.url, scrubbed.site_id) == (page.url, site.pk)
    assert list(scrubbed.creators.all()) == [creator]


def test_scrub_is_idempotent_and_a_no_op_on_an_empty_list(site: Site) -> None:
    page = Page.objects.create(site=site, url=f"https://{site.domain}/one", title="a video")

    assert scrub_deactivated_pages([]) == 0
    first = scrub_deactivated_pages([page.pk])
    second = scrub_deactivated_pages([page.pk])
    assert (first, second) == (1, 1)  # unconditional UPDATE, still a no-op result
    assert Page.objects.get(pk=page.pk).title == ""


def test_boundary_one_day_inside_and_outside_the_retention_window(site: Site) -> None:
    now = timezone.now()
    threshold = months_ago(now, PAGE_RETENTION_MONTHS)
    just_inside = deactivated_page(site, "inside", threshold + timedelta(days=1))
    just_outside = deactivated_page(site, "outside", threshold - timedelta(days=1))

    report = apply_retention(now=now)

    assert dict(report.by_site) == {site.domain: 1}
    assert Page.objects.filter(pk=just_inside.pk).exists()
    assert not Page.objects.filter(pk=just_outside.pk).exists()


def test_dry_run_writes_nothing(site: Site) -> None:
    now = timezone.now()
    old = months_ago(now, PAGE_RETENTION_MONTHS) - timedelta(days=365)
    page = deactivated_page(site, "old", old)

    report = apply_retention(now=now, dry_run=True)

    assert report.dry_run is True
    assert dict(report.by_site) == {site.domain: 1}
    assert report.deleted_ids == []
    assert Page.objects.filter(pk=page.pk).exists()
    # A second dry run changes nothing either.
    again = apply_retention(now=now, dry_run=True)
    assert dict(again.by_site) == {site.domain: 1}


def test_active_pages_and_pages_with_no_clock_are_never_candidates(site: Site) -> None:
    now = timezone.now()
    very_old = now - timedelta(days=3650)
    # An active page (e.g. a target's) is never touched however old its timestamps are.
    active_page = Page.objects.create(
        site=site, url=f"https://{site.domain}/active", title="active"
    )
    Page.objects.filter(pk=active_page.pk).update(first_seen=very_old, last_seen=very_old)
    # A page deactivated before this feature shipped, never backfilled, has no clock and is
    # never a candidate either (it needs `deactivated_at` set first, e.g. by the migration
    # backfill or a fresh (de)activation).
    no_clock = Page.objects.create(
        site=site,
        url=f"https://{site.domain}/no-clock",
        title="x",
        active=False,
        inactive_reason=UNTARGETED,
    )
    Page.objects.filter(pk=no_clock.pk).update(active_changed_at=very_old)

    report = apply_retention(now=now)

    assert report.by_site == {}
    assert Page.objects.filter(pk__in=[active_page.pk, no_clock.pk]).count() == 2


def test_reactivated_page_never_expires_by_this_clock(site: Site) -> None:
    now = timezone.now()
    old = months_ago(now, PAGE_RETENTION_MONTHS) - timedelta(days=365)
    page = deactivated_page(site, "reactivated", old)
    Page.objects.filter(pk=page.pk).update(active=True, inactive_reason="", deactivated_at=None)

    report = apply_retention(now=now)

    assert report.by_site == {}
    assert Page.objects.filter(pk=page.pk).exists()


def test_deletion_cascades_its_creator_links(site: Site) -> None:
    """The creator link kept for reactivation (index.retention module docstring) goes with the
    page once it is actually deleted -- nothing needs to recompute the breadcrumbs, since a
    deactivated page never counted towards one in the first place (index.breadcrumbs)."""
    now = timezone.now()
    old = months_ago(now, PAGE_RETENTION_MONTHS) - timedelta(days=365)
    creator = Creator.objects.create(name="Legacy")
    page = deactivated_page(site, "legacy", old)
    page.creators.add(creator)

    apply_retention(now=now)

    assert not Page.objects.filter(pk=page.pk).exists()
    assert not Page.creators.through.objects.filter(creator_id=creator.pk).exists()


def test_command_defaults_to_dry_run_and_says_so(site: Site) -> None:
    now = timezone.now()
    old = months_ago(now, PAGE_RETENTION_MONTHS) - timedelta(days=365)
    page = deactivated_page(site, "old", old)
    out = StringIO()

    call_command("page_retention", stdout=out)

    assert "dry run" in out.getvalue()
    assert "to delete: 1" in out.getvalue()
    assert Page.objects.filter(pk=page.pk).exists()
    # The help text says the default is a dry run, per the task (deletion is irreversible).
    assert "dry run" in PageRetentionCommand.help and "irreversible" in PageRetentionCommand.help


@pytest.mark.parametrize("status", [SafetyIncident.Status.OPEN, SafetyIncident.Status.REPORTED])
def test_a_page_in_an_open_or_reported_safety_case_is_never_deleted(
    site: Site, status: str
) -> None:
    now = timezone.now()
    old = months_ago(now, PAGE_RETENTION_MONTHS) - timedelta(days=365)
    page = deactivated_page(site, "cased", old)
    attach_incident(page, status)

    dry = apply_retention(now=now, dry_run=True)
    assert dry.by_site == {}  # excluded from "would delete" too, not only from the real run
    assert dry.safety_protected_ids == [page.pk]

    report = apply_retention(now=now)

    assert report.by_site == {}
    assert report.deleted_ids == []
    assert report.safety_protected_ids == [page.pk]
    assert Page.objects.filter(pk=page.pk).exists()


@pytest.mark.parametrize(
    "status", [SafetyIncident.Status.CLOSED, SafetyIncident.Status.FALSE_POSITIVE]
)
def test_a_page_in_a_settled_safety_case_is_deleted_as_usual(site: Site, status: str) -> None:
    now = timezone.now()
    old = months_ago(now, PAGE_RETENTION_MONTHS) - timedelta(days=365)
    page = deactivated_page(site, "settled", old)
    attach_incident(page, status)

    report = apply_retention(now=now)

    assert report.safety_protected_ids == []
    assert report.deleted_ids == [page.pk]
    assert not Page.objects.filter(pk=page.pk).exists()
    # The incident itself is untouched: SafetyIncidentPage.page is SET_NULL by design, the URL
    # stays as the record (docs/safety.md, index.models.SafetyIncidentPage).
    link = SafetyIncidentPage.objects.get()
    assert (link.page_id, link.url) == (None, page.url)


def test_command_apply_deletes(site: Site) -> None:
    now = timezone.now()
    old = months_ago(now, PAGE_RETENTION_MONTHS) - timedelta(days=365)
    page = deactivated_page(site, "old", old)
    out = StringIO()

    call_command("page_retention", apply=True, stdout=out)

    assert "deleted: 1" in out.getvalue()
    assert not Page.objects.filter(pk=page.pk).exists()
