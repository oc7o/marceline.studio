"""DNS: the TXT wire format, the origin-AS service's answers, and address resolution. No network."""

import socket
import struct
from pathlib import Path
from typing import Any

import pytest

from catalog.hosting import dns
from catalog.hosting.dns import (
    DnsError,
    build_query,
    lookup_origin,
    nameservers,
    origin_query_name,
    parse_txt_response,
)

CLOUDFLARE_AS = "13335 | US | arin | 2010-07-14 | CLOUDFLARENET - Cloudflare, Inc., US"
HETZNER_AS = "24940 | DE | ripencc | 2002-06-03 | HETZNER-AS - Hetzner Online GmbH, DE"
CYMRU = {
    "1.0.16.104.origin.asn.cymru.com": ["13335 | 104.16.0.0/20 | US | arin | 2014-03-28"],
    "AS13335.asn.cymru.com": [CLOUDFLARE_AS],
    "1.0.9.5.origin.asn.cymru.com": ["24940 9009 | 5.9.0.0/16 | DE | ripencc | 2012-04-25"],
    "AS24940.asn.cymru.com": [HETZNER_AS],
}


def fake_txt(name: str) -> list[str]:
    return CYMRU.get(name, [])


def reply(query: bytes, answers: list[list[bytes]], *, rcode: int = 0) -> bytes:
    """A reply to `query` carrying one TXT record per entry, names compressed to the question."""
    query_id = struct.unpack(">H", query[:2])[0]
    header = struct.pack(">HHHHHH", query_id, 0x8180 | rcode, 1, len(answers), 0, 0)
    records = b""
    for strings in answers:
        rdata = b"".join(bytes([len(part)]) + part for part in strings)
        records += b"\xc0\x0c" + struct.pack(">HHIH", 16, 1, 300, len(rdata)) + rdata
    return header + query[12:] + records


def test_query_and_reply_round_trip() -> None:
    query = build_query("1.0.16.104.origin.asn.cymru.com", 0x1234)

    assert query[:2] == b"\x12\x34"
    assert query.endswith(b"\x06origin\x03asn\x05cymru\x03com\x00\x00\x10\x00\x01")
    data = reply(query, [[b"13335 | 104.16.0.0/20 | ", b"US | arin | 2014-03-28"]])
    assert parse_txt_response(data, 0x1234) == ["13335 | 104.16.0.0/20 | US | arin | 2014-03-28"]


def test_nxdomain_is_no_records_and_other_failures_raise() -> None:
    query = build_query("example.com", 7)
    assert parse_txt_response(reply(query, [], rcode=3), 7) == []
    with pytest.raises(DnsError):
        parse_txt_response(reply(query, [], rcode=2), 7)
    with pytest.raises(DnsError):
        parse_txt_response(reply(query, []), 8)  # someone else's reply
    with pytest.raises(DnsError):
        parse_txt_response(b"\x00\x07", 7)


def test_query_txt_asks_the_configured_nameserver(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSocket:
        def __init__(self, family: int, kind: int) -> None:
            assert (family, kind) == (socket.AF_INET, socket.SOCK_DGRAM)

        def __enter__(self) -> "FakeSocket":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def settimeout(self, value: float) -> None:
            return None

        def sendto(self, payload: bytes, address: tuple[str, int]) -> None:
            sent.append((payload, address))

        def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
            return reply(sent[-1][0], [[b"hello"]]), ("192.0.2.53", 53)

    monkeypatch.setattr(socket, "socket", FakeSocket)

    assert dns.query_txt("example.com", servers=["not-an-ip", "192.0.2.53"]) == ["hello"]
    assert [address for _, address in sent] == [("192.0.2.53", 53)]
    with pytest.raises(DnsError):
        dns.query_txt("example.com", servers=[])


def test_nameservers_from_resolv_conf(tmp_path: Path) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text("# comment\nsearch example\nnameserver 192.0.2.53\nnameserver fe80::1%eth0\n")
    assert nameservers(conf) == ["192.0.2.53", "fe80::1"]
    assert nameservers(tmp_path / "missing") == []


def test_origin_query_names() -> None:
    assert origin_query_name("104.16.0.1") == "1.0.16.104.origin.asn.cymru.com"
    name = origin_query_name("2606:4700::1")
    assert name.endswith(".0.0.7.4.6.0.6.2.origin6.asn.cymru.com")
    assert name.startswith("1.0.0.0.")
    assert len(name.removesuffix(".origin6.asn.cymru.com").split(".")) == 32


def test_origin_lookup_parses_asn_country_and_as_name() -> None:
    origin = lookup_origin("104.16.0.1", fake_txt)

    assert origin is not None
    assert (origin.asn, origin.prefix, origin.country, origin.registry) == (
        13335,
        "104.16.0.0/20",
        "US",
        "arin",
    )
    assert origin.as_name == "CLOUDFLARENET - Cloudflare, Inc., US"


def test_origin_lookup_takes_the_first_of_several_origin_ases() -> None:
    origin = lookup_origin("5.9.0.1", fake_txt)
    assert origin is not None and origin.asn == 24940


def test_an_unannounced_address_has_no_origin() -> None:
    assert lookup_origin("192.0.2.1", fake_txt) is None


def test_resolve_addresses_orders_ipv4_first_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def getaddrinfo(host: str, port: None, proto: int = 0) -> list[tuple[Any, ...]]:
        assert host == "cdn.example.test"
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    assert dns.resolve_addresses("cdn.example.test") == ["192.0.2.1", "192.0.2.9", "2001:db8::1"]


def test_an_unresolvable_name_has_no_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host: str, port: None, proto: int = 0) -> list[tuple[Any, ...]]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    assert dns.resolve_addresses("gone.example.test") == []
