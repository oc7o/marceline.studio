"""Hosting records in the database: refresh policy, storing a lookup, cooperation summaries."""

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q

from catalog.hosting.cooperation import Cooperation
from catalog.hosting.history import ResponseHistory, response_history
from catalog.hosting.lookup import HostFacts
from catalog.hosting.speed import NoticeTimes, SpeedStats, speed_stats
from catalog.models import HosterContact, HostingProvider, HostingRecord

#: A failed lookup (the name did not resolve, a registry was down) is retried sooner than a
#: successful one is refreshed: tomorrow's answer may differ, next month's hardly. A host that has
#: never once succeeded backs off further (`failed_retry_after`).
FAILED_RETRY_AFTER = timedelta(days=1)
#: The error code of a lookup that raised instead of returning facts (a registry answer nobody
#: anticipated). The run records it and moves on to the next host.
LOOKUP_ERROR = "lookup_error"


def refresh_after() -> timedelta:
    return timedelta(days=settings.HOSTING_REFRESH_DAYS)


def normalize_hostname(value: str) -> str:
    """Lower case, no trailing dot, IDNA-encoded. "" for anything that is not a hostname."""
    host = value.strip().rstrip(".").lower()
    if not host or len(host) > 253 or any(char in host for char in "/:@ ?#"):
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def is_stale(record: HostingRecord, now: datetime, *, max_age: timedelta | None = None) -> bool:
    """Older than the refresh window: still shown, flagged so nobody mistakes it for current."""
    return now - record.looked_up_at >= (max_age if max_age is not None else refresh_after())


def failed_retry_after(record: HostingRecord, *, max_age: timedelta | None = None) -> timedelta:
    """How long a failed record waits before the next attempt.

    One day, as long as the host ever gave an answer. A host that has failed on every attempt since
    its record was created waits as long as it has been failing — one day, one, two, four… — up
    to the refresh window, so a name that is simply broken stops costing registry requests daily.
    """
    delay = FAILED_RETRY_AFTER
    never_succeeded = not record.addresses and not record.registrar_name
    if never_succeeded and record.created_at is not None:
        delay = max(delay, record.looked_up_at - record.created_at)
    return min(delay, max_age or refresh_after())


def needs_lookup(
    record: HostingRecord | None, now: datetime, *, max_age: timedelta | None = None
) -> bool:
    """Never looked up, past the refresh window, or failed longer ago than the retry delay."""
    if record is None:
        return True
    if record.status == HostingRecord.Status.FAILED:
        return now - record.looked_up_at >= failed_retry_after(record, max_age=max_age)
    return is_stale(record, now, max_age=max_age)


def due_hostnames(hostnames: Sequence[str], now: datetime, *, refresh: bool = False) -> list[str]:
    """`hostnames` in order, keeping only those whose record needs a lookup (all with refresh).

    Hosts whose last attempt failed go to the back, in their original order: a host that fails
    every time must not take the first slot of every `--limit`ed run.
    """
    if refresh:
        return list(hostnames)
    records = {
        record.hostname: record for record in HostingRecord.objects.filter(hostname__in=hostnames)
    }
    due = [hostname for hostname in hostnames if needs_lookup(records.get(hostname), now)]
    return sorted(due, key=lambda hostname: _failed(records.get(hostname)))


def _failed(record: HostingRecord | None) -> bool:
    return record is not None and record.status == HostingRecord.Status.FAILED


def failed_facts(hostname: str, code: str = LOOKUP_ERROR) -> HostFacts:
    """A lookup that produced nothing: stored as `failed`, keeping what an earlier success found."""
    return HostFacts(
        hostname=hostname,
        addresses=(),
        address=None,
        origin=None,
        network=None,
        domain=None,
        registrar=None,
        cdn=None,
        errors=(code,),
    )


