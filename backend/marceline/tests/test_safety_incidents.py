"""Mirror/cluster deduplication and the backfill of hand-written report notes (issue #66)."""

from collections.abc import Callable
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog.models import Site
from index.models import Media, Page, SafetyIncident, SafetyIncidentPage
from marceline import safety_incidents
from marceline.management.commands.backfill_safety_incidents import (
    MARKER,
    REPORT_CATEGORY,
    REPORTED_AT,
    REPORTED_VIA,
)
from matching.models import Cluster, ClusterMember

Evidence = SafetyIncidentPage.Evidence
TERM = {"category": "minor", "term": "malolet", "action": "block"}
MIRROR_PATH = "imgs/aa/bb/0123456789abcde"
NOTE = (
    "2026-01-15 gemeldet ueber das Webformular der Beschwerdestelle. "
    "Keine Referenznummer vergeben. Antwort moeglich an "
    "reporting@marceline.studio. Inhalt nie geoeffnet, keine Medien geladen."
)


@pytest.fixture
def other_site(db: None) -> Site:
    return Site.objects.create(domain="mirror.test", name="Mirror")


@pytest.fixture
def make_blocked(site: Site) -> Callable[..., Page]:
    def _make(slug: str, *, page_site: Site | None = None, note: str = "") -> Page:
        page_site = page_site or site
        return Page.objects.create(
            site=page_site,
            url=f"https://{page_site.domain}/{slug}",
            title=f"title of {slug}",
            safety_status=Page.SafetyStatus.BLOCKED,
            safety_matches=[TERM],
            safety_flagged_at=timezone.now(),
            safety_note=note,
        )

    return _make


def test_a_mirror_poster_key_folds_both_urls_into_one_case(
    make_blocked: Callable[..., Page], other_site: Site
) -> None:
    """Deduplication case (b): the same content on another site, recognised without any fetch."""
    first = make_blocked("clip")
    mirror = make_blocked("clip", page_site=other_site)
    Media.objects.create(
        page=first, kind=Media.Kind.POSTER, url=f"https://cdn.one.test/{MIRROR_PATH}_main.jpg"
    )
    Media.objects.create(
        page=mirror, kind=Media.Kind.POSTER, url=f"https://img.two.test/x/{MIRROR_PATH}_1.jpg"
    )

    report = safety_incidents.sync_incidents()

    assert (report.scanned, report.created) == (2, 1)
    assert report.linked[Evidence.MIRROR] == 1
    incident = SafetyIncident.objects.get()
    assert sorted(incident.incident_pages.values_list("url", flat=True)) == sorted(
        [first.url, mirror.url]
    )


def test_cluster_membership_folds_both_pages_into_one_case(
    make_blocked: Callable[..., Page], other_site: Site
) -> None:
    first = make_blocked("video")
    mirror = make_blocked("video", page_site=other_site)
    cluster = Cluster.objects.create()
    for page in (first, mirror):
        ClusterMember.objects.create(
            cluster=cluster,
            page=page,
            status=ClusterMember.Status.CONFIRMED,
            evidence=ClusterMember.Evidence.PDQ,
        )

    report = safety_incidents.sync_incidents()

    assert (report.created, report.linked[Evidence.CLUSTER]) == (1, 1)
    incident = SafetyIncident.objects.get()
    assert incident.cluster_ref == cluster.pk
    assert incident.incident_pages.count() == 2


def test_sync_is_idempotent_and_its_dry_run_writes_nothing(
    make_blocked: Callable[..., Page],
) -> None:
    make_blocked("alone")

    planned = safety_incidents.sync_incidents(dry_run=True)
    assert (planned.created, SafetyIncident.objects.count()) == (1, 0)

    safety_incidents.sync_incidents()
    again = safety_incidents.sync_incidents()

    assert (again.created, again.linked[Evidence.SAME_URL]) == (0, 1)
    assert SafetyIncident.objects.count() == 1


def test_backfill_preserves_every_fact_of_the_recorded_report(
    make_blocked: Callable[..., Page],
) -> None:
    page = make_blocked("reported-page", note=NOTE)
    out = StringIO()

    call_command("backfill_safety_incidents", "--dry-run", stdout=out)
    assert SafetyIncident.objects.count() == 0

    call_command("backfill_safety_incidents", stdout=out)

    incident = SafetyIncident.objects.get()
    assert incident.status == SafetyIncident.Status.REPORTED
    assert incident.reported_at == REPORTED_AT
    assert incident.reported_via == REPORTED_VIA
    assert incident.report_category == REPORT_CATEGORY
    assert incident.reference == ""  # no reference number was issued
    assert incident.decided_by == "operator"
    assert "reporting@marceline.studio" in incident.note
    assert "No reference number was issued" in incident.note
    assert "never opened" in incident.note
    assert NOTE in incident.note  # the operator's own words, verbatim
    assert incident.incident_pages.get().url == page.url


def test_backfill_is_idempotent(make_blocked: Callable[..., Page]) -> None:
    make_blocked("reported-page", note=NOTE)
    out = StringIO()
    call_command("backfill_safety_incidents", stdout=out)
    note = SafetyIncident.objects.get().note

    call_command("backfill_safety_incidents", stdout=out)

    assert SafetyIncident.objects.count() == 1
    assert SafetyIncident.objects.get().note == note
    assert "already correct" in out.getvalue()


def test_backfill_ignores_a_review_reason_that_is_not_a_report(
    make_blocked: Callable[..., Page],
) -> None:
    make_blocked("just-blocked", note="Blocked: title matches the age pattern.")
    out = StringIO()

    call_command("backfill_safety_incidents", stdout=out)

    assert SafetyIncident.objects.count() == 0


def test_backfill_never_overwrites_a_human_edited_note(
    make_blocked: Callable[..., Page],
) -> None:
    make_blocked("reported-page", note=NOTE)
    out = StringIO()
    call_command("backfill_safety_incidents", stdout=out)
    incident = SafetyIncident.objects.get()
    incident.note = "Hand-written follow-up from the hotline."
    incident.save(update_fields=("note",))

    call_command("backfill_safety_incidents", stdout=out)

    assert SafetyIncident.objects.get().note == "Hand-written follow-up from the hotline."
    assert MARKER not in SafetyIncident.objects.get().note
    assert "human-edited note" in out.getvalue()


def test_retention_command_prints_its_plan(make_blocked: Callable[..., Page]) -> None:
    make_blocked("alone")
    safety_incidents.sync_incidents()
    out = StringIO()

    call_command("safety_retention", "--dry-run", stdout=out)

    printed = out.getvalue()
    assert "dry run, nothing written" in printed
    assert "incidents: 1 total (open 1" in printed
    assert "to close: 0" in printed
    assert "to delete: 0" in printed
