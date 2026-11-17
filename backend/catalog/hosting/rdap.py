"""RDAP (RFC 9082/9083): which registry answers, and what it says about a network or a domain.

The registry for an address or a TLD comes from IANA's bootstrap files
(https://data.iana.org/rdap/, RFC 9224), fetched once and cached for a day per client. Requests go
through `core.http.PoliteClient` — the crawler's User-Agent, a per-host delay, retries on 429/5xx —
one at a time. Only registries are ever asked; the host being looked up never is.

Parsing is pure and works on the JSON as registries really send it:

- the **abuse contact** of a network is an entity with the role `abuse` — at the top level (RIPE,
  ARIN) or nested inside the registrant organisation (ARIN) — whose vCard carries an email;
- the **organisation** is the registrant entity of kind `org`, wherever it is nested (RIPE puts a
  maintainer at the top level and the organisation under the abuse role);
- the **registrar** of a domain is the entity with the role `registrar`, and its abuse contact is
  the `abuse` entity nested inside it (ICANN RDAP profile).

`source_url` is the response's own `self` link, else the URL that was asked: where the contact came
from, so a notice can cite it.

A registry answer is **untrusted input**. Parsing never assumes a type: a member of the wrong
type counts as absent, entity walks stop after `MAX_ENTITIES` or `MAX_ENTITY_DEPTH`, JSON nested
deeper than the decoder can take is `rdap_invalid_json`, and only values that look right are kept:
an `https://` link without credentials (`safe_link`) and a plain `local@domain` address
(`safe_email`). Anything else is dropped, never stored — these values end up as links and `mailto:`
targets in front of people.
"""

import ipaddress
import json
import re
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.http import PoliteClient, ResponseTooLargeError

IANA_BOOTSTRAP = "https://data.iana.org/rdap/"
BOOTSTRAP_TTL_S = 24 * 60 * 60
#: Seconds between two requests to the same registry host.
RDAP_DELAY_S = 1.0
RDAP_MAX_BYTES = 2 * 1024 * 1024
BOOTSTRAP_MAX_BYTES = 1024 * 1024
#: Second-level suffixes under which a registrable domain has three labels. Deliberately small and
#: offline, like `crawler.discovery.domains` (which `catalog` may not import).
MULTIPART_SUFFIXES = frozenset({"co.uk", "com.au", "co.nz", "co.jp", "com.br", "co.in"})
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
#: How many entities one response may contribute, and how deep they may nest. Real answers have a
#: handful at depth two or three; the caps only keep a hostile one from costing time and memory.
MAX_ENTITIES = 200
MAX_ENTITY_DEPTH = 8
MAX_EMAIL_LENGTH = 254
MAX_URL_LENGTH = 2000
#: `local@domain` with a dotted, LDH domain. Deliberately narrower than RFC 5322: an abuse desk
#: address that fails this is rare, and a value that passes can safely become a `mailto:` target.
EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)

Json = dict[str, Any]


