"""RDAP: bootstrap routing, abuse-contact and registrar extraction, and the polite client.

Fixtures under `fixtures/rdap/` are real registry responses (Cloudflare at ARIN, Hetzner at RIPE,
cloudflare.com at Verisign) trimmed to their structure, with every individual's entity removed.
They hold registry data only. No test here touches the network.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from catalog.hosting.rdap import (
    MAX_ENTITIES,
    RDAP_MAX_BYTES,
    RdapClient,
    RdapError,
    domain_base_url,
    ip_base_url,
    parse_domain,
    parse_ip_network,
    registrable_domain,
    safe_email,
    safe_link,
    self_link,
    walk_entities,
)
from core.http import PoliteClient

FIXTURES = Path(__file__).parent / "fixtures" / "rdap"
UA = "MarcelineBot/0.1"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


# --- bootstrap ---------------------------------------------------------------------------------


def test_ip_bootstrap_routes_to_the_right_registry() -> None:
    v4 = fixture("bootstrap_ipv4.json")
    assert ip_base_url(v4, "104.16.0.1") == "https://rdap.arin.net/registry/"
    assert ip_base_url(v4, "5.9.0.1") == "https://rdap.db.ripe.net/"
    assert ip_base_url(v4, "41.0.0.1") == "https://rdap.afrinic.net/rdap/"  # https preferred
    assert ip_base_url(fixture("bootstrap_ipv6.json"), "2a01:4f8::1") == "https://rdap.db.ripe.net/"
    assert ip_base_url(v4, "2a01:4f8::1") is None


def test_ip_bootstrap_prefers_the_longest_prefix() -> None:
    bootstrap = {
        "services": [
            [["10.0.0.0/8"], ["https://wide.example/"]],
            [["10.1.0.0/16"], ["https://narrow.example"]],
        ]
    }
    assert ip_base_url(bootstrap, "10.1.2.3") == "https://narrow.example/"
    assert ip_base_url(bootstrap, "10.2.2.3") == "https://wide.example/"


def test_domain_bootstrap_matches_the_tld() -> None:
    dns = fixture("bootstrap_dns.json")
    assert domain_base_url(dns, "cloudflare.com") == "https://rdap.verisign.com/com/v1/"
    assert domain_base_url(dns, "cdn.example.net") == "https://rdap.verisign.com/net/v1/"
    assert domain_base_url(dns, "example.de") is None  # not every ccTLD is in the bootstrap


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("cdn1.example.com", "example.com"),
        ("example.com.", "example.com"),
        ("static.media.example.co.uk", "example.co.uk"),
        ("EXAMPLE.NET", "example.net"),
        ("104.16.0.1", None),
        ("localhost", None),
        ("bad_label.example.com", None),
    ],
)
def test_registrable_domain(hostname: str, expected: str | None) -> None:
    assert registrable_domain(hostname) == expected


# --- abuse contacts ----------------------------------------------------------------------------


def test_abuse_contact_from_the_network_entity() -> None:
    facts = parse_ip_network(fixture("ip_ripe_hetzner.json"), "https://asked.example/ip/5.9.0.1")

    assert facts.abuse_email == "abuse@hetzner.com"
    assert (facts.name, facts.country) == ("HETZNER-fsn1-dc7", "DE")
    # RIPE nests the organisation under the abuse role; the self link is where it came from.
    assert facts.organisation == "Hetzner Online GmbH"
    assert facts.source_url == "https://rdap.db.ripe.net/ip/5.9.0.1"


def test_abuse_contact_nested_inside_the_registrant() -> None:
    """ARIN puts the abuse role under the organisation entity; it must still be found."""
    data = fixture("ip_arin_nested_abuse.json")
    assert all("abuse" not in entity["roles"] for entity in data["entities"])

    facts = parse_ip_network(data, "https://rdap.arin.net/registry/ip/104.16.0.1")

    assert facts.abuse_email == "abuse@cloudflare.com"
    assert facts.organisation == "Cloudflare, Inc."
    assert facts.country == ""  # ARIN networks carry no country; the origin lookup supplies it
    assert facts.source_url == "https://rdap.arin.net/registry/ip/104.16.0.0"


def test_a_top_level_abuse_role_outranks_other_roles_with_an_email() -> None:
    facts = parse_ip_network(fixture("ip_arin_cloudflare.json"), "https://x.example/")
    # The NOC and admin entities also carry emails; only the abuse role counts.
    assert facts.abuse_email == "abuse@cloudflare.com"


def test_a_network_without_an_abuse_role_has_no_abuse_contact() -> None:
    facts = parse_ip_network(fixture("ip_ripe_no_abuse.json"), "https://x.example/")
    assert facts.abuse_email == ""
    assert facts.organisation == "Hetzner Online GmbH"


def test_the_request_url_is_the_source_when_there_is_no_self_link() -> None:
    facts = parse_ip_network({"name": "NET", "country": "nl"}, "https://asked.example/ip/1.2.3.4")
    assert (facts.source_url, facts.country) == ("https://asked.example/ip/1.2.3.4", "NL")


def test_registrar_and_its_abuse_contact() -> None:
    facts = parse_domain(fixture("domain_verisign_cloudflare.json"), "https://asked.example/")

    assert facts is not None
    assert (facts.name, facts.iana_id) == ("Cloudflare, Inc.", "1910")
    assert facts.abuse_email == "registrar-abuse@cloudflare.com"
    assert facts.source_url == "https://rdap.verisign.com/com/v1/domain/cloudflare.com"


def test_a_domain_without_a_registrar_entity() -> None:
    assert parse_domain({"objectClassName": "domain", "ldhName": "example.de"}, "u") is None


# --- hostile responses --------------------------------------------------------------------------
# A registry answer is untrusted input: whatever its shape, parsing yields facts or empty values,
# never an exception that would end the whole `lookup_hosting` run (review finding M4).

REGISTRAR = {"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "Registrar"]]]}


@pytest.mark.parametrize(
    "data",
    [
        {"entities": 5},
        {"entities": [{"roles": 1}]},
        {"entities": [{"roles": [["registrar"]], "entities": "abuse"}]},
        {"entities": [{"roles": ["abuse"], "vcardArray": ["vcard", 7]}]},
        {"entities": [{"roles": ["abuse"], "vcardArray": ["vcard", [["email", {}, "text", 5]]]}]},
        {"links": 7, "handle": {"a": 1}, "name": ["x"], "country": 49},
        {"links": [{"rel": "self", "href": ["https://x.example/"]}]},
        {"entities": [{**REGISTRAR, "publicIds": 3}]},
        {"entities": [{**REGISTRAR, "publicIds": [{"type": 1, "identifier": {"x": 1}}]}]},
        {"entities": [{**REGISTRAR, "handle": ["h"], "vcardArray": None}]},
    ],
)
def test_unexpected_types_parse_to_empty_values(data: dict[str, Any]) -> None:
    network = parse_ip_network(data, "https://asked.example/ip/1.2.3.4")
    registrar = parse_domain(data, "https://asked.example/domain/example.com")

    assert network.source_url == "https://asked.example/ip/1.2.3.4"
    assert network.abuse_email == network.handle == network.country == ""
    assert registrar is None or (registrar.iana_id == "" and registrar.abuse_email == "")


def test_deeply_nested_json_is_an_error_code_not_a_crash() -> None:
    nested = b"[" * 200_000
    assert len(nested) < RDAP_MAX_BYTES
    registry = Registry(
        {
            "https://data.iana.org/rdap/ipv4.json": body("bootstrap_ipv4.json"),
            "https://rdap.db.ripe.net/ip/5.9.0.1": (200, nested),
        }
    )

    with pytest.raises(RdapError) as raised:
        client_for(registry).ip_network("5.9.0.1")

    assert raised.value.code == "rdap_invalid_json"


def test_deeply_nested_entities_are_capped() -> None:
    node: dict[str, Any] = {"roles": ["abuse"]}
    for _ in range(5000):
        node = {"roles": ["technical"], "entities": [node]}
    wide = {"entities": [{"roles": ["noc"]} for _ in range(MAX_ENTITIES * 3)]}

    assert len(list(walk_entities(node))) <= MAX_ENTITIES
    assert len(list(walk_entities(wide))) == MAX_ENTITIES
    assert parse_ip_network(node, "https://asked.example/").abuse_email == ""


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "http://rdap.example/ip/1.2.3.4",
        "data:text/html,<script>",
        "https://",
        "https://rdap.example/ip/1.2.3.4\n<script>",
        "https://user@rdap.example/",
        "//rdap.example/ip",
    ],
)
def test_only_https_self_links_are_kept(href: str) -> None:
    data = {"links": [{"rel": "self", "href": href}], "entities": [REGISTRAR]}

    facts = parse_domain(data, "https://asked.example/domain/example.com")

    assert facts is not None
    assert facts.source_url == "https://asked.example/domain/example.com"
    assert self_link(data, "http://plain.example/") == ""
    assert safe_link(href) == ""


def test_a_valid_https_self_link_is_kept() -> None:
    assert safe_link("https://rdap.db.ripe.net/ip/5.9.0.1") == "https://rdap.db.ripe.net/ip/5.9.0.1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abuse@hetzner.com", "abuse@hetzner.com"),
        ("mailto:Abuse@Example.NET", "Abuse@Example.NET"),
        ("abuse@localhost", ""),
        ("<script>@example.com", ""),
        ("abuse@example.com, other@example.com", ""),
        ("abuse @example.com", ""),
        ("abuse@exa mple.com", ""),
        ("abuse@-example.com", ""),
        ("abuse@@example.com", ""),
        ("a" * 250 + "@example.com", ""),
        ("abuse@example.com\r\nBcc: x@example.com", ""),
    ],
)
def test_abuse_emails_are_checked_before_they_are_kept(value: str, expected: str) -> None:
    assert safe_email(value) == expected


def test_an_invalid_abuse_email_falls_through_to_the_next_abuse_entity() -> None:
    def abuse(email: str) -> dict[str, Any]:
        return {"roles": ["abuse"], "vcardArray": ["vcard", [["email", {}, "text", email]]]}

    data = {"entities": [abuse("javascript:alert(1)@x"), abuse("abuse@example.net")]}

    assert parse_ip_network(data, "https://asked.example/").abuse_email == "abuse@example.net"


# --- client ------------------------------------------------------------------------------------


class Registry:
    """A fake set of RDAP servers: path → fixture, and a log of every request."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.routes.get(str(request.url), (404, b"{}"))
        return httpx.Response(status, content=body)


