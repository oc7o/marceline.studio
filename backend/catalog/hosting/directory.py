"""The provider directory in the database: channel versions, and the providers of a hostname.

`provider_roles` answers, for hosts attached to findings: which providers are involved (the
network serving the host, the registrar of its domain, and a service staff linked to the hostname),
where each faces, what it does in that role, its complaint channels with their v1 support, and the
channel to start from. It reads hosting records, the directory and staff overrides; it makes no
request. The policy is `catalog.hosting.providers` (ADR 0027 amendment 2026-09-17).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from catalog.hosting.providers import (
    ChannelChoice,
    FunctionSource,
    Nexus,
    NexusSource,
    ProviderFunction,
    Slot,
    V1Support,
    derive_function,
    hostname_suffixes,
    needs_recheck,
    resolve_nexus,
    suggest_channel,
    v1_support,
)
from catalog.models import (
    ComplaintChannel,
    DirectoryProvider,
    HostingRecord,
    ProviderFunctionOverride,
    ProviderIdentifier,
)


def recheck_days() -> int:
    return int(settings.PROVIDER_CHANNEL_RECHECK_DAYS)


def current_channels() -> Q:
    return Q(superseded_at__isnull=True)


@transaction.atomic
def supersede_channel(previous_pk: int, replacement: ComplaintChannel, now: datetime) -> None:
    """Store `replacement` as the new current version of channel `previous_pk`, in place.

    `replacement` is an unsaved edit of the previous row (the admin hands over the changed
    instance): it gets a new primary key, and the previous row is stamped superseded — never
    edited otherwise. Superseding a version that is no longer current is refused.
    """
    previous = ComplaintChannel.objects.select_for_update().get(pk=previous_pk)
    if previous.superseded_at is not None:
        raise ValueError(f"channel {previous_pk} was already superseded")
    replacement.pk = None
    replacement._state.adding = True
    replacement.superseded_at = None
    replacement.superseded_by = None
    replacement.save()
    previous.superseded_at = now
    previous.superseded_by = replacement
    previous.save(update_fields=["superseded_at", "superseded_by"])


def mark_rechecked(channel: ComplaintChannel, now: datetime) -> ComplaintChannel:
    """A person verified the channel and nothing changed: a new version checked `now`."""
    replacement = ComplaintChannel.objects.get(pk=channel.pk)
    replacement.checked_at = now
    supersede_channel(channel.pk, replacement, now)
    return replacement


def retire_channel(channel: ComplaintChannel, now: datetime) -> None:
    """The channel no longer exists: stamped superseded, with no replacement."""
    ComplaintChannel.objects.filter(pk=channel.pk, superseded_at__isnull=True).update(
        superseded_at=now
    )


def due_channels(now: datetime) -> list[ComplaintChannel]:
    """Current channels a person verified longer ago than the re-check window, oldest first."""
    channels = ComplaintChannel.objects.filter(current_channels()).select_related("provider")
    return sorted(
        (channel for channel in channels if needs_recheck(channel.checked_at, now, recheck_days())),
        key=lambda channel: channel.checked_at,
    )


@dataclass(frozen=True)
class ChannelView:
    """One complaint channel of a provider role, from the directory or the RDAP fallback."""

    kind: str
    support: V1Support
    url: str
    email: str
    languages: str
    forwards_to_site_operator: bool | None
    publishes_to_transparency_db: bool | None
    transfer_basis: str
    source: str  # "directory" or "rdap"
    source_url: str
    checked_at: datetime | None
    recheck_due: bool
    channel_id: int | None


@dataclass(frozen=True)
class ProviderRole:
    """One provider involved with one hostname, in one role (`slot`)."""

    hostname: str
    slot: Slot
    provider_name: str
    directory_provider_id: int | None
    country: str
    nexus: Nexus
    nexus_source: NexusSource
    function: ProviderFunction
    function_source: FunctionSource
    function_note: str
    channels: tuple[ChannelView, ...]
    suggested: ChannelView | None


@dataclass
class _Directory:
    by_asn: dict[str, DirectoryProvider]
    by_registrar: dict[str, DirectoryProvider]
    by_hostname: dict[str, DirectoryProvider]
    channels: dict[int, list[ComplaintChannel]]
    overrides: dict[tuple[str, str], ProviderFunctionOverride]


def _load(hostnames: Sequence[str], records: Iterable[HostingRecord]) -> _Directory:
    records = list(records)
    asns = {str(record.asn) for record in records if record.asn is not None}
    registrars = {record.registrar_iana_id for record in records if record.registrar_iana_id}
    suffixes = {suffix for name in hostnames for suffix in hostname_suffixes(name)}
    identifiers = ProviderIdentifier.objects.filter(
        Q(kind=ProviderIdentifier.Kind.ASN, value__in=asns)
        | Q(kind=ProviderIdentifier.Kind.REGISTRAR_IANA_ID, value__in=registrars)
        | Q(kind=ProviderIdentifier.Kind.HOSTNAME, value__in=suffixes)
    ).select_related("provider")
    directory = _Directory({}, {}, {}, {}, {})
    target = {
        ProviderIdentifier.Kind.ASN: directory.by_asn,
        ProviderIdentifier.Kind.REGISTRAR_IANA_ID: directory.by_registrar,
        ProviderIdentifier.Kind.HOSTNAME: directory.by_hostname,
    }
    for identifier in identifiers:
        target[ProviderIdentifier.Kind(identifier.kind)][identifier.value] = identifier.provider
    provider_ids = {
        provider.pk
        for mapping in (directory.by_asn, directory.by_registrar, directory.by_hostname)
        for provider in mapping.values()
    }
    for channel in ComplaintChannel.objects.filter(
        current_channels(), provider_id__in=provider_ids
    ).order_by("pk"):
        directory.channels.setdefault(channel.provider_id, []).append(channel)
    for override in ProviderFunctionOverride.objects.filter(hostname__in=list(hostnames)):
        directory.overrides[(override.hostname, override.slot)] = override
    return directory


def provider_roles(hostnames: Sequence[str], now: datetime) -> dict[str, list[ProviderRole]]:
    """Per hostname: its service (if staff linked one), network and registrar roles.

    A hostname without a hosting record still gets its service role if one is linked; network and
    registrar roles need a record (an AS, a registrar) to exist.
    """
    records = {
        record.hostname: record for record in HostingRecord.objects.filter(hostname__in=hostnames)
    }
    directory = _load(hostnames, records.values())
    return {
        hostname: _roles(hostname, records.get(hostname), directory, now) for hostname in hostnames
    }


def _service_provider(hostname: str, directory: _Directory) -> DirectoryProvider | None:
    for suffix in hostname_suffixes(hostname):  # most specific first
        if suffix in directory.by_hostname:
            return directory.by_hostname[suffix]
    return None


def _roles(
    hostname: str, record: HostingRecord | None, directory: _Directory, now: datetime
) -> list[ProviderRole]:
    roles: list[ProviderRole] = []
    service = _service_provider(hostname, directory)
    if service is not None:
        roles.append(_role(hostname, "service", service, "", service.name, "", [], directory, now))
    if record is None:
        return roles
    if record.asn is not None:
        network = directory.by_asn.get(str(record.asn))
        rdap = (
            [_rdap_channel(record.abuse_email, record.abuse_source_url, record.looked_up_at)]
            if record.abuse_email and record.abuse_source == HostingRecord.AbuseSource.IP_NETWORK
            else []
        )
        name = network.name if network else (record.organisation or record.as_name)
        roles.append(
            _role(
                hostname, "network", network, record.cdn, name, record.country, rdap, directory, now
            )
        )
    if record.registrar_name:
        registrar = directory.by_registrar.get(record.registrar_iana_id)
        rdap = (
            [
                _rdap_channel(
                    record.registrar_abuse_email, record.registrar_source_url, record.looked_up_at
                )
            ]
            if record.registrar_abuse_email
            else []
        )
        name = registrar.name if registrar else record.registrar_name
        roles.append(_role(hostname, "registrar", registrar, "", name, "", rdap, directory, now))
    return roles


def _rdap_channel(email: str, source_url: str, looked_up_at: datetime) -> ComplaintChannel:
    """The RDAP abuse contact as an unsaved channel: the fallback when the directory has none."""
    return ComplaintChannel(
        kind=ComplaintChannel.Kind.ABUSE_EMAIL,
        email=email,
        source_url=source_url,
        checked_at=looked_up_at,
    )


def _role(
    hostname: str,
    slot: Slot,
    provider: DirectoryProvider | None,
    cdn: str,
    name: str,
    allocation_country: str,
    fallback: list[ComplaintChannel],
    directory: _Directory,
    now: datetime,
) -> ProviderRole:
    override = directory.overrides.get((hostname, slot))
    function, function_source = derive_function(
        slot,
        cdn=cdn,
        override=override.function if override else "",
        directory_default=provider.function if provider else "",
    )
    nexus, nexus_source = resolve_nexus(
        staff_nexus=provider.nexus if provider else "",
        establishment_country=provider.establishment_country if provider else "",
        allocation_country=allocation_country,
    )
    stored = directory.channels.get(provider.pk, []) if provider else []
    # The RDAP contact is only the fallback: once staff recorded channels, theirs are the truth.
    channels = tuple(
        _view(channel, function, now, from_directory=bool(stored))
        for channel in (stored or fallback)
    )
    choice = suggest_channel(
        [
            ChannelChoice(view.kind, view.support, view.source == "directory", view.recheck_due)
            for view in channels
        ],
        nexus,
    )
    country = allocation_country if slot == "network" else ""
    if not country and provider is not None:
        country = provider.establishment_country
    return ProviderRole(
        hostname=hostname,
        slot=slot,
        provider_name=name,
        directory_provider_id=provider.pk if provider else None,
        country=country,
        nexus=nexus,
        nexus_source=nexus_source,
        function=function,
        function_source=function_source,
        function_note=override.note if override else "",
        channels=channels,
        suggested=channels[choice] if choice is not None else None,
    )


def _view(
    channel: ComplaintChannel, function: str, now: datetime, *, from_directory: bool
) -> ChannelView:
    return ChannelView(
        kind=channel.kind,
        support=v1_support(function, channel.kind),
        url=channel.url,
        email=channel.email,
        languages=channel.languages,
        forwards_to_site_operator=channel.forwards_to_site_operator,
        publishes_to_transparency_db=channel.publishes_to_transparency_db,
        transfer_basis=channel.transfer_basis,
        source="directory" if from_directory else "rdap",
        source_url=channel.source_url,
        checked_at=channel.checked_at,
        # The RDAP fallback is refreshed by `lookup_hosting`, not re-checked by a person.
        recheck_due=from_directory and needs_recheck(channel.checked_at, now, recheck_days()),
        channel_id=channel.pk if from_directory else None,
    )
