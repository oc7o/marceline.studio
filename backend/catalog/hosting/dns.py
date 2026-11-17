"""Addresses of a hostname, and the autonomous system an address is routed from.

Addresses come from the system resolver (`socket.getaddrinfo`). The origin AS comes from Team
Cymru's IP-to-ASN DNS service (`<reversed ip>.origin.asn.cymru.com` TXT, then
`AS<n>.asn.cymru.com` for the AS name): RDAP IP responses do not carry the origin AS at most
registries, and a TXT query is DNS, not a request to anyone's website.

The standard library has no TXT lookup, so `query_txt` speaks the few bytes of RFC 1035 it needs
over UDP to the nameservers in `/etc/resolv.conf`. Everything network-facing is injectable, and
tests never touch the network.
"""

import ipaddress
import secrets
import socket
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

TXT = 16
IN = 1
DNS_PORT = 53
DNS_TIMEOUT_S = 3.0
RESOLV_CONF = Path("/etc/resolv.conf")
CYMRU_V4 = "origin.asn.cymru.com"
CYMRU_V6 = "origin6.asn.cymru.com"
CYMRU_AS = "asn.cymru.com"

Resolver = Callable[[str], list[str]]
TxtLookup = Callable[[str], list[str]]


class DnsError(Exception):
    """A TXT query could not be answered (timeout, malformed reply, server failure)."""


@dataclass(frozen=True)
class Origin:
    """What Team Cymru's service knows about the prefix an address is announced in."""

    asn: int
    prefix: str
    country: str
    registry: str
    as_name: str = ""


def resolve_addresses(hostname: str) -> list[str]:
    """A and AAAA records via the system resolver, IPv4 first, deduplicated. [] when none."""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _type, _proto, _name, sockaddr in infos:
        try:
            address = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    addresses.sort(key=lambda address: (address.version, int(address)))
    return [str(address) for address in addresses]


def build_query(name: str, query_id: int) -> bytes:
    """A recursive TXT query for `name`."""
    labels = [label.encode("idna") for label in name.rstrip(".").split(".") if label]
    if any(len(label) > 63 for label in labels):
        raise DnsError("label too long")
    question = b"".join(bytes([len(label)]) + label for label in labels) + b"\x00"
    return (
        struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
        + question
        + struct.pack(">HH", TXT, IN)
    )


def _skip_name(data: bytes, offset: int) -> int:
    while True:
        if offset >= len(data):
            raise DnsError("truncated name")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


def parse_txt_response(data: bytes, query_id: int) -> list[str]:
    """The TXT strings of a reply, each record's character-strings joined. [] for NXDOMAIN."""
    if len(data) < 12:
        raise DnsError("truncated header")
    reply_id, flags, questions, answers, _authority, _additional = struct.unpack(
        ">HHHHHH", data[:12]
    )
    if reply_id != query_id or not flags & 0x8000:
        raise DnsError("not a reply to this query")
    rcode = flags & 0x000F
    if rcode == 3:
        return []
    if rcode != 0:
        raise DnsError(f"rcode {rcode}")
    offset = 12
    for _ in range(questions):
        offset = _skip_name(data, offset) + 4
    records: list[str] = []
    for _ in range(answers):
        offset = _skip_name(data, offset)
        if offset + 10 > len(data):
            raise DnsError("truncated record")
        rtype, _rclass, _ttl, length = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + length]
        offset += length
        if rtype != TXT:
            continue
        parts: list[bytes] = []
        position = 0
        while position < len(rdata):
            size = rdata[position]
            parts.append(rdata[position + 1 : position + 1 + size])
            position += 1 + size
        records.append(b"".join(parts).decode("utf-8", errors="replace"))
    return records


def nameservers(path: Path = RESOLV_CONF) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    servers = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1].split("%")[0])
    return servers


def query_txt(
    name: str, *, servers: Sequence[str] | None = None, timeout_s: float = DNS_TIMEOUT_S
) -> list[str]:
    """TXT records of `name` from the first nameserver that answers."""
    query_id = secrets.randbelow(0x10000)
    payload = build_query(name, query_id)
    last_error: Exception = DnsError("no nameserver configured")
    for server in servers if servers is not None else nameservers():
        try:
            family = (
                socket.AF_INET6 if ipaddress.ip_address(server).version == 6 else socket.AF_INET
            )
        except ValueError:
            continue
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout_s)
                sock.sendto(payload, (server, DNS_PORT))
                data, _ = sock.recvfrom(4096)
            return parse_txt_response(data, query_id)
        except (OSError, DnsError) as exc:
            last_error = exc
    raise DnsError(str(last_error))


def origin_query_name(address: str) -> str:
    ip = ipaddress.ip_address(address)
    if ip.version == 4:
        return ".".join(reversed(str(ip).split("."))) + "." + CYMRU_V4
    nibbles = ip.exploded.replace(":", "")
    return ".".join(reversed(nibbles)) + "." + CYMRU_V6


def _fields(record: str) -> list[str]:
    return [field.strip() for field in record.split("|")]


def lookup_origin(address: str, txt: TxtLookup = query_txt) -> Origin | None:
    """Origin AS, prefix, allocation country and AS name of `address`; None when unannounced.

    An address announced by several ASes lists them space-separated; the first one is used.
    Raises `DnsError` when the service cannot be reached.
    """
    for record in txt(origin_query_name(address)):
        fields = _fields(record)
        if len(fields) < 4 or not fields[0]:
            continue
        try:
            asn = int(fields[0].split()[0])
        except ValueError:
            continue
        as_name = ""
        for detail in txt(f"AS{asn}.{CYMRU_AS}"):
            parts = _fields(detail)
            if len(parts) >= 5:
                as_name = parts[4]
                break
        return Origin(
            asn=asn,
            prefix=fields[1],
            country=fields[2].upper(),
            registry=fields[3],
            as_name=as_name,
        )
    return None