def body(name: str) -> tuple[int, bytes]:
    return 200, (FIXTURES / name).read_bytes()


def client_for(registry: Registry, clock: list[float] | None = None) -> RdapClient:
    now = clock if clock is not None else [0.0]
    polite = PoliteClient(
        UA, max_retries=0, transport=httpx.MockTransport(registry), sleep=lambda _: None
    )
    return RdapClient(polite, clock=lambda: now[0])


def test_client_looks_up_networks_and_domains_through_the_bootstrap() -> None:
    registry = Registry(
        {
            "https://data.iana.org/rdap/ipv4.json": body("bootstrap_ipv4.json"),
            "https://data.iana.org/rdap/dns.json": body("bootstrap_dns.json"),
            "https://rdap.db.ripe.net/ip/5.9.0.1": body("ip_ripe_hetzner.json"),
            "https://rdap.verisign.com/com/v1/domain/cloudflare.com": body(
                "domain_verisign_cloudflare.json"
            ),
        }
    )
    rdap = client_for(registry)

    network = rdap.ip_network("5.9.0.1")
    registrar = rdap.domain("cloudflare.com")

    assert network.abuse_email == "abuse@hetzner.com"
    assert registrar is not None and registrar.iana_id == "1910"
    assert {request.headers["user-agent"] for request in registry.requests} == {UA}
    hosts = {request.url.host for request in registry.requests}
    assert hosts == {"data.iana.org", "rdap.db.ripe.net", "rdap.verisign.com"}


