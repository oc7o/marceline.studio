"""Provider function, location/nexus, complaint channels and v1 support. Pure; codes are stable.

A jurisdiction label cannot pick a notice route: the route depends on what a provider **does** for
the material
(its function) far more than on where its servers sit. So every provider attached to a finding
carries, separately (ADR 0027 amendment 2026-09-17):

- **location/nexus** — `eu_facing`, `us_nexus`, `other`, `unknown`: a hint for a person, **not a
  legal verdict**. Staff may set it on the directory entry; otherwise it follows the provider's
  published establishment country, else the IP allocation country.
- **function** in its role — derived where it is mechanical (a registrar is a registrar; an AS we
  detect as a CDN is a reverse proxy), else the directory entry's default, and a per-hostname staff
  override with a note beats both. **Nothing derives `storing_cdn`**: that a CDN stores the
  material is a verified fact, so only an override can say so.
- **complaint channels** — from the staff-maintained directory, or the RDAP abuse contact as the
  fallback. Each carries a **v1 support** code computed from (function, channel kind) by
  `v1_support`.

Roles (`Slot`): `network` is the AS serving a hostname (the page's host or a content host),
`registrar` is the registrar of its registrable domain, `service` is an operator staff linked to
the hostname itself (a file host, a platform).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from catalog.hosting.jurisdiction import jurisdiction_of

Nexus = Literal["eu_facing", "us_nexus", "other", "unknown"]
NEXUSES: tuple[Nexus, ...] = ("eu_facing", "us_nexus", "other", "unknown")
NexusSource = Literal["directory", "establishment_country", "allocation_country", "none"]

ProviderFunction = Literal[
    "origin_host",
    "file_host",
    "user_content_platform",
    "reverse_proxy_cdn",
    "storing_cdn",
    "registrar",
    "search_engine",
    "payment",
    "advertiser",
    "other",
    "unknown",
]
PROVIDER_FUNCTIONS: tuple[ProviderFunction, ...] = (
    "origin_host",
    "file_host",
    "user_content_platform",
    "reverse_proxy_cdn",
    "storing_cdn",
    "registrar",
    "search_engine",
    "payment",
    "advertiser",
    "other",
    "unknown",
)
#: Where a role's function came from, strongest first.
FunctionSource = Literal["override", "derived", "directory", "none"]

ChannelKind = Literal[
    "dsa_art16",
    "dmca_512c",
    "dmca_512d",
    "provider_copyright_policy",
    "provider_copyright_policy_dmca_style",
    "abuse_email",
    "web_form",
    "forwarding_only",
    "none_known",
]
CHANNEL_KINDS: tuple[ChannelKind, ...] = (
    "dsa_art16",
    "dmca_512c",
    "dmca_512d",
    "provider_copyright_policy",
    "provider_copyright_policy_dmca_style",
    "abuse_email",
    "web_form",
    "forwarding_only",
    "none_known",
)

#: Staff-set, per channel; never inferred from a country (DPF certification is per recipient).
TransferBasis = Literal[
    "not_required", "adequacy", "dpf", "art46", "art49_legal_claim", "counsel_review"
]
TRANSFER_BASES: tuple[TransferBasis, ...] = (
    "not_required",
    "adequacy",
    "dpf",
    "art46",
    "art49_legal_claim",
    "counsel_review",
)

Slot = Literal["service", "network", "registrar"]
SLOTS: tuple[Slot, ...] = ("service", "network", "registrar")

#: What version one may do with a (function, channel) pair, best first.
#: - `primary`: the route a notice draft is built for (origin host, file host, platform, and a CDN
#:   staff verified as storing the material).
#: - `delisting_draft`: a separate search-removal draft; it removes a result, not the page.
#: - `forwarding_only`: a reverse-proxy CDN — forwarding and host identification, not removal.
#: - `additional_contact_only`: registrar, payment, advertiser — shown as a contact, no escalation.
#: - `needs_classification`: the function is `other`/`unknown`; a person decides first.
#: - `not_supported`: no channel, or a channel that does not fit the function (e.g. §512(d) at a
#:   host, Art. 16 at a search engine).
V1Support = Literal[
    "primary",
    "delisting_draft",
    "forwarding_only",
    "additional_contact_only",
    "needs_classification",
    "not_supported",
]
V1_SUPPORT_ORDER: tuple[V1Support, ...] = (
    "primary",
    "delisting_draft",
    "forwarding_only",
    "additional_contact_only",
    "needs_classification",
    "not_supported",
)

HOSTING_FUNCTIONS: frozenset[str] = frozenset(
    {"origin_host", "file_host", "user_content_platform", "storing_cdn"}
)
CONTACT_ONLY_FUNCTIONS: frozenset[str] = frozenset({"registrar", "payment", "advertiser"})
UNCLASSIFIED_FUNCTIONS: frozenset[str] = frozenset({"other", "unknown"})
#: Channels a host can act on. §512(d) is for information-location tools, not hosts.
HOST_CHANNELS: frozenset[str] = frozenset(
    {
        "dsa_art16",
        "dmca_512c",
        "provider_copyright_policy",
        "provider_copyright_policy_dmca_style",
        "abuse_email",
        "web_form",
    }
)
#: A search engine's delisting routes. Art. 16 and §512(c) are hosting routes and an abuse mailbox
#: is not a legal-removal process.
SEARCH_CHANNELS: frozenset[str] = frozenset(
    {"dmca_512d", "provider_copyright_policy", "provider_copyright_policy_dmca_style", "web_form"}
)

#: Default number of days after which a directory channel is due to be checked again.
DEFAULT_RECHECK_DAYS = 90


def nexus_of_country(country: str | None) -> Nexus:
    """The location hint a country gives. A hint only: a non-EU provider can be EU-facing."""
    return {"eu": "eu_facing", "us": "us_nexus", "other": "other"}.get(  # type: ignore[return-value]
        jurisdiction_of(country), "unknown"
    )


def resolve_nexus(
    *, staff_nexus: str, establishment_country: str, allocation_country: str
) -> tuple[Nexus, NexusSource]:
    """Staff's nexus, else the published establishment country, else the IP allocation country."""
    if staff_nexus in NEXUSES:
        return staff_nexus, "directory"
    if nexus_of_country(establishment_country) != "unknown":
        return nexus_of_country(establishment_country), "establishment_country"
    if nexus_of_country(allocation_country) != "unknown":
        return nexus_of_country(allocation_country), "allocation_country"
    return "unknown", "none"


