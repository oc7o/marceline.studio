"""The `hosting` section of a finding: who serves it and whom to complain to (ADR 0027).

Read-only over `catalog.HostingRecord`; nothing here makes a request — records are filled by
`lookup_hosting`. A host nobody has looked up yet says `not_looked_up` with every fact null,
rather than being left out: a missing host would read as "nobody serves this".

Four rules:

- **Only the organisation's own findings.** Hosting is attached to findings the caller already
  passed the ownership check for; there is no endpoint that lists hosts, providers or sites. The
  operator decided on 2026-09-16 that providers, CDNs and their cooperation statistics may be
  shown, but the list of sites we crawl may not — so nothing here enumerates anything.
- **Flagged pages contribute nothing.** Callers pass visible findings; a page that is not visible
  is dropped here as well, and `marceline.hosting.content_hosts_by_page` ignores it too.
- **Rounded history, never the numbers, never a forecast.** Our notice history with a provider is
  internal; a customer sees `response_history` for the providers of their own findings only — the
  removal share rounded to 5 %, the median time to removal as a floor bucket, and nothing below
  `HOSTING_HISTORY_MIN_NOTICES` observed notices (`catalog.hosting.history`). No counts, exact
  rates or durations, and never the portfolio (operator decisions 2026-09-16 and 2026-09-17: the
  earlier takedown-chance and -speed bands read as predictions and are internal now).
- **Hostnames, never media.** The content hosts are derived from media URLs in
  `marceline.hosting`, which hands over hostnames and nothing else of them (#53).
"""

from collections.abc import Iterable
from datetime import datetime

from django.conf import settings

from catalog.hosting.history import ResponseHistory, customer_history
from catalog.hosting.jurisdiction import jurisdiction_of
from catalog.hosting.services import history_by_provider, is_stale, normalize_hostname
from catalog.models import HostingRecord
from index.models import Page
from marceline.app.safety import page_visible
from marceline.app.schemas import (
    CustomerFindingHostingOut,
    CustomerHostOut,
    CustomerResponseHistoryOut,
    HostAbuseContactOut,
    HostRegistrarOut,
)
from marceline.hosting import content_hosts_by_page


def _or_none(value: str) -> str | None:
    return value or None


def response_history_out(history: ResponseHistory | None) -> CustomerResponseHistoryOut | None:
    """Rounded and bucketed, or None ("no response history yet") below the minimum sample."""
    shown = customer_history(history or ResponseHistory(), settings.HOSTING_HISTORY_MIN_NOTICES)
    if shown is None:
        return None
    return CustomerResponseHistoryOut(
        removed_percent=shown.removed_percent, usual_removal_time=shown.usual_removal_time
    )


def host_out(
    hostname: str,
    record: HostingRecord | None,
    histories: dict[int, ResponseHistory],
    now: datetime,
) -> CustomerHostOut:
    if record is None:
        return CustomerHostOut(
            hostname=hostname,
            lookup="not_looked_up",
            looked_up_at=None,
            stale=False,
            addresses=[],
            asn=None,
            as_name=None,
            provider=None,
            country=None,
            jurisdiction="unknown",
            cdn=None,
            abuse_contact=None,
            registrar=None,
            response_history=None,
        )
    return CustomerHostOut(
        hostname=hostname,
        lookup=record.status,  # type: ignore[arg-type]
        looked_up_at=record.looked_up_at,
        stale=is_stale(record, now),
        addresses=[str(address) for address in record.addresses],
        asn=record.asn,
        as_name=_or_none(record.as_name),
        provider=_or_none(record.organisation),
        country=_or_none(record.country),
        jurisdiction=jurisdiction_of(record.country),
        cdn=_or_none(record.cdn),
        abuse_contact=(
            HostAbuseContactOut(
                email=record.abuse_email,
                source=record.abuse_source,  # type: ignore[arg-type]
                source_url=record.abuse_source_url,
            )
            if record.abuse_email and record.abuse_source
            else None
        ),
        registrar=(
            HostRegistrarOut(
                name=record.registrar_name,
                abuse_email=_or_none(record.registrar_abuse_email),
                source_url=record.registrar_source_url,
            )
            if record.registrar_name
            else None
        ),
        response_history=(
            response_history_out(histories.get(record.provider_id))
            if record.provider_id is not None
            else None
        ),
    )


def hosting_by_page(pages: Iterable[Page], now: datetime) -> dict[int, CustomerFindingHostingOut]:
    """The hosting section of every visible page, in a few queries however many pages."""
    visible = {page.pk: page for page in pages if page_visible(page)}
    content = content_hosts_by_page(visible.values())
    page_hosts = {
        page_id: normalize_hostname(page.site.domain) for page_id, page in visible.items()
    }
    names = set(page_hosts.values()) | {name for hosts in content.values() for name in hosts}
    records = {
        record.hostname: record for record in HostingRecord.objects.filter(hostname__in=names)
    }
    provider_ids = {
        record.provider_id for record in records.values() if record.provider_id is not None
    }
    histories = history_by_provider(provider_ids, now)

    def out(hostname: str) -> CustomerHostOut:
        return host_out(hostname, records.get(hostname), histories, now)

    return {
        page_id: CustomerFindingHostingOut(
            page_host=out(page_hosts[page_id]),
            content_hosts=[out(name) for name in content.get(page_id, [])],
        )
        for page_id in visible
    }
