"""Staff maintain the provider directory in the admin; channel edits are versions."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from catalog.models import (
    ComplaintChannel,
    DirectoryProvider,
    HostingRecord,
    ProviderFunctionOverride,
    ProviderIdentifier,
)

pytestmark = pytest.mark.django_db


def channel_form(provider: DirectoryProvider, **overrides: str) -> dict[str, str]:
    now = timezone.localtime()
    form = {
        "provider": str(provider.pk),
        "kind": "abuse_email",
        "url": "",
        "email": "abuse@provider.example",
        "languages": "en",
        "forwards_to_site_operator": "unknown",
        "forwarding_source_url": "",
        "publishes_to_transparency_db": "unknown",
        "transparency_source_url": "",
        "transfer_basis": "counsel_review",
        "transfer_note": "",
        "source_url": "https://provider.example/legal",
        "checked_at_0": now.strftime("%Y-%m-%d"),
        "checked_at_1": now.strftime("%H:%M:%S"),
        "note": "",
    }
    form.update(overrides)
    return form


def test_editing_a_channel_writes_a_new_version(admin_client: Client) -> None:
    provider = DirectoryProvider.objects.create(name="Example Hosting")
    old = ComplaintChannel.objects.create(
        provider=provider,
        kind="abuse_email",
        email="abuse@provider.example",
        source_url="https://provider.example/legal",
        checked_at=timezone.now() - timedelta(days=200),
    )

    response = admin_client.post(
        reverse("admin:catalog_complaintchannel_change", args=(old.pk,)),
        channel_form(provider, email="copyright@provider.example"),
    )

    assert response.status_code == 302
    old.refresh_from_db()
    current = ComplaintChannel.objects.get(superseded_at__isnull=True)
    assert current.pk != old.pk
    assert current.email == "copyright@provider.example"
    assert old.email == "abuse@provider.example"
    assert old.superseded_by_id == current.pk
    # The old version is history: read-only.
    refused = admin_client.post(
        reverse("admin:catalog_complaintchannel_change", args=(old.pk,)),
        channel_form(provider, email="dmca@provider.example"),
    )
    assert refused.status_code == 403


def test_a_named_persons_mailbox_is_refused(admin_client: Client) -> None:
    provider = DirectoryProvider.objects.create(name="Example Hosting")

    response = admin_client.post(
        reverse("admin:catalog_complaintchannel_add"),
        channel_form(provider, email="jane.doe@provider.example"),
    )

    assert response.status_code == 200
    assert "role address" in response.content.decode()
    assert not ComplaintChannel.objects.exists()


def test_changelists_remind_of_due_rechecks(admin_client: Client) -> None:
    provider = DirectoryProvider.objects.create(name="Example Hosting")
    ComplaintChannel.objects.create(
        provider=provider,
        kind="web_form",
        url="https://provider.example/form",
        source_url="https://provider.example/legal",
        checked_at=timezone.now() - timedelta(days=91),
    )

    for name in ("catalog_directoryprovider", "catalog_complaintchannel"):
        body = admin_client.get(reverse(f"admin:{name}_changelist"), follow=True).content.decode()
        assert "due for re-checking" in body
    filtered = admin_client.get(
        reverse("admin:catalog_complaintchannel_changelist") + "?recheck_due=yes"
    )
    assert "provider.example/form" in filtered.content.decode()


def test_hosting_record_page_shows_roles_and_suggested_channel(admin_client: Client) -> None:
    record = HostingRecord.objects.create(
        hostname="media.site.example",
        status="complete",
        looked_up_at=timezone.now(),
        asn=13335,
        country="US",
        cdn="cloudflare",
    )
    provider = DirectoryProvider.objects.create(name="Cloudflare", establishment_country="US")
    ProviderIdentifier.objects.create(provider=provider, kind="asn", value="13335")
    ComplaintChannel.objects.create(
        provider=provider,
        kind="web_form",
        url="https://provider.example/abuse",
        source_url="https://provider.example/abuse",
        checked_at=timezone.now(),
    )

    body = admin_client.get(
        reverse("admin:catalog_hostingrecord_change", args=(record.pk,))
    ).content.decode()

    assert "reverse_proxy_cdn" in body
    assert "us_nexus" in body
    assert "forwarding_only" in body


def test_override_needs_a_note(admin_client: Client) -> None:
    response = admin_client.post(
        reverse("admin:catalog_providerfunctionoverride_add"),
        {"hostname": "media.site.example", "slot": "network", "function": "storing_cdn"},
    )

    assert response.status_code == 200
    assert not ProviderFunctionOverride.objects.exists()
