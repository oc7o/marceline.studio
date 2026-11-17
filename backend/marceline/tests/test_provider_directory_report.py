"""`provider_directory_report`: roles, suggested channels, re-check reminders and gaps."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog.models import (
    ComplaintChannel,
    DirectoryProvider,
    HostingRecord,
    ProviderIdentifier,
    Site,
)
from marceline import provider_directory

pytestmark = pytest.mark.django_db


def run(*args: str) -> str:
    out = StringIO()
    call_command("provider_directory_report", *args, stdout=out)
    return out.getvalue()


def test_report_lists_roles_due_channels_and_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    Site.objects.create(domain="site.example", name="Site")
    HostingRecord.objects.create(
        hostname="site.example",
        status="complete",
        looked_up_at=timezone.now(),
        asn=64500,
        organisation="Example Hosting",
        country="NL",
        abuse_email="abuse@hosting.example",
        abuse_source=HostingRecord.AbuseSource.IP_NETWORK,
        registrar_name="Example Registrar",
        registrar_iana_id="9999",
    )
    registrar = DirectoryProvider.objects.create(name="Example Registrar")
    ProviderIdentifier.objects.create(provider=registrar, kind="registrar_iana_id", value="9999")
    ComplaintChannel.objects.create(
        provider=registrar,
        kind="abuse_email",
        email="abuse@registrar.example",
        source_url="https://registrar.example/abuse",
        checked_at=timezone.now() - timedelta(days=100),
    )
    monkeypatch.setattr(
        provider_directory, "customer_hostnames", lambda: ["site.example", "cdn.example"]
    )

    output = run()

    assert "1 due for re-check" in output
    assert "due: Example Registrar abuse_email" in output
    assert "site.example (page host)" in output
    assert "cdn.example (content host)" in output
    assert "no hosting record yet" in output
    assert (
        "network: Example Hosting | country NL | nexus eu_facing (allocation_country) | "
        "function origin_host (derived) | suggested channel: abuse_email abuse@hosting.example "
        "[primary, from rdap, transfer counsel_review]"
    ) in output
    assert (
        "additional_contact_only, from directory, transfer counsel_review, re-check due" in output
    )
    assert "not covered by the directory: 1\n  Example Hosting (network)" in output


def test_named_hosts_and_due_only() -> None:
    output = run("--hostname", "Other.Example.")
    assert "named hosts: 1" in output
    assert "other.example (content host)" in output

    assert run("--due-only").strip().startswith("provider directory: 0 provider(s)")
    assert "named hosts" not in run("--due-only")
