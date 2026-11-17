"""Staff safety incidents: the hotline report draft and "mark reported" (issue #66).

The draft is the one response that carries page URLs, because it *is* the report. It must never
carry anything else of the page (title, tags, text) or anything about media, and it must never
name the matched term — docs/safety.md: a report contains the URL, the site, when it was seen and
the matched category.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from catalog.models import Site
from index import incidents
from index.incident_report import REPORT_CHANNEL, TEMPLATE_VERSION
from index.models import Media, Page, SafetyIncident, SafetyIncidentPage
from marceline.management.commands.backfill_safety_incidents import REPORTED_VIA
from marceline.tests.app_helpers import add_member, client_for, make_organisation

Evidence = SafetyIncidentPage.Evidence
TERM = {"category": "minor", "term": "secretterm", "action": "block"}
TITLE = "page title that must stay internal"
TAG = "tag-that-must-stay-internal"
MEDIA_HOST = "cdn.media.example.test"
DETECTED = datetime(2026, 1, 15, 8, 30, tzinfo=UTC)
BASE = "/api/v1/review/safety/incidents"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


@pytest.fixture
def mirror_site(db: None) -> Site:
    return Site.objects.create(domain="mirror.example.test", name="Mirror")


@pytest.fixture
def make_blocked(site: Site) -> Callable[..., Page]:
    def _make(slug: str, *, page_site: Site | None = None) -> Page:
        page_site = page_site or site
        page = Page.objects.create(
            site=page_site,
            url=f"https://{page_site.domain}/video/{slug}",
            title=TITLE,
            tags=[TAG],
            safety_status=Page.SafetyStatus.BLOCKED,
            safety_matches=[TERM],
            safety_flagged_at=DETECTED,
        )
        Media.objects.create(
            page=page, kind=Media.Kind.POSTER, url=f"https://{MEDIA_HOST}/{slug}.jpg"
        )
        return page

    return _make


@pytest.fixture
def case(make_blocked: Callable[..., Page], mirror_site: Site) -> SafetyIncident:
    """One open case with its origin URL and a mirror URL on another site."""
    origin = make_blocked("clip")
    decision = incidents.open_or_link(origin, now=DETECTED)
    assert decision.incident is not None
    mirror = make_blocked("clip-copy", page_site=mirror_site)
    incidents.open_or_link(
        mirror,
        related=[incidents.Related(page_id=origin.pk, evidence=Evidence.MIRROR)],
        now=DETECTED + timedelta(hours=2),
    )
    return SafetyIncident.objects.get(pk=decision.incident.pk)


def draft(client: Client, incident: SafetyIncident) -> Any:
    return client.get(f"{BASE}/{incident.pk}/report-draft")


def body_of(payload: dict[str, Any], language: str) -> str:
    [text] = [item for item in payload["drafts"] if item["language"] == language]
    return f"{text['subject']}\n{text['body']}"


def test_draft_holds_urls_dates_category_and_reference_in_both_languages(
    staff_client: Client, case: SafetyIncident
) -> None:
    response = draft(staff_client, case)

    assert response.status_code == 200
    payload = response.json()
    assert payload["reference_id"] == f"MARCELINE-SI-{case.pk}"
    assert payload["template_version"] == TEMPLATE_VERSION
    assert payload["channel"] == REPORT_CHANNEL
    assert payload["supplement"] is False
    assert payload["media_fetch"] == "none"
    assert [(u["url"], u["evidence"], u["primary"]) for u in payload["urls"]] == [
        ("https://example.test/video/clip", "origin", True),
        ("https://mirror.example.test/video/clip-copy", "mirror", False),
    ]
    assert {item["language"] for item in payload["drafts"]} == {"de", "en"}

    german = body_of(payload, "de")
    assert "Maschinell erzeugter Verdacht" in german
    assert "nicht visuell verifiziert" in german
    assert "Mit dieser Meldung wird nicht behauptet" in german
    assert f"Unser Zeichen (interne Vorfall-ID):\nMARCELINE-SI-{case.pk}" in german
    assert "URL:\nhttps://example.test/video/clip\n(Website: example.test;" in german
    assert (
        "Weitere URLs mit demselben Inhalt:\nhttps://mirror.example.test/video/clip-copy" in german
    )
    assert "identischen Vorschaubild-Pfad" in german
    assert "Erstmalig festgestellt:\n2026-01-15 08:30 UTC" in german
    assert "Kategorie: minor" in german
    assert "Medienabruf für diesen Vorfall:\nKein Medienabruf erfolgt." in german
    assert "reporting@marceline.studio" in german
    assert "Ergänzung" not in german

    english = body_of(payload, "en")
    assert "Machine-generated suspicion" in english
    assert "not visually verified" in english
    assert "does not assert that the material is in fact unlawful" in english
    assert f"Our reference (internal incident ID):\nMARCELINE-SI-{case.pk}" in english
    assert "URL:\nhttps://example.test/video/clip\n(site: example.test;" in english
    assert (
        "Further URLs with the same content:\nhttps://mirror.example.test/video/clip-copy"
        in english
    )
    assert "First detected:\n2026-01-15 08:30 UTC" in english
    assert "category: minor" in english
    assert "Media-fetch status for this incident:\nNo media request was made." in english


def test_draft_carries_no_media_no_page_content_and_no_term(
    staff_client: Client, case: SafetyIncident
) -> None:
    response = draft(staff_client, case)

    raw = response.content.decode()
    for forbidden in (MEDIA_HOST, ".jpg", TITLE, TAG, TERM["term"], "media_id", "poster"):
        assert forbidden not in raw, forbidden
    payload = response.json()
    assert set(payload) == {
        "incident_id",
        "reference_id",
        "status",
        "template_version",
        "channel",
        "supplement",
        "reported_at",
        "reference",
        "media_fetch",
        "urls",
        "drafts",
    }
    assert set(payload["urls"][0]) == {
        "url",
        "site",
        "evidence",
        "primary",
        "first_seen_at",
        "last_seen_at",
        "new_since_report",
    }


def test_media_fetch_statement_is_truthful(staff_client: Client, case: SafetyIncident) -> None:
    link = case.incident_pages.get(evidence=Evidence.MIRROR)
    assert link.page is not None
    link.page.media.update(fetched_at=DETECTED - timedelta(days=1))

    payload = draft(staff_client, case).json()

    assert payload["media_fetch"] == "fetched"
    assert "Kein Medienabruf erfolgt" not in body_of(payload, "de")
    assert "No media request was made" not in body_of(payload, "en")
    assert "Before the flag was raised, media of at least one" in body_of(payload, "en")


def test_media_fetch_is_unknown_once_a_page_row_is_gone(
    staff_client: Client, case: SafetyIncident
) -> None:
    Page.objects.filter(url="https://mirror.example.test/video/clip-copy").delete()

    payload = draft(staff_client, case).json()

    assert payload["media_fetch"] == "unknown"
    assert "nicht mehr feststellbar" in body_of(payload, "de")
    assert [u["url"] for u in payload["urls"]] == [
        "https://example.test/video/clip",
        "https://mirror.example.test/video/clip-copy",
    ]


def test_reported_case_gets_a_supplement_marking_urls_added_since(
    staff_client: Client,
    case: SafetyIncident,
    make_blocked: Callable[..., Page],
) -> None:
    incidents.mark_reported(case, reference="REF-123", reported_at=DETECTED + timedelta(hours=3))
    origin = Page.objects.get(url="https://example.test/video/clip")
    late = make_blocked("clip-late")
    incidents.open_or_link(
        late,
        related=[incidents.Related(page_id=origin.pk, evidence=Evidence.SHA256)],
        now=DETECTED + timedelta(days=1),
    )
    late_link = case.incident_pages.get(url=late.url)
    late_link.first_seen_at = DETECTED + timedelta(days=1)
    late_link.save()

    payload = draft(staff_client, case).json()

    assert payload["supplement"] is True
    assert payload["reference"] == "REF-123"
    new = {u["url"]: u["new_since_report"] for u in payload["urls"]}
    assert new == {
        "https://example.test/video/clip": False,
        "https://mirror.example.test/video/clip-copy": False,
        late.url: True,
    }
    german = body_of(payload, "de")
    assert german.startswith("Ergänzung: ")
    assert "Ergänzung zu unserer Meldung vom 2026-01-15 (Ihr Zeichen: REF-123)" in german
    assert "neu seit unserer Meldung" in german
    english = body_of(payload, "en")
    assert "This supplements our report of 2026-01-15 (your reference: REF-123)." in english


@pytest.mark.parametrize("settle", [incidents.mark_closed, incidents.mark_false_positive])
def test_settled_cases_get_no_draft(
    staff_client: Client,
    case: SafetyIncident,
    settle: Callable[..., SafetyIncident],
) -> None:
    settle(case)

    response = draft(staff_client, case)

    assert response.status_code == 409
    assert response.json()["conflicts"][0]["status"] == case.status


def test_unknown_incident_is_404(staff_client: Client, db: None) -> None:
    assert staff_client.get(f"{BASE}/999999/report-draft").status_code == 404
    assert post_json(staff_client, f"{BASE}/999999/reported", {}).status_code == 404


def test_mark_reported_records_date_channel_reference_and_template(
    staff_client: Client, case: SafetyIncident
) -> None:
    when = DETECTED + timedelta(hours=5)

    response = post_json(
        staff_client,
        f"{BASE}/{case.pk}/reported",
        {
            "reference": " REF-2026-77 ",
            "report_category": "abuse material involving minors",
            "reported_at": when.isoformat(),
            "language": "de",
        },
    )

    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "reported"
    assert item["reference"] == "REF-2026-77"
    assert item["reported_via"] == REPORT_CHANNEL
    assert item["url_count"] == 2
    case.refresh_from_db()
    assert case.status == SafetyIncident.Status.REPORTED
    assert case.reported_at == when
    assert case.decided_by == "reviewer"
    assert f"{TEMPLATE_VERSION}, de" in case.note
    again = post_json(staff_client, f"{BASE}/{case.pk}/reported", {"reference": "other"})
    assert again.status_code == 409
    case.refresh_from_db()
    assert case.reference == "REF-2026-77"


def test_mark_reported_without_reference_means_none_was_issued(
    staff_client: Client, case: SafetyIncident
) -> None:
    response = post_json(staff_client, f"{BASE}/{case.pk}/reported", {"via": "hotline web form"})

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.reference == ""
    assert case.reported_via == "hotline web form"
    assert case.reported_at is not None
    assert "No reference number was issued." in case.note


def test_mark_reported_refuses_a_future_date_and_settled_cases(
    staff_client: Client, case: SafetyIncident
) -> None:
    future = (timezone.now() + timedelta(days=1)).isoformat()
    assert (
        post_json(staff_client, f"{BASE}/{case.pk}/reported", {"reported_at": future}).status_code
        == 422
    )
    incidents.mark_false_positive(case)
    assert post_json(staff_client, f"{BASE}/{case.pk}/reported", {}).status_code == 409


def test_list_is_text_only_and_filters_by_status(
    staff_client: Client, case: SafetyIncident, make_blocked: Callable[..., Page]
) -> None:
    other = incidents.open_or_link(make_blocked("other"), now=DETECTED).incident
    assert other is not None
    incidents.mark_closed(other)

    response = staff_client.get(BASE)

    assert response.status_code == 200
    raw = response.content.decode()
    for forbidden in ("https://", MEDIA_HOST, TITLE, TAG):
        assert forbidden not in raw, forbidden
    [item] = response.json()["items"]
    assert item["id"] == case.pk
    assert item["categories"] == ["minor"]
    assert item["url_count"] == 2
    assert item["sites"] == ["example.test", "mirror.example.test"]
    settled = staff_client.get(BASE, {"status": "settled"}).json()["items"]
    assert [i["id"] for i in settled] == [other.pk]
    assert len(staff_client.get(BASE, {"status": "all"}).json()["items"]) == 2
    assert staff_client.get(BASE, {"status": "bogus"}).status_code == 422


def test_reply_address_comes_from_settings(
    staff_client: Client, case: SafetyIncident, settings: Settings
) -> None:
    settings.SAFETY_REPORT_REPLY_TO = ""

    payload = draft(staff_client, case).json()

    assert "@" not in body_of(payload, "de")
    assert "@" not in body_of(payload, "en")


def test_channel_matches_the_recorded_reports() -> None:
    """The channel the draft suggests is the one the backfill records reports under."""
    assert REPORT_CHANNEL == REPORTED_VIA


@pytest.fixture
def customer_client(db: None) -> Client:
    organisation: Organisation = make_organisation("Customer Studio")
    return client_for(add_member(organisation, "customer-owner"))


def test_incident_endpoints_are_staff_only(customer_client: Client, case: SafetyIncident) -> None:
    paths = (BASE, f"{BASE}/{case.pk}/report-draft")
    for path in paths:
        assert Client().get(path).status_code == 401
        assert customer_client.get(path).status_code == 403
        assert b"example.test" not in customer_client.get(path).content
    reported = f"{BASE}/{case.pk}/reported"
    assert post_json(Client(), reported, {}).status_code == 401
    assert post_json(customer_client, reported, {}).status_code == 403
    case.refresh_from_db()
    assert case.status == SafetyIncident.Status.OPEN


def test_incident_endpoints_do_not_exist_on_the_customer_surface(
    staff_client: Client, case: SafetyIncident, settings: Settings
) -> None:
    settings.PUBLIC_SURFACE = "customer"

    for path in (BASE, f"{BASE}/{case.pk}/report-draft"):
        assert staff_client.get(path).status_code == 404
    assert post_json(staff_client, f"{BASE}/{case.pk}/reported", {}).status_code == 404
