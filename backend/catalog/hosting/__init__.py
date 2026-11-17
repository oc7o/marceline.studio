"""Hoster intelligence (ADR 0027): who hosts a hostname, whom to complain to, which regime applies.

Sources are DNS and the RDAP registries only — **never the host itself**. Nothing in this package
requests a piracy page or its media: it asks the system resolver for addresses, Team Cymru's DNS
service for the origin AS, and the RDAP server IANA's bootstrap names for the network and the
domain.

- `jurisdiction` — country → `eu` / `us` / `other` / `unknown` (pure).
- `cdn` — CDN/proxy in front, by ASN or published ranges (pure).
- `dns` — address resolution and the origin-AS TXT lookup.
- `rdap` — IANA bootstrap, polite RDAP fetches, abuse-contact and registrar extraction.
- `lookup` — one hostname's facts from the three above; no database.
- `services` — refresh policy, storing records, cooperation summaries.
- `providers` — provider function, location/nexus, v1 support of a channel, suggestion (pure).
- `directory` — the staff-maintained provider directory: channel versions, roles of a host.
"""
