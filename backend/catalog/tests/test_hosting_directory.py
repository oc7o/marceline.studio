"""The provider directory in the database: roles of a host, overrides, channel versions."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from catalog.hosting.directory import (
    ProviderRole,
    due_channels,
    mark_rechecked,
    provider_roles,
    retire_channel,
    supersede_channel,
)
from catalog.models import (
    ComplaintChannel,
    DirectoryProvider,
    HostingRecord,
    ProviderFunctionOverride,
    ProviderIdentifier,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def make_record(hostname: str, **fields: object) -> HostingRecord:
    defaults: dict[str, object] = {
        "status": HostingRecord.Status.COMPLETE,
        "looked_up_at": NOW - timedelta(days=2),
    }
    defaults.update(fields)
    return HostingRecord.objects.create(hostname=hostname, **defaults)


def make_channel(provider: DirectoryProvider, **fields: object) -> ComplaintChannel:
    defaults: dict[str, object] = {
        "kind": ComplaintChannel.Kind.ABUSE_EMAIL,
        "email": "abuse@provider.example",
        "source_url": "https://provider.example/legal",
        "checked_at": NOW - timedelta(days=1),
    }
    defaults.update(fields)
    return ComplaintChannel.objects.create(provider=provider, **defaults)


def roles_by_slot(hostname: str) -> dict[str, ProviderRole]:
    return {role.slot: role for role in provider_roles([hostname], NOW)[hostname]}


def test_undirected_host_falls_back_to_rdap_facts() -> None:
    make_record(
        "site.example",
        asn=64500,
        as_name="EXAMPLE-AS",
        organisation="Example Hosting",
        country="NL",
        abuse_email="abuse@hosting.example",
        abuse_source=HostingRecord.AbuseSource.IP_NETWORK,
        abuse_source_url="https://rdap.example/ip/192.0.2.1",
        registrar_name="Example Registrar",
        registrar_iana_id="9999",
        registrar_abuse_email="abuse@registrar.example",
        registrar_source_url="https://rdap.example/domain/site.example",
    )

    roles = roles_by_slot("site.example")

    network = roles["network"]
    assert network.provider_name == "Example Hosting"
    assert network.country == "NL"
    assert network.nexus == "eu_facing"
    assert network.function == "origin_host"
    suggested = network.suggested
    assert suggested is not None
    assert suggested.kind == "abuse_email"
    assert suggested.email == "abuse@hosting.example"
    assert suggested.source == "rdap"
    assert suggested.support == "primary"
    assert suggested.transfer_basis == "counsel_review"
    assert not suggested.recheck_due

    registrar = roles["registrar"]
    assert registrar.function == "registrar"
    assert registrar.nexus == "unknown"
    assert registrar.suggested is not None
    assert registrar.suggested.support == "additional_contact_only"
    assert "service" not in roles


def test_directory_channels_replace_the_rdap_fallback_and_cdn_stays_a_proxy() -> None:
    make_record(
        "cdn.site.example",
        asn=13335,
        as_name="CLOUDFLARENET",
        country="US",
        cdn="cloudflare",
        abuse_email="abuse@cloudflare.example",
        abuse_source=HostingRecord.AbuseSource.IP_NETWORK,
    )
    provider = DirectoryProvider.objects.create(name="Cloudflare", establishment_country="US")
    ProviderIdentifier.objects.create(provider=provider, kind="asn", value="13335")
    make_channel(
        provider,
        kind=ComplaintChannel.Kind.WEB_FORM,
        email="",
        url="https://provider.example/abuse",
        forwards_to_site_operator=True,
        forwarding_source_url="https://provider.example/abuse-policy",
    )

    network = roles_by_slot("cdn.site.example")["network"]

    assert network.provider_name == "Cloudflare"
    assert network.function == "reverse_proxy_cdn"
    assert network.nexus == "us_nexus"
    assert [channel.source for channel in network.channels] == ["directory"]
    assert network.suggested is not None
    assert network.suggested.support == "forwarding_only"
    assert network.suggested.forwards_to_site_operator is True


def test_override_with_note_is_the_only_way_to_storing_cdn() -> None:
    make_record("media.site.example", asn=13335, cdn="cloudflare", country="US")
    ProviderFunctionOverride.objects.create(
        hostname="media.site.example",
        slot="network",
        function="storing_cdn",
        note="Verified: stored in the provider's object storage product.",
    )

    network = roles_by_slot("media.site.example")["network"]

    assert network.function == "storing_cdn"
    assert network.function_source == "override"
    assert network.function_note.startswith("Verified")


def test_override_requires_a_note_and_directory_never_stores_storing_cdn() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ProviderFunctionOverride.objects.create(
            hostname="x.example", slot="network", function="storing_cdn", note=""
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        DirectoryProvider.objects.create(name="Some CDN", function="storing_cdn")


def test_hostname_identifier_adds_a_service_role() -> None:
    platform = DirectoryProvider.objects.create(
        name="Example File Host", function="file_host", nexus="eu_facing"
    )
    ProviderIdentifier.objects.create(provider=platform, kind="hostname", value="files.example")
    make_channel(
        platform,
        kind=ComplaintChannel.Kind.DSA_ART16,
        email="",
        url="https://files.example/dsa",
        languages="en, de",
    )

    roles = provider_roles(["dl3.files.example"], NOW)["dl3.files.example"]

    assert [role.slot for role in roles] == ["service"]
    service = roles[0]
    assert service.function == "file_host"
    assert service.function_source == "directory"
    assert service.suggested is not None
    assert service.suggested.kind == "dsa_art16"
    assert service.suggested.support == "primary"
    assert service.suggested.languages == "en, de"


def test_superseding_keeps_the_old_version() -> None:
    provider = DirectoryProvider.objects.create(name="Example Hosting")
    first = make_channel(provider)
    edited = ComplaintChannel.objects.get(pk=first.pk)
    edited.email = "copyright@provider.example"

    supersede_channel(first.pk, edited, NOW)

    first.refresh_from_db()
    assert edited.pk != first.pk
    assert first.email == "abuse@provider.example"
    assert first.superseded_at == NOW
    assert first.superseded_by_id == edited.pk
    assert list(provider.channels.filter(superseded_at__isnull=True)) == [edited]
    with pytest.raises(ValueError):
        supersede_channel(first.pk, ComplaintChannel.objects.get(pk=first.pk), NOW)


def test_recheck_reminder_and_versions() -> None:
    provider = DirectoryProvider.objects.create(name="Example Hosting")
    old = make_channel(provider, checked_at=NOW - timedelta(days=120))
    fresh = make_channel(provider, kind="web_form", email="", url="https://provider.example/f")

    assert due_channels(NOW) == [old]
    network_roles = provider_roles(["nothing.example"], NOW)
    assert network_roles == {"nothing.example": []}

    renewed = mark_rechecked(old, NOW)

    assert renewed.checked_at == NOW
    assert due_channels(NOW) == []
    retire_channel(fresh, NOW)
    assert list(provider.channels.filter(superseded_at__isnull=True)) == [renewed]


def test_channel_validation() -> None:
    provider = DirectoryProvider.objects.create(name="Example Hosting")
    personal = ComplaintChannel(
        provider=provider,
        kind="abuse_email",
        email="jane.doe@provider.example",
        source_url="https://provider.example/legal",
        checked_at=NOW,
        forwards_to_site_operator=False,
    )
    with pytest.raises(ValidationError) as error:
        personal.full_clean()
    assert set(error.value.message_dict) == {"email", "forwarding_source_url"}
    with pytest.raises(IntegrityError), transaction.atomic():
        ComplaintChannel.objects.create(
            provider=provider,
            kind="web_form",
            source_url="https://provider.example/legal",
            checked_at=NOW,
        )
    ComplaintChannel.objects.create(
        provider=provider,
        kind="none_known",
        source_url="https://provider.example/legal",
        checked_at=NOW,
    )


def test_identifier_normalisation() -> None:
    provider = DirectoryProvider.objects.create(name="Example Hosting")
    asn = ProviderIdentifier(provider=provider, kind="asn", value="AS64500")
    asn.full_clean()
    assert asn.value == "64500"
    host = ProviderIdentifier(provider=provider, kind="hostname", value="https://files.example/")
    with pytest.raises(ValidationError):
        host.full_clean()
