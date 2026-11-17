"""The internal reaction statistics are readable in the provider admin (ADR 0027 amendment)."""

from datetime import timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from catalog.models import HosterContact, HostingProvider


def test_provider_admin_shows_speed_statistics(admin_client: Client) -> None:
    provider = HostingProvider.objects.create(asn=24940, name="HETZNER-AS")
    sent = timezone.now() - timedelta(days=40)
    for hours in (5, 30, 48):
        HosterContact.objects.create(
            provider=provider,
            channel=HosterContact.Channel.EMAIL,
            sent_at=sent,
            responded_at=sent + timedelta(hours=2),
            removed_observed_at=sent + timedelta(hours=hours),
            outcome=HosterContact.Outcome.REMOVED,
        )
    HosterContact.objects.create(
        provider=provider, channel=HosterContact.Channel.EMAIL, sent_at=sent
    )

    response = admin_client.get(
        reverse("admin:catalog_hostingprovider_change", args=(provider.pk,))
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "Takedown speed (internal band)" in body
    assert "fast" in body  # median 48 h
    assert "3 of 4 (75%)" in body
    assert "48 h" in body and "never" in body
