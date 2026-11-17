"""One hostname's lookup end to end (fakes for DNS and RDAP), storing it, and the refresh policy."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from django.test import override_settings

from catalog.hosting.cooperation import Cooperation
from catalog.hosting.dns import DnsError, Origin
from catalog.hosting.history import ResponseHistory
from catalog.hosting.lookup import HostFacts, lookup_host
from catalog.hosting.rdap import RdapClient
from catalog.hosting.services import (
    cooperation_by_provider,
    due_hostnames,
    history_by_provider,
    is_stale,
    needs_lookup,
    normalize_hostname,
    speed_by_provider,
    store_facts,
)
from catalog.hosting.speed import NEVER
from catalog.models import HosterContact, HostingProvider, HostingRecord
from core.http import PoliteClient

FIXTURES = Path(__file__).parent / "fixtures" / "rdap"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

ROUTES = {
    "https://data.iana.org/rdap/ipv4.json": "bootstrap_ipv4.json",
    "https://data.iana.org/rdap/dns.json": "bootstrap_dns.json",
    "https://rdap.arin.net/registry/ip/104.16.0.1": "ip_arin_cloudflare.json",
    "https://rdap.db.ripe.net/ip/5.9.0.1": "ip_ripe_hetzner.json",
    "https://rdap.db.ripe.net/ip/5.9.0.2": "ip_ripe_no_abuse.json",
    "https://rdap.verisign.com/com/v1/domain/cloudflare.com": "domain_verisign_cloudflare.json",
    "https://rdap.verisign.com/com/v1/domain/example-hoster.com": "domain_verisign_cloudflare.json",
}
ADDRESSES = {
    "www.cloudflare.com": ["104.16.0.1", "2606:4700::1"],
    "cdn.example-hoster.com": ["5.9.0.1"],
    "static.example-hoster.com": ["5.9.0.2"],
}
ORIGINS = {
    "104.16.0.1": Origin(13335, "104.16.0.0/20", "US", "arin", "CLOUDFLARENET - Cloudflare"),
    "5.9.0.1": Origin(24940, "5.9.0.0/16", "DE", "ripencc", "HETZNER-AS - Hetzner Online GmbH"),
    "5.9.0.2": Origin(24940, "5.9.0.0/16", "DE", "ripencc", "HETZNER-AS - Hetzner Online GmbH"),
}


def handler(request: httpx.Request) -> httpx.Response:
    name = ROUTES.get(str(request.url))
    if name is None:
        return httpx.Response(404, content=b"{}")
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


def rdap() -> RdapClient:
    client = PoliteClient(
        "MarcelineBot/0.1",
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    return RdapClient(client)


def origin_of(address: str, txt: Callable[[str], list[str]]) -> Origin | None:
    return ORIGINS.get(address)


def look_up(hostname: str) -> HostFacts:
    return lookup_host(
        hostname,
        rdap=rdap(),
        resolve=lambda name: ADDRESSES.get(name, []),
        txt=lambda name: [],
        origin_lookup=origin_of,
    )


def test_a_cdn_fronted_host() -> None:
    facts = look_up("www.cloudflare.com")

    assert facts.status == "complete"
    assert facts.addresses == ("104.16.0.1", "2606:4700::1")
    assert (facts.cdn, facts.country) == ("cloudflare", "US")  # ARIN has no country; origin has
    assert facts.abuse == (
        "abuse@cloudflare.com",
        "ip_network",
        "https://rdap.arin.net/registry/ip/104.16.0.0",
    )
    assert facts.registrar is not None and facts.registrar.name == "Cloudflare, Inc."


def test_registrar_abuse_is_the_fallback_when_the_network_names_none() -> None:
    facts = look_up("static.example-hoster.com")

    assert facts.abuse == (
        "registrar-abuse@cloudflare.com",
        "registrar",
        "https://rdap.verisign.com/com/v1/domain/cloudflare.com",
    )
    assert facts.domain == "example-hoster.com"
    assert facts.errors == ("ip_network_no_abuse_contact",)
    assert facts.status == "partial"


def test_every_part_fails_on_its_own() -> None:
    def broken_origin(address: str, txt: Callable[[str], list[str]]) -> Origin | None:
        raise DnsError("timeout")

    facts = lookup_host(
        "cdn.unknown-registry.test",
        rdap=rdap(),
        resolve=lambda name: ["5.9.0.1"],
        txt=lambda name: [],
        origin_lookup=broken_origin,
    )

    assert facts.errors == ("asn_lookup_failed", "domain_rdap_no_registry")
    assert facts.abuse[0] == "abuse@hetzner.com"
    assert (facts.country, facts.cdn, facts.status) == ("DE", None, "partial")


def test_a_name_that_does_not_resolve_and_has_no_registrar_failed() -> None:
    facts = lookup_host(
        "gone.unknown-registry.test",
        rdap=rdap(),
        resolve=lambda name: [],
        txt=lambda name: [],
        origin_lookup=origin_of,
    )
    assert facts.status == "failed"
    assert facts.errors == ("dns_no_address", "domain_rdap_no_registry")


@pytest.mark.django_db
def test_storing_a_lookup_and_its_provider() -> None:
    record = store_facts(look_up("cdn.example-hoster.com"), NOW)

    assert record.status == HostingRecord.Status.COMPLETE
    assert (record.asn, record.country, record.cdn) == (24940, "DE", "")
    assert (record.organisation, record.network_name) == ("Hetzner Online GmbH", "HETZNER-fsn1-dc7")
    assert (record.abuse_email, record.abuse_source) == ("abuse@hetzner.com", "ip_network")
    assert record.abuse_source_url == "https://rdap.db.ripe.net/ip/5.9.0.1"
    assert (record.registrable_domain, record.registrar_iana_id) == ("example-hoster.com", "1910")
    assert record.provider is not None and record.provider.asn == 24940
    assert record.looked_up_at == NOW

    store_facts(look_up("static.example-hoster.com"), NOW)
    assert HostingProvider.objects.count() == 1, "one provider per AS, however many hosts"


@pytest.mark.django_db
def test_a_failed_refresh_keeps_what_was_known() -> None:
    store_facts(look_up("cdn.example-hoster.com"), NOW)
    failed = HostFacts(
        hostname="cdn.example-hoster.com",
        addresses=(),
        address=None,
        origin=None,
        network=None,
        domain=None,
        registrar=None,
        cdn=None,
        errors=("dns_no_address",),
    )

    record = store_facts(failed, NOW + timedelta(days=31))

    assert record.status == HostingRecord.Status.FAILED
    assert record.errors == ["dns_no_address"]
    assert record.abuse_email == "abuse@hetzner.com"
    assert record.looked_up_at == NOW + timedelta(days=31)


def record(status: str, looked_up_at: datetime) -> HostingRecord:
    return HostingRecord(hostname="h.example", status=status, looked_up_at=looked_up_at)


@override_settings(HOSTING_REFRESH_DAYS=30)
def test_refresh_policy() -> None:
    complete = HostingRecord.Status.COMPLETE
    failed = HostingRecord.Status.FAILED

    assert needs_lookup(None, NOW)
    assert not needs_lookup(record(complete, NOW - timedelta(days=29)), NOW)
    assert needs_lookup(record(complete, NOW - timedelta(days=30)), NOW)
    assert not needs_lookup(record("partial", NOW - timedelta(days=10)), NOW)
    # A failure is retried the next day, not a month later.
    assert not needs_lookup(record(failed, NOW - timedelta(hours=23)), NOW)
    assert needs_lookup(record(failed, NOW - timedelta(days=1)), NOW)
    assert is_stale(record(complete, NOW - timedelta(days=31)), NOW)
    assert not is_stale(record(complete, NOW - timedelta(days=1)), NOW)


@override_settings(HOSTING_REFRESH_DAYS=7)
def test_the_refresh_window_is_a_setting() -> None:
    assert needs_lookup(record(HostingRecord.Status.COMPLETE, NOW - timedelta(days=7)), NOW)


@pytest.mark.django_db
def test_due_hostnames_keeps_order_and_skips_fresh_records() -> None:
    HostingRecord.objects.create(hostname="fresh.example", status="complete", looked_up_at=NOW)
    HostingRecord.objects.create(
        hostname="old.example", status="complete", looked_up_at=NOW - timedelta(days=60)
    )
    names = ["old.example", "fresh.example", "new.example"]

    assert due_hostnames(names, NOW) == ["old.example", "new.example"]
    assert due_hostnames(names, NOW, refresh=True) == names


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CDN.Example.com.", "cdn.example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("example.com/path", ""),
        ("user@example.com", ""),
        ("", ""),
    ],
)
def test_normalize_hostname(value: str, expected: str) -> None:
    assert normalize_hostname(value) == expected


@pytest.mark.django_db
def test_cooperation_counts_are_empty_until_a_notice_exists() -> None:
    provider = HostingProvider.objects.create(asn=24940, name="HETZNER-AS")
    other = HostingProvider.objects.create(asn=13335, name="CLOUDFLARENET")

    assert cooperation_by_provider([provider.pk]) == {}

    for outcome in ("awaiting", "removed", "forwarded", "refused"):
        HosterContact.objects.create(
            provider=provider, channel="email", sent_at=NOW, outcome=outcome
        )

    assert cooperation_by_provider([provider.pk, other.pk]) == {
        provider.pk: Cooperation(notices_sent=4, decided=3, removed=1, forwarded=1)
    }


@pytest.mark.django_db
def test_speed_statistics_per_provider() -> None:
    provider = HostingProvider.objects.create(asn=24940, name="HETZNER-AS")
    other = HostingProvider.objects.create(asn=13335, name="CLOUDFLARENET")
    assert speed_by_provider([provider.pk, other.pk], NOW) == {}

    old = NOW - timedelta(days=40)
    for hours in (10, 30, 50):
        HosterContact.objects.create(
            provider=provider,
            channel="email",
            sent_at=old,
            responded_at=old + timedelta(hours=hours / 10),
            removed_observed_at=old + timedelta(hours=hours),
            outcome="removed",
        )
    HosterContact.objects.create(provider=provider, channel="email", sent_at=old)  # never
    HosterContact.objects.create(provider=provider, channel="email", sent_at=NOW)  # too young

    stats = speed_by_provider([provider.pk, other.pk], NOW)
    assert set(stats) == {provider.pk}
    speed = stats[provider.pk]
    assert (speed.responded, speed.response_samples) == (3, 4)
    assert (speed.median_to_response, speed.p90_to_response) == (timedelta(hours=5), NEVER)
    assert (speed.removals_timed, speed.removal_samples) == (3, 4)
    assert (speed.median_to_removal, speed.p90_to_removal) == (timedelta(hours=50), NEVER)
    # The same notices as a response history: 4 observed (the young one is not), 3 removed.
    assert history_by_provider([provider.pk, other.pk], NOW) == {
        provider.pk: ResponseHistory(
            observed=4,
            removed=3,
            removal_times=(timedelta(hours=10), timedelta(hours=30), timedelta(hours=50)),
        )
    }