def test_the_bootstrap_is_fetched_once_per_day() -> None:
    registry = Registry(
        {
            "https://data.iana.org/rdap/ipv4.json": body("bootstrap_ipv4.json"),
            "https://rdap.db.ripe.net/ip/5.9.0.1": body("ip_ripe_hetzner.json"),
            "https://rdap.db.ripe.net/ip/5.9.0.2": body("ip_ripe_hetzner.json"),
        }
    )
    clock = [0.0]
    rdap = client_for(registry, clock)

    rdap.ip_network("5.9.0.1")
    clock[0] = 3600.0
    rdap.ip_network("5.9.0.2")
    bootstraps = [r for r in registry.requests if r.url.host == "data.iana.org"]
    assert len(bootstraps) == 1

    clock[0] = 25 * 3600.0
    rdap.ip_network("5.9.0.1")
    assert len([r for r in registry.requests if r.url.host == "data.iana.org"]) == 2


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ((404, b"{}"), "rdap_not_found"),
        ((503, b""), "rdap_http_503"),
        ((200, b"<html>"), "rdap_invalid_json"),
        ((200, b"[]"), "rdap_invalid_json"),
    ],
)
def test_client_errors_are_codes(response: tuple[int, bytes], code: str) -> None:
    registry = Registry(
        {
            "https://data.iana.org/rdap/ipv4.json": body("bootstrap_ipv4.json"),
            "https://rdap.db.ripe.net/ip/5.9.0.1": response,
        }
    )
    with pytest.raises(RdapError) as raised:
        client_for(registry).ip_network("5.9.0.1")
    assert raised.value.code == code


def test_an_address_no_registry_serves() -> None:
    registry = Registry({"https://data.iana.org/rdap/ipv4.json": (200, b'{"services": []}')})
    with pytest.raises(RdapError) as raised:
        client_for(registry).ip_network("5.9.0.1")
    assert raised.value.code == "rdap_no_registry"
