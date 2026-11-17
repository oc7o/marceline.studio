"""The `hosting` section of a finding (ADR 0027), from the negative side first.

- another organisation learns nothing: its case is a 404 byte-identical to a missing one, and its
  lists carry no hostname, abuse contact or provider of ours;
- a safety-flagged page contributes nothing — not its site's host, not its CDN, not a record;
- no customer endpoint enumerates hosts, providers or sites, and our notice history reaches a
  customer only as a rounded, past-tense response history — never counts, exact rates, durations
  or a forecast band (operator decisions 2026-09-16 and 2026-09-17).

Then what a looked-up host, a never-looked-up host and a CDN look like. Records are created
directly; nothing here performs a lookup.
"""

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import get_resolver
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from catalog.hosting.history import REMOVAL_TIMES
from catalog.models import HosterContact, HostingProvider, HostingRecord, Site
from index.models import Media, Page
from marceline.api import api
from marceline.tests.app_helpers import (
    APP,
    UNKNOWN_ORIGINAL,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.models import Reference, ReferenceLeak

HOST_FIELDS = {
    "hostname",
    "lookup",
    "looked_up_at",
    "stale",
    "addresses",
    "asn",
    "as_name",
    "provider",
    "country",
    "jurisdiction",
    "cdn",
    "abuse_contact",
    "registrar",
    "response_history",
}


@pytest.fixture
def owner_a(db: None) -> User:
    return add_member(make_organisation("Alpha Studio"), "alpha-owner")


@pytest.fixture
def owner_b(db: None) -> User:
    return add_member(make_organisation("Bravo Creator"), "bravo-owner")


@pytest.fixture
def original_a(owner_a: User) -> Reference:
    return make_reference(owner_a, title="Alpha's own original")


def hoster(asn: int, name: str, country: str) -> HostingProvider:
    return HostingProvider.objects.get_or_create(
        asn=asn, defaults={"name": name, "country": country}
    )[0]


def record(hostname: str, **fields: Any) -> HostingRecord:
    values: dict[str, Any] = {
        "status": HostingRecord.Status.COMPLETE,
        "looked_up_at": timezone.now(),
        **fields,
    }
    return HostingRecord.objects.create(hostname=hostname, **values)


@pytest.fixture
def finding_a(
    original_a: Reference,
    site: Site,
    make_page: Callable[[str], Page],
    make_media: Callable[..., Media],
) -> Page:
    """Alpha's finding on a German-hosted site whose previews come from a Cloudflare CDN host."""
    page = make_page("alpha-leak")
    ReferenceLeak.objects.create(reference=original_a, page=page, status="confirmed")
    make_media(page, "https://thumbs.cdn-one.test/p/1.jpg")
    make_media(page, "https://img.cdn-new.test/p/2.jpg", Media.Kind.FRAME)

    hetzner = hoster(24940, "HETZNER-AS", "DE")
    record(
        site.domain,
        addresses=["192.0.2.10"],
        address="192.0.2.10",
        asn=24940,
        as_name="HETZNER-AS",
        provider=hetzner,
        organisation="Example Hosting GmbH",
        country="DE",
        abuse_email="abuse@hosting.example",
        abuse_source=HostingRecord.AbuseSource.IP_NETWORK,
        abuse_source_url="https://rdap.db.ripe.net/ip/192.0.2.10",
        registrable_domain=site.domain,
        registrar_name="Example Registrar",
        registrar_abuse_email="abuse@registrar.example",
        registrar_source_url="https://rdap.example/domain/example.test",
    )
    cloudflare = hoster(13335, "CLOUDFLARENET", "US")
    record(
        "thumbs.cdn-one.test",
        status=HostingRecord.Status.PARTIAL,
        errors=["ip_network_no_abuse_contact"],
        addresses=["104.16.0.1"],
        asn=13335,
        provider=cloudflare,
        country="US",
        cdn="cloudflare",
        abuse_email="registrar-abuse@registrar.example",
        abuse_source=HostingRecord.AbuseSource.REGISTRAR,
        abuse_source_url="https://rdap.example/domain/cdn-one.test",
        looked_up_at=timezone.now() - timedelta(days=45),
    )
    for outcome in ("removed", "removed", "removed", "refused"):
        HosterContact.objects.create(
            provider=cloudflare,
            channel=HosterContact.Channel.EMAIL,
            sent_at=timezone.now() - timedelta(days=3),
            responded_at=timezone.now() - timedelta(hours=50),
            outcome=outcome,
        )
    return page


# --- negative side ------------------------------------------------------------------------------


def test_a_foreign_case_with_hosting_is_indistinguishable_from_a_missing_one(
    owner_b: User, original_a: Reference, finding_a: Page
) -> None:
    client = client_for(owner_b)

    foreign = client.get(f"{APP}/cases/{public_id_of(original_a)}")
    missing = client.get(f"{APP}/cases/{UNKNOWN_ORIGINAL}")

    assert foreign.status_code == 404
    assert foreign.content == missing.content


def test_another_organisation_sees_no_host_of_ours(
    owner_b: User, original_a: Reference, finding_a: Page
) -> None:
    client = client_for(owner_b)
    make_reference(owner_b, title="Bravo's own original")

    for path in ("findings", "cases", "removals", "originals", "me"):
        body = client.get(f"{APP}/{path}").content.decode()
        for secret in ("cdn-one.test", "abuse@hosting.example", "HETZNER", "Example Registrar"):
            assert secret not in body, f"{secret} leaked into /{path}"


def test_a_flagged_page_contributes_no_host_at_all(
    owner_a: User,
    original_a: Reference,
    finding_a: Page,
    make_media: Callable[..., Media],
) -> None:
    other_site = Site.objects.create(domain="flagged-site.test")
    flagged = Page.objects.create(
        site=other_site,
        url="https://flagged-site.test/x",
        safety_status=Page.SafetyStatus.REVIEW,
        safety_score=1.0,
    )
    ReferenceLeak.objects.create(reference=original_a, page=flagged, status="confirmed")
    make_media(flagged, "https://secret.cdn-two.test/x.jpg")
    record("flagged-site.test", country="NL", abuse_email="abuse@flagged.example")
    record("secret.cdn-two.test", country="NL", abuse_email="abuse@secret.example")
    client = client_for(owner_a)

    for response in (
        client.get(f"{APP}/findings"),
        client.get(f"{APP}/cases/{public_id_of(original_a)}"),
    ):
        body = response.content.decode()
        assert response.status_code == 200
        for secret in ("flagged-site", "cdn-two", "abuse@flagged", "abuse@secret"):
            assert secret not in body


def test_no_customer_endpoint_enumerates_hosts_providers_or_sites() -> None:
    patterns: list[str] = []

    def walk(resolver: Any, prefix: str) -> None:
        for entry in resolver.url_patterns:
            if hasattr(entry, "url_patterns"):
                walk(entry, prefix + str(entry.pattern))
            else:
                patterns.append(prefix + str(entry.pattern))

    walk(get_resolver(), "")
    customer = [path for path in patterns if path.startswith("api/v1/app/")]
    assert customer, "the customer API must be routed"
    for path in customer:
        assert not any(word in path for word in ("host", "provider", "site", "registrar")), path


# --- what it says -------------------------------------------------------------------------------


def hosting_of(client: Client, original: Reference) -> dict[str, Any]:
    [finding] = client.get(f"{APP}/cases/{public_id_of(original)}").json()["findings"]
    hosting: dict[str, Any] = finding["hosting"]
    return hosting


def test_page_host_content_hosts_jurisdiction_and_sources(
    owner_a: User, original_a: Reference, finding_a: Page, site: Site
) -> None:
    client = client_for(owner_a)
    hosting = hosting_of(client, original_a)

    assert set(hosting) == {"page_host", "content_hosts"}
    page_host = hosting["page_host"]
    assert set(page_host) == HOST_FIELDS
    assert (page_host["hostname"], page_host["lookup"], page_host["stale"]) == (
        site.domain,
        "complete",
        False,
    )
    assert (page_host["country"], page_host["jurisdiction"], page_host["cdn"]) == ("DE", "eu", None)
    assert page_host["provider"] == "Example Hosting GmbH"
    assert page_host["abuse_contact"] == {
        "email": "abuse@hosting.example",
        "source": "ip_network",
        "source_url": "https://rdap.db.ripe.net/ip/192.0.2.10",
    }
    assert page_host["registrar"] == {
        "name": "Example Registrar",
        "abuse_email": "abuse@registrar.example",
        "source_url": "https://rdap.example/domain/example.test",
    }
    # No notice to this provider yet: no response history.
    assert page_host["response_history"] is None

    new, cdn = hosting["content_hosts"]  # sorted by hostname
    assert (cdn["hostname"], cdn["cdn"], cdn["jurisdiction"]) == (
        "thumbs.cdn-one.test",
        "cloudflare",
        "us",
    )
    assert (cdn["lookup"], cdn["stale"]) == ("partial", True)
    assert cdn["abuse_contact"]["source"] == "registrar"
    # Four observed notices are below the default minimum of five: still nothing to show.
    assert cdn["response_history"] is None

    # Never looked up: said, not omitted, and nothing invented.
    assert new == {
        "hostname": "img.cdn-new.test",
        "lookup": "not_looked_up",
        "looked_up_at": None,
        "stale": False,
        "addresses": [],
        "asn": None,
        "as_name": None,
        "provider": None,
        "country": None,
        "jurisdiction": "unknown",
        "cdn": None,
        "abuse_contact": None,
        "registrar": None,
        "response_history": None,
    }
    # The same section rides on the findings list.
    [listed] = client.get(f"{APP}/findings").json()
    assert listed["hosting"] == hosting


def test_the_response_history_is_rounded_and_needs_the_minimum_sample(
    owner_a: User, original_a: Reference, finding_a: Page, settings: Settings
) -> None:
    client = client_for(owner_a)
    settings.HOSTING_HISTORY_MIN_NOTICES = 4

    _, cdn = hosting_of(client, original_a)["content_hosts"]

    # Three removals reported 22 h after sending, one refusal: about 75 %, usually the same day.
    assert cdn["response_history"] == {"removed_percent": 75, "usual_removal_time": "same_day"}

    settings.HOSTING_HISTORY_MIN_NOTICES = 5
    _, cdn = hosting_of(client, original_a)["content_hosts"]
    assert cdn["response_history"] is None

    cloudflare = HostingProvider.objects.get(asn=13335)
    HosterContact.objects.create(
        provider=cloudflare,
        channel=HosterContact.Channel.EMAIL,
        sent_at=timezone.now() - timedelta(days=2),
        outcome="no_response",
    )
    _, cdn = hosting_of(client, original_a)["content_hosts"]
    assert cdn["response_history"] == {"removed_percent": 60, "usual_removal_time": "same_day"}


def test_the_notice_history_itself_never_reaches_a_customer(
    owner_a: User, original_a: Reference, finding_a: Page, settings: Settings
) -> None:
    settings.HOSTING_HISTORY_MIN_NOTICES = 1
    client = client_for(owner_a)
    for response in (
        client.get(f"{APP}/findings"),
        client.get(f"{APP}/cases/{public_id_of(original_a)}"),
    ):
        body = response.content.decode()
        assert "removed_percent" in body  # the rounded history is there, the counts are not
        for internal in (
            "notices",
            "responded",
            "outcome",
            "refused",
            "sent_at",
            "observed",
            "decided",
            "median",
            "p90",
            "chance",
            "speed",
        ):
            assert internal not in body


def test_the_customer_schema_carries_rounded_history_never_counts_or_bands() -> None:
    schemas = api.get_openapi_schema()["components"]["schemas"]
    host = schemas["CustomerHostOut"]["properties"]
    history = schemas["CustomerResponseHistoryOut"]["properties"]

    assert set(host) == HOST_FIELDS
    assert set(history) == {"removed_percent", "usual_removal_time"}
    internal = ("median", "p90", "duration", "hours", "days", "sample", "responded", "observed")
    for name in (*host, *history, *schemas["CustomerFindingHostingOut"]["properties"]):
        assert not any(word in name for word in internal), name
        assert not {"count", "counts", "total"} & set(name.split("_")), name
    assert history["usual_removal_time"]["anyOf"][0]["enum"] == list(REMOVAL_TIMES)
    text = json.dumps([schemas[name] for name in ("CustomerHostOut", "CustomerResponseHistoryOut")])
    for forecast in ("takedown", "chance", "likely", "expect", "predict", "probab"):
        assert forecast not in text.lower(), forecast


def test_hosting_is_codes_and_data_never_a_media_reference(
    owner_a: User, original_a: Reference, finding_a: Page
) -> None:
    body = client_for(owner_a).get(f"{APP}/cases/{public_id_of(original_a)}").content.decode()

    assert "/p/1.jpg" not in body and "/p/2.jpg" not in body
    assert "media" not in body.lower()


def test_a_site_with_nothing_looked_up_yet(
    owner_a: User, original_a: Reference, make_page: Callable[[str], Page]
) -> None:
    page = make_page("fresh-find")
    ReferenceLeak.objects.create(reference=original_a, page=page, status="confirmed")

    hosting = hosting_of(client_for(owner_a), original_a)

    assert hosting["page_host"]["lookup"] == "not_looked_up"
    assert hosting["content_hosts"] == []


def test_organisation_segment_does_not_matter(db: None) -> None:
    """Enterprise sees hosting exactly like Professional: segment is billing only (ADR 0021)."""
    owner = add_member(make_organisation("Studio", Organisation.Segment.ENTERPRISE), "studio")
    original = make_reference(owner)
    site = Site.objects.create(domain="enterprise-site.test")
    page = Page.objects.create(site=site, url="https://enterprise-site.test/a", safety_score=1.0)
    ReferenceLeak.objects.create(reference=original, page=page, status="confirmed")
    record("enterprise-site.test", country="US")

    hosting = hosting_of(client_for(owner), original)

    assert hosting["page_host"]["jurisdiction"] == "us"
