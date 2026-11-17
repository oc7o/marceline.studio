"""Does a URL belong to a licensed partner of this organisation? (ADR 0034)

A copy on a licensed partner's site is not a copy to report. `partner_for_url` answers that for
one organisation; `normalise_domain` is what partner rows store, so a lookup compares like with
like. A partner domain covers its subdomains (`example.com` covers `cdn.example.com`, never
`badexample.com`); a `path_prefix` narrows a partner to one area of a shared platform
(`/studio-name` on a platform domain), matched on whole path segments.
"""

from urllib.parse import urlsplit

from accounts.models import Organisation
from marceline.rights.models import LicensedPartner


class InvalidDomainError(ValueError):
    pass


def normalise_domain(value: str) -> str:
    """A bare lowercase host from a host or URL: no scheme, port, path, trailing dot or `www.`.

    Internationalised names are stored in their ASCII (punycode) form, as URLs carry them.
    """
    raw = value.strip()
    if not raw:
        raise InvalidDomainError("empty domain")
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""
    host = host.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise InvalidDomainError(f"not a domain: {value!r}") from error
    labels = host.split(".")
    if len(labels) < 2 or not all(
        label and len(label) <= 63 and all(ch.isalnum() or ch == "-" for ch in label)
        for label in labels
    ):
        raise InvalidDomainError(f"not a domain: {value!r}")
    return host


def normalise_path_prefix(value: str) -> str:
    """`/studio-name` from `studio-name/`, `/studio-name`, or empty for no prefix."""
    stripped = value.strip().strip("/")
    return f"/{stripped}" if stripped else ""


def host_matches(host: str, domain: str) -> bool:
    """Whether `host` is `domain` or one of its subdomains. Both already normalised."""
    return host == domain or host.endswith(f".{domain}")


def path_matches(path: str, prefix: str) -> bool:
    """Whether `path` lies under `prefix` on whole segments (`/a` covers `/a/b`, not `/ab`)."""
    if not prefix:
        return True
    lowered, wanted = path.lower(), prefix.lower()
    return lowered == wanted or lowered.startswith(f"{wanted}/")


def matching_partner(url_or_host: str, partners: list[LicensedPartner]) -> LicensedPartner | None:
    """The most specific partner row covering this URL or host, or None. Pure."""
    try:
        host = normalise_domain(url_or_host)
    except InvalidDomainError:
        return None
    raw = url_or_host.strip()
    path = urlsplit(raw if "//" in raw else f"//{raw}").path or "/"
    candidates = [
        partner
        for partner in partners
        if partner.domain
        and host_matches(host, partner.domain)
        and path_matches(path, partner.path_prefix)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (len(row.domain), len(row.path_prefix), -row.pk))


def partner_for_url(organisation: Organisation, url_or_host: str) -> LicensedPartner | None:
    """The licensed partner of this organisation a URL or host belongs to, or None."""
    partners = list(LicensedPartner.objects.filter(organisation=organisation).exclude(domain=""))
    return matching_partner(url_or_host, partners)
