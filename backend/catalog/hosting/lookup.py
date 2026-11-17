"""Everything we can find out about one hostname, without asking the hostname anything.

`lookup_host` makes, at most: one resolver call, two DNS TXT queries (origin AS, AS name), one
RDAP IP-network lookup for the first address, and one RDAP domain lookup for the registrable
domain. Each part fails on its own and leaves an error code; a partial record is still a record.
No database access here — `services.store_facts` persists the result.
"""

from collections.abc import Callable
from dataclasses import dataclass

from catalog.hosting.cdn import detect_cdn
from catalog.hosting.dns import DnsError, Origin, Resolver, TxtLookup, lookup_origin, query_txt
from catalog.hosting.dns import resolve_addresses as system_resolve
from catalog.hosting.rdap import (
    NetworkFacts,
    RdapClient,
    RdapError,
    RegistrarFacts,
    registrable_domain,
)

OriginLookup = Callable[[str, TxtLookup], Origin | None]


@dataclass(frozen=True)
class HostFacts:
    hostname: str
    addresses: tuple[str, ...]
    address: str | None
    origin: Origin | None
    network: NetworkFacts | None
    domain: str | None
    registrar: RegistrarFacts | None
    cdn: str | None
    errors: tuple[str, ...]

    @property
    def country(self) -> str:
        """The network's own country, else the allocation country from the origin lookup."""
        if self.network is not None and self.network.country:
            return self.network.country
        return (
            self.origin.country if self.origin is not None and len(self.origin.country) == 2 else ""
        )

    @property
    def abuse(self) -> tuple[str, str, str]:
        """(email, source, source URL): the network's abuse role first, then the registrar's."""
        if self.network is not None and self.network.abuse_email:
            return self.network.abuse_email, "ip_network", self.network.source_url
        if self.registrar is not None and self.registrar.abuse_email:
            return self.registrar.abuse_email, "registrar", self.registrar.source_url
        return "", "", ""

    @property
    def status(self) -> str:
        if not self.addresses and self.registrar is None:
            return "failed"
        return "partial" if self.errors else "complete"


def lookup_host(
    hostname: str,
    *,
    rdap: RdapClient,
    resolve: Resolver = system_resolve,
    txt: TxtLookup = query_txt,
    origin_lookup: OriginLookup = lookup_origin,
) -> HostFacts:
    errors: list[str] = []
    addresses = tuple(resolve(hostname))
    address = addresses[0] if addresses else None
    origin: Origin | None = None
    network: NetworkFacts | None = None
    if address is None:
        errors.append("dns_no_address")
    else:
        try:
            origin = origin_lookup(address, txt)
            if origin is None:
                errors.append("asn_not_announced")
        except DnsError:
            errors.append("asn_lookup_failed")
        try:
            network = rdap.ip_network(address)
            if not network.abuse_email:
                errors.append("ip_network_no_abuse_contact")
        except RdapError as exc:
            errors.append(f"ip_{exc.code}")
    domain = registrable_domain(hostname)
    registrar: RegistrarFacts | None = None
    if domain is not None:
        try:
            registrar = rdap.domain(domain)
            if registrar is None:
                errors.append("domain_no_registrar")
        except RdapError as exc:
            errors.append(f"domain_{exc.code}")
    return HostFacts(
        hostname=hostname,
        addresses=addresses,
        address=address,
        origin=origin,
        network=network,
        domain=domain,
        registrar=registrar,
        cdn=detect_cdn(origin.asn if origin is not None else None, address),
        errors=tuple(errors),
    )