def derive_function(
    slot: Slot, *, cdn: str, override: str, directory_default: str
) -> tuple[ProviderFunction, FunctionSource]:
    """A role's function: override > mechanical > directory default > mechanical fallback.

    Mechanical: the registrar slot is `registrar`; a network we detected as a CDN is
    `reverse_proxy_cdn`, whatever its directory entry says — only a per-hostname override may call
    it `storing_cdn`. A network with no CDN in front is the `origin_host` unless the directory says
    otherwise. A directory default of `storing_cdn` is ignored (the model forbids storing one).
    """
    if override in PROVIDER_FUNCTIONS:
        return override, "override"
    if slot == "registrar":
        return "registrar", "derived"
    if slot == "network" and cdn:
        return "reverse_proxy_cdn", "derived"
    if directory_default in PROVIDER_FUNCTIONS and directory_default != "storing_cdn":
        return directory_default, "directory"
    if slot == "network":
        return "origin_host", "derived"
    return "unknown", "none"


def v1_support(function: str, kind: str) -> V1Support:
    """The version-one policy for one (function, channel kind) pair."""
    if kind not in CHANNEL_KINDS or kind == "none_known":
        return "not_supported"
    if function in UNCLASSIFIED_FUNCTIONS or function not in PROVIDER_FUNCTIONS:
        return "needs_classification"
    if function in CONTACT_ONLY_FUNCTIONS:
        return "additional_contact_only"
    if function == "search_engine":
        return "delisting_draft" if kind in SEARCH_CHANNELS else "not_supported"
    if function == "reverse_proxy_cdn":
        return "not_supported" if kind == "dmca_512d" else "forwarding_only"
    # A hosting function.
    if kind == "forwarding_only":
        return "forwarding_only"
    return "primary" if kind in HOST_CHANNELS else "not_supported"


