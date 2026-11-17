"""Staff report over the provider directory (ADR 0027 amendment 2026-09-17). Writes nothing.

For the hosts of customer findings (or named hosts): every provider role with its nexus, function
and suggested notice channel, the channels due for re-checking, and the roles the directory does
not cover yet — the list of what staff should fill in next. Makes no request.

It lives in the project package because "hosts of customer findings" needs `marceline.hosting`.
"""

from collections.abc import Sequence
from datetime import datetime

from catalog.hosting.directory import ProviderRole, due_channels, provider_roles, recheck_days
from catalog.hosting.services import normalize_hostname
from catalog.models import ComplaintChannel, DirectoryProvider, HostingRecord, Site
from marceline.hosting import customer_hostnames


def role_line(role: ProviderRole) -> str:
    parts = [
        f"{role.slot}: {role.provider_name or '-'}",
        f"country {role.country or '-'}",
        f"nexus {role.nexus} ({role.nexus_source})",
        f"function {role.function} ({role.function_source})",
    ]
    suggested = role.suggested
    if suggested is None:
        parts.append("suggested channel: none known")
    else:
        due = ", re-check due" if suggested.recheck_due else ""
        parts.append(
            f"suggested channel: {suggested.kind} {suggested.email or suggested.url} "
            f"[{suggested.support}, from {suggested.source}, "
            f"transfer {suggested.transfer_basis}{due}]"
        )
    return " | ".join(parts)


def report_lines(
    now: datetime, *, hostnames: Sequence[str] | None = None, due_only: bool = False
) -> list[str]:
    due = due_channels(now)
    lines = [
        f"provider directory: {DirectoryProvider.objects.count()} provider(s), "
        f"{ComplaintChannel.objects.filter(superseded_at__isnull=True).count()} current "
        f"channel(s), {len(due)} due for re-check (verified more than {recheck_days()} days ago)"
    ]
    lines.extend(
        f"  due: {channel.provider.name} {channel.kind} checked {channel.checked_at:%Y-%m-%d} "
        f"(channel {channel.pk})"
        for channel in due
    )
    if due_only:
        return lines
    named = hostnames is not None
    candidates = (
        [normalize_hostname(host) for host in hostnames]
        if hostnames is not None
        else customer_hostnames()
    )
    names = list(dict.fromkeys(name for name in candidates if name))
    site_hosts = {
        normalize_hostname(domain) for domain in Site.objects.values_list("domain", flat=True)
    }
    looked_up = set(
        HostingRecord.objects.filter(hostname__in=names).values_list("hostname", flat=True)
    )
    roles = provider_roles(names, now)
    lines.append(f"{'named hosts' if named else 'hosts of customer findings'}: {len(names)}")
    gaps: list[str] = []
    for name in names:
        position = "page host" if name in site_hosts else "content host"
        lines.append(f"  {name} ({position})")
        if name not in looked_up:
            lines.append("    no hosting record yet (lookup_hosting)")
        for role in roles[name]:
            lines.append(f"    {role_line(role)}")
            if role.directory_provider_id is None:
                gaps.append(f"{role.provider_name or name} ({role.slot})")
            elif not role.channels:
                gaps.append(f"{role.provider_name} ({role.slot}, no channel)")
    unique_gaps = list(dict.fromkeys(gaps))
    lines.append(f"not covered by the directory: {len(unique_gaps)}")
    lines.extend(f"  {gap}" for gap in unique_gaps)
    return lines