@transaction.atomic
def store_facts(facts: HostFacts, now: datetime) -> HostingRecord:
    """Write one lookup. A failed attempt keeps what an earlier success found and says it failed."""
    existing = HostingRecord.objects.filter(hostname=facts.hostname).first()
    if facts.status == HostingRecord.Status.FAILED and existing is not None:
        existing.status = HostingRecord.Status.FAILED
        existing.errors = list(facts.errors)
        existing.looked_up_at = now
        existing.save(update_fields=["status", "errors", "looked_up_at", "updated_at"])
        return existing
    origin, network, registrar = facts.origin, facts.network, facts.registrar
    provider = None
    if origin is not None:
        provider, _ = HostingProvider.objects.update_or_create(
            asn=origin.asn,
            defaults={"name": origin.as_name[:255], "country": origin.country[:2]},
        )
    email, source, source_url = facts.abuse
    record, _ = HostingRecord.objects.update_or_create(
        hostname=facts.hostname,
        defaults={
            "status": facts.status,
            "errors": list(facts.errors),
            "addresses": list(facts.addresses),
            "address": facts.address,
            "asn": origin.asn if origin is not None else None,
            "as_name": origin.as_name[:255] if origin is not None else "",
            "provider": provider,
            "network_name": network.name if network is not None else "",
            "network_handle": network.handle if network is not None else "",
            "organisation": network.organisation if network is not None else "",
            "country": facts.country,
            "cdn": facts.cdn or "",
            "abuse_email": email,
            "abuse_source": source,
            "abuse_source_url": source_url,
            "registrable_domain": facts.domain or "",
            "registrar_name": registrar.name if registrar is not None else "",
            "registrar_iana_id": registrar.iana_id if registrar is not None else "",
            "registrar_abuse_email": registrar.abuse_email if registrar is not None else "",
            "registrar_source_url": registrar.source_url if registrar is not None else "",
            "looked_up_at": now,
        },
    )
    return record


def cooperation_by_provider(provider_ids: Iterable[int]) -> dict[int, Cooperation]:
    """Internal outcome counts per provider id; a provider with no contacts is absent.

    Never serialise these for a customer: they see `history.customer_history` only.
    """
    rows = (
        HosterContact.objects.filter(provider_id__in=set(provider_ids))
        .values("provider_id")
        .annotate(
            sent=Count("pk"),
            decided=Count("pk", filter=~Q(outcome=HosterContact.Outcome.AWAITING)),
            removed=Count("pk", filter=Q(outcome=HosterContact.Outcome.REMOVED)),
            forwarded=Count("pk", filter=Q(outcome=HosterContact.Outcome.FORWARDED)),
        )
    )
    return {
        row["provider_id"]: Cooperation(
            notices_sent=row["sent"],
            decided=row["decided"],
            removed=row["removed"],
            forwarded=row["forwarded"],
        )
        for row in rows
    }


def notices_by_provider(provider_ids: Iterable[int]) -> dict[int, list[NoticeTimes]]:
    """What the reaction statistics need of every notice, per provider id; no contacts: absent."""
    notices: dict[int, list[NoticeTimes]] = {}
    rows = HosterContact.objects.filter(provider_id__in=set(provider_ids)).values_list(
        "provider_id", "sent_at", "responded_at", "removed_observed_at", "outcome"
    )
    for provider_id, sent_at, responded_at, removed_observed_at, outcome in rows:
        notices.setdefault(provider_id, []).append(
            NoticeTimes(sent_at, responded_at, removed_observed_at, outcome)
        )
    return notices


def speed_by_provider(provider_ids: Iterable[int], now: datetime) -> dict[int, SpeedStats]:
    """Internal reaction statistics per provider id; a provider with no contacts is absent.

    Never serialise these for a customer: they see `history.customer_history` only.
    """
    return {
        provider_id: speed_stats(times, now)
        for provider_id, times in notices_by_provider(provider_ids).items()
    }


def history_by_provider(provider_ids: Iterable[int], now: datetime) -> dict[int, ResponseHistory]:
    """Internal response history per provider id; a provider with no contacts is absent.

    Exact counts: a customer sees only `history.customer_history` of it, rounded and bucketed.
    """
    return {
        provider_id: response_history(times, now)
        for provider_id, times in notices_by_provider(provider_ids).items()
    }
