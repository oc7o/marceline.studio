"""Is a CDN or reverse proxy in front of an address? Pure, and without asking the site anything.

Decided from the origin AS first and from ranges the CDN publishes second (for when the AS lookup
failed). Only networks whose business is overwhelmingly proxying are listed: a general hosting AS
that also sells a CDN (AWS, Gcore, DataCamp) would turn "behind a CDN" into a guess. CloudFront
is therefore not detected; ADR 0027 names that gap.
"""

import ipaddress

#: Origin AS → CDN code.
CDN_ASNS: dict[int, str] = {
    13335: "cloudflare",
    209242: "cloudflare",
    54113: "fastly",
    20940: "akamai",
    16625: "akamai",
    200325: "bunnycdn",
    57724: "ddos_guard",
    262254: "ddos_guard",
    20446: "stackpath",
}

#: https://www.cloudflare.com/ips-v4 and /ips-v6, as published on 2026-09-16.
CLOUDFLARE_RANGES = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)
RANGES: tuple[tuple[str, tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]], ...] = (
    ("cloudflare", CLOUDFLARE_RANGES),
)


def detect_cdn(asn: int | None, address: str | None) -> str | None:
    """The CDN code for this origin AS or address, or None when nothing known is in front."""
    if asn is not None and asn in CDN_ASNS:
        return CDN_ASNS[asn]
    if not address:
        return None
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    for code, networks in RANGES:
        if any(ip.version == network.version and ip in network for network in networks):
            return code
    return None
