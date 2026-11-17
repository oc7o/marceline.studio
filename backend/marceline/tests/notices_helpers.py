"""Helpers for the notice-draft tests (issue #33, ADR 0035)."""

from datetime import UTC, datetime
from typing import Any

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Organisation
from catalog.models import HostingProvider, HostingRecord
from index.models import Page
from marceline.notices import services
from marceline.rights import services as rights_services
from marceline.tests.rights_helpers import declared

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def make_hosting_record(hostname: str, *, asn: int = 64500, **fields: object) -> HostingRecord:
    """A hosting record whose network role gets a `primary` RDAP fallback channel, linked to a
    real `HostingProvider` so `catalog.HosterContact` (cooperation history) can key off it."""
    provider, _ = HostingProvider.objects.get_or_create(
        asn=asn, defaults={"name": "Example Hosting"}
    )
    defaults: dict[str, object] = {
        "status": HostingRecord.Status.COMPLETE,
        "looked_up_at": NOW,
        "asn": asn,
        "as_name": "EXAMPLE-AS",
        "provider": provider,
        "organisation": "Example Hosting",
        "country": "NL",
        "abuse_email": "abuse@hosting.example",
        "abuse_source": HostingRecord.AbuseSource.IP_NETWORK,
        "abuse_source_url": "https://rdap.example/ip/192.0.2.1",
    }
    defaults.update(fields)
    return HostingRecord.objects.create(hostname=hostname, **defaults)


def playable(page: Page, *, checked_at: datetime | None = None) -> Page:
    page.availability = Page.Availability.PLAYABLE
    # The real clock, not NOW: the API paths judge freshness against `timezone.now()`, and a pinned
    # date made every fixture stale 48 hours later (2026-09-19 12:00 UTC, 47 tests at once). A
    # caller that passes `now=NOW` to a service still sees a fresh check — it is never older.
    page.availability_checked_at = checked_at or timezone.now()
    page.save(update_fields=["availability", "availability_checked_at"])
    return page


def rights_ready(organisation: Organisation, sha256: str, member: User, staff: User) -> None:
    """Take a work all the way: documents formally complete, the customer's own authority
    declaration made, partners affirmed — the rights side clear."""
    from marceline.tests.rights_helpers import authority_declared, documents_complete

    customer = rights_services.Actor.customer(member)
    rights_services.set_claimant_type(organisation, "self_producing_creator", customer)
    rights_services.affirm_no_partners(
        organisation,
        version="partners-none-2026-09-17",
        language="en",
        confirmed=True,
        actor=customer,
    )
    declared(organisation, sha256, member)
    rights_services.verify_identity(
        organisation,
        method="account_control_token",
        evidence_note="token seen on the creator's sales profile",
        actor=rights_services.Actor.staff(staff),
    )
    documents_complete(organisation, sha256, staff)
    authority_declared(organisation, sha256, member)


def claimant_input(**overrides: object) -> services.ClaimantInput:
    values: dict[str, object] = {"name": "Alpha Studio", "email": "rights@alpha.example"}
    values.update(overrides)
    return services.ClaimantInput(**values)  # type: ignore[arg-type]


def recipient_choice(**overrides: object) -> services.RecipientChoice:
    values: dict[str, object] = {
        "hostname": "hoster.example",
        "provider_name": "Example Hosting",
        "channel_kind": "abuse_email",
        "email": "abuse@hosting.example",
    }
    values.update(overrides)
    return services.RecipientChoice(**values)  # type: ignore[arg-type]


def all_confirmations(
    *, language: str = "en", version_overrides: dict[str, str] | None = None
) -> list[Any]:
    from marceline.notices import declarations
    from marceline.notices.models import REQUIRED_CONFIRMATIONS

    overrides = version_overrides or {}
    return [
        services.ConfirmationInput(
            kind=kind,
            version=overrides.get(kind, declarations.current(kind).version),
            language=language,
            confirmed=True,
        )
        for kind in REQUIRED_CONFIRMATIONS
    ]