#: Channel kinds in order of preference, per nexus. A published legal channel beats a form, a form
#: beats a mailbox. Only an ordering for a person to start from — never a legal conclusion.
_KIND_PREFERENCE: dict[str, tuple[str, ...]] = {
    "eu_facing": (
        "dsa_art16",
        "provider_copyright_policy",
        "provider_copyright_policy_dmca_style",
        "dmca_512c",
        "dmca_512d",
        "web_form",
        "abuse_email",
        "forwarding_only",
    ),
    "us_nexus": (
        "dmca_512c",
        "dmca_512d",
        "provider_copyright_policy",
        "provider_copyright_policy_dmca_style",
        "dsa_art16",
        "web_form",
        "abuse_email",
        "forwarding_only",
    ),
    "other": (
        "provider_copyright_policy",
        "provider_copyright_policy_dmca_style",
        "dsa_art16",
        "dmca_512c",
        "dmca_512d",
        "web_form",
        "abuse_email",
        "forwarding_only",
    ),
}


@dataclass(frozen=True)
class ChannelChoice:
    """What `suggest_channel` needs to rank one channel."""

    kind: str
    support: V1Support
    from_directory: bool
    recheck_due: bool


def suggest_channel(channels: Sequence[ChannelChoice], nexus: str) -> int | None:
    """Index of the channel to start from, or None when no channel is usable at all.

    By v1 support first, then the kind preference for the nexus, a directory channel before the
    RDAP fallback, and a freshly checked one before one due for re-check.
    """
    preference = _KIND_PREFERENCE.get(nexus, _KIND_PREFERENCE["other"])
    usable = [
        (index, channel)
        for index, channel in enumerate(channels)
        if channel.support != "not_supported"
    ]
    if not usable:
        return None

    def rank(item: tuple[int, ChannelChoice]) -> tuple[int, int, bool, bool, int]:
        index, channel = item
        kind_rank = (
            preference.index(channel.kind) if channel.kind in preference else len(preference)
        )
        return (
            V1_SUPPORT_ORDER.index(channel.support),
            kind_rank,
            not channel.from_directory,
            channel.recheck_due,
            index,
        )

    return min(usable, key=rank)[0]


def needs_recheck(checked_at: datetime, now: datetime, days: int = DEFAULT_RECHECK_DAYS) -> bool:
    return now - checked_at >= timedelta(days=days)


#: Words that make a mailbox a role address. Long words match inside a token ("legalteam"), short
#: ones only as a whole token (so "ip" does not match "philip").
ROLE_WORDS_LONG = (
    "abuse",
    "dmca",
    "copyright",
    "legal",
    "notice",
    "takedown",
    "removal",
    "report",
    "complaint",
    "compliance",
    "trust",
    "safety",
    "security",
    "support",
    "helpdesk",
    "info",
    "contact",
    "hostmaster",
    "postmaster",
    "webmaster",
    "admin",
    "privacy",
    "policy",
    "infringement",
)
ROLE_WORDS_SHORT = frozenset({"dsa", "ip", "ipr", "noc", "law", "tos", "aup", "help"})


def is_role_address(email: str) -> bool:
    """Does this mailbox name a function (abuse@, dmca-notices@), not a person?

    A guard, not a proof: the directory stores professional role addresses only, never a named
    abuse officer's personal data. An address without any role word is refused; staff can still
    find the role address the provider publishes.
    """
    local, at, domain = email.strip().lower().rpartition("@")
    if not at or not local or not domain:
        return False
    tokens = [token for token in re.split(r"[^a-z0-9]+", local) if token]
    return any(
        token in ROLE_WORDS_SHORT or any(word in token for word in ROLE_WORDS_LONG)
        for token in tokens
    )


LANGUAGE_LIST = re.compile(r"^[a-z]{2}(,[a-z]{2})*$")


def parse_languages(value: str) -> list[str] | None:
    """ "en, de" → ["en", "de"]; "" → []; None when it is not a list of ISO 639-1 codes."""
    compact = value.replace(" ", "").lower()
    if not compact:
        return []
    if not LANGUAGE_LIST.match(compact):
        return None
    return list(dict.fromkeys(compact.split(",")))


def hostname_suffixes(hostname: str) -> list[str]:
    """`a.b.example.com` → itself and every parent domain with at least two labels."""
    labels = hostname.split(".")
    return [".".join(labels[index:]) for index in range(max(len(labels) - 1, 1))]