class RdapError(Exception):
    """An RDAP lookup gave no usable answer. `code` is a machine-readable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class NetworkFacts:
    handle: str
    name: str
    country: str
    organisation: str
    abuse_email: str
    source_url: str


@dataclass(frozen=True)
class RegistrarFacts:
    name: str
    iana_id: str
    abuse_email: str
    source_url: str


# --- bootstrap ---------------------------------------------------------------------------------


def _services(bootstrap: Json) -> Iterator[tuple[list[str], list[str]]]:
    for service in _list(bootstrap.get("services")):
        if isinstance(service, list) and len(service) >= 2:
            keys, urls = service[0], service[1]
            if isinstance(keys, list) and isinstance(urls, list):
                yield [str(key) for key in keys], [str(url) for url in urls]


def _preferred(urls: list[str]) -> str | None:
    """HTTPS first, as RFC 9224 recommends; base URLs always end in a slash."""
    ordered = sorted(urls, key=lambda url: not url.startswith("https://"))
    return (ordered[0].rstrip("/") + "/") if ordered else None


def ip_base_url(bootstrap: Json, address: str) -> str | None:
    """The RDAP base URL for `address`: the longest matching prefix wins."""
    ip = ipaddress.ip_address(address)
    best: tuple[int, str] | None = None
    for prefixes, urls in _services(bootstrap):
        for prefix in prefixes:
            try:
                network = ipaddress.ip_network(prefix, strict=False)
            except ValueError:
                continue
            if network.version != ip.version or ip not in network:
                continue
            url = _preferred(urls)
            if url and (best is None or network.prefixlen > best[0]):
                best = (network.prefixlen, url)
    return best[1] if best else None


def domain_base_url(bootstrap: Json, domain: str) -> str | None:
    """The RDAP base URL for `domain`: the longest matching label suffix wins."""
    labels = domain.lower().rstrip(".").split(".")
    best: tuple[int, str] | None = None
    for suffixes, urls in _services(bootstrap):
        for suffix in suffixes:
            parts = suffix.lower().strip(".").split(".")
            if labels[-len(parts) :] != parts:
                continue
            url = _preferred(urls)
            if url and (best is None or len(parts) > best[0]):
                best = (len(parts), url)
    return best[1] if best else None


def registrable_domain(hostname: str) -> str | None:
    """`cdn1.example.com` → `example.com`; None for addresses and malformed names."""
    host = hostname.strip().rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or not all(LABEL.match(label) for label in labels):
        return None
    count = 3 if ".".join(labels[-2:]) in MULTIPART_SUFFIXES else 2
    if len(labels) < count:
        return None
    return ".".join(labels[-count:])


# --- parsing -----------------------------------------------------------------------------------


def _list(value: Any) -> list[Any]:
    """`value` if it is a JSON array, else empty: a member of the wrong type counts as absent."""
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    """`value` if it is a JSON string, else "". Numbers, objects and arrays are not names."""
    return value.strip() if isinstance(value, str) else ""


def safe_link(value: Any) -> str:
    """`value` if it is a plain `https://` URL with a host and no credentials or spaces, else ""."""
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        return ""
    if any(char.isspace() or ord(char) < 0x20 or char in '<>"\\' for char in value):
        return ""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        return ""
    if parts.scheme != "https" or not hostname or "@" in parts.netloc:
        return ""
    return value


def safe_email(value: Any) -> str:
    """A plain `local@domain` address (a `mailto:` prefix removed), else ""."""
    if not isinstance(value, str):
        return ""
    address = value.strip()
    if address[:7].lower() == "mailto:":
        address = address[7:]
    if len(address) > MAX_EMAIL_LENGTH or not EMAIL.fullmatch(address):
        return ""
    return address


def _entities(node: Mapping[str, Any]) -> list[Json]:
    return [entity for entity in _list(node.get("entities")) if isinstance(entity, dict)]


def walk_entities(node: Mapping[str, Any]) -> Iterator[Json]:
    """Every entity under `node`, breadth first: a top-level role outranks a nested one.

    At most `MAX_ENTITIES` entities, none deeper than `MAX_ENTITY_DEPTH`.
    """
    queue: deque[tuple[int, Json]] = deque((1, entity) for entity in _entities(node))
    seen = 0
    while queue and seen < MAX_ENTITIES:
        depth, entity = queue.popleft()
        seen += 1
        yield entity
        if depth < MAX_ENTITY_DEPTH:
            children = _entities(entity)[: max(MAX_ENTITIES - seen - len(queue), 0)]
            queue.extend((depth + 1, child) for child in children)


def roles(entity: Mapping[str, Any]) -> set[str]:
    return {role.lower() for role in _list(entity.get("roles")) if isinstance(role, str)}


def vcard(entity: Mapping[str, Any], prop: str) -> str:
    """The first text value of a vCard property (jCard, RFC 7095), or ""."""
    card = entity.get("vcardArray")
    if not isinstance(card, list) or len(card) < 2 or not isinstance(card[1], list):
        return ""
    for item in card[1]:
        if isinstance(item, list) and len(item) >= 4 and _text(item[0]).lower() == prop:
            value = item[3]
            if isinstance(value, list):
                value = next((part for part in value if isinstance(part, str) and part), "")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _email(entity: Mapping[str, Any]) -> str:
    return safe_email(vcard(entity, "email"))


def abuse_email(node: Mapping[str, Any]) -> str:
    """The email of the nearest `abuse` entity under `node`, or ""."""
    for entity in walk_entities(node):
        if "abuse" in roles(entity) and (email := _email(entity)):
            return email
    return ""


def self_link(node: Mapping[str, Any], fallback: str) -> str:
    """The first valid `https://` self link, else `fallback` if that is one, else ""."""
    for link in _list(node.get("links")):
        if (
            isinstance(link, dict)
            and link.get("rel") == "self"
            and (href := safe_link(link.get("href")))
        ):
            return href
    return safe_link(fallback)


def organisation_name(node: Mapping[str, Any]) -> str:
    """The registrant organisation, preferring an entity whose vCard kind is `org`."""
    registrants = [entity for entity in walk_entities(node) if "registrant" in roles(entity)]
    for entity in registrants:
        if vcard(entity, "kind").lower() == "org" and (name := vcard(entity, "fn")):
            return name
    return next((name for entity in registrants if (name := vcard(entity, "fn"))), "")


def parse_ip_network(data: Mapping[str, Any], url: str) -> NetworkFacts:
    country = _text(data.get("country")).upper()
    return NetworkFacts(
        handle=_text(data.get("handle"))[:255],
        name=_text(data.get("name"))[:255],
        country=country if len(country) == 2 and country.isascii() and country.isalpha() else "",
        organisation=organisation_name(data)[:255],
        abuse_email=abuse_email(data),
        source_url=self_link(data, url),
    )


def parse_domain(data: Mapping[str, Any], url: str) -> RegistrarFacts | None:
    """The registrar and its abuse contact; None when the registry names no registrar."""
    registrar = next(
        (entity for entity in walk_entities(data) if "registrar" in roles(entity)), None
    )
    if registrar is None:
        return None
    iana_id = next(
        (
            _text(public_id.get("identifier"))
            for public_id in _list(registrar.get("publicIds"))
            if isinstance(public_id, dict)
            and _text(public_id.get("type")).lower() == "iana registrar id"
        ),
        "",
    )
    return RegistrarFacts(
        name=(vcard(registrar, "fn") or _text(registrar.get("handle")))[:255],
        iana_id=iana_id[:32],
        abuse_email=abuse_email(registrar) or abuse_email(data),
        source_url=self_link(data, url),
    )


# --- client ------------------------------------------------------------------------------------


class RdapClient:
    """Sequential, polite RDAP lookups with a cached IANA bootstrap."""

    def __init__(
        self,
        client: PoliteClient,
        *,
        delay_s: float = RDAP_DELAY_S,
        bootstrap_url: str = IANA_BOOTSTRAP,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._delay_s = delay_s
        self._bootstrap_url = bootstrap_url
        self._clock = clock
        self._bootstraps: dict[str, tuple[float, Json]] = {}

    def bootstrap(self, name: str) -> Json:
        """`ipv4`, `ipv6` or `dns`, fetched at most once per `BOOTSTRAP_TTL_S`."""
        cached = self._bootstraps.get(name)
        if cached is not None and self._clock() - cached[0] < BOOTSTRAP_TTL_S:
            return cached[1]
        data = self._get_json(f"{self._bootstrap_url}{name}.json", BOOTSTRAP_MAX_BYTES)
        self._bootstraps[name] = (self._clock(), data)
        return data

    def ip_network(self, address: str) -> NetworkFacts:
        version = ipaddress.ip_address(address).version
        base = ip_base_url(self.bootstrap(f"ipv{version}"), address)
        if base is None:
            raise RdapError("rdap_no_registry")
        url = f"{base}ip/{address}"
        return parse_ip_network(self._get_json(url, RDAP_MAX_BYTES), url)

    def domain(self, domain: str) -> RegistrarFacts | None:
        base = domain_base_url(self.bootstrap("dns"), domain)
        if base is None:
            raise RdapError("rdap_no_registry")
        url = f"{base}domain/{domain}"
        return parse_domain(self._get_json(url, RDAP_MAX_BYTES), url)

    def _get_json(self, url: str, max_bytes: int) -> Json:
        try:
            response = self._client.get(url, min_delay_s=self._delay_s, max_bytes=max_bytes)
        except ResponseTooLargeError as exc:
            raise RdapError("rdap_too_large") from exc
        except httpx.HTTPError as exc:
            raise RdapError("rdap_network_error") from exc
        if response.status == 404:
            raise RdapError("rdap_not_found")
        if response.status != 200:
            raise RdapError(f"rdap_http_{response.status}")
        try:
            data = json.loads(response.body)
        except (ValueError, RecursionError) as exc:  # RecursionError: nesting past the decoder
            raise RdapError("rdap_invalid_json") from exc
        if not isinstance(data, dict):
            raise RdapError("rdap_invalid_json")
        return data
