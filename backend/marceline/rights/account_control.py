"""Proof of control of a creator's own platform account (ADR 0055): the pure half.

Identity is established by **control of the account whose content we protect**, not
by a document: we show the creator a one-time code, they place it for a short time in the bio of
that account (or send it from it by direct message), and a staff member looks at that public
profile **by hand, in their own browser** and records what they saw. No identity document and no date
of birth is ever stored (operator decision 2026-09-19; `docs/adr/0055-…`).

Everything here is a pure rule — the platform list, the shape of a claimed profile, the code's
alphabet and its lifetime. Nothing in this module, in `models.py` or in `services.py` fetches
anything: **a claimed profile URL is text we show to staff, never a URL our code opens.** The
creator writes that string; a crawler, an HTTP client or a preview fetcher behind it would be a
fetch instruction written by a stranger, exactly the hole `crawler/CLAUDE.md` keeps shut for page
content. `marceline/tests/test_account_control.py::test_no_http_client_is_imported` fails if any of
the three modules grows an HTTP import.

The host allowlist is therefore **not** a security boundary for a request that never happens. It is
a typo and confusion guard: it keeps a link to a piracy mirror, a shortener or an unrelated site
out of the field a staff member is about to open by hand.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

#: Every code is prefixed, so a creator who finds it in their own bio weeks later knows what it was
#: and staff recognise it at a glance in a profile full of other text.
CODE_PREFIX = "marceline-"
#: Unambiguous by construction: no 0/O, no 1/I/L, no 5/S, no 8/B. A creator types this on a phone
#: and a staff member compares it by eye — the two failure modes are a typo and a misread glyph.
CODE_ALPHABET = "234679ACDEFGHJKMNPQRTUVWXYZ"
CODE_LENGTH = 8
#: 27**8 ≈ 2.8e11. The code is a nonce, not a credential: it grants nothing, it only has to be
#: unguessable enough that nobody stumbles on a live one by accident.
CODE_TTL = timedelta(hours=72)
CODE_PATTERN = re.compile(f"^{CODE_PREFIX}[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")

#: A handle as we store it: the platform's own characters, no leading "@", no spaces.
HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._\-]{2,64}$")
MAX_HANDLE = 64
MAX_PROFILE_URL = 300


class InvalidClaimError(Exception):
    """A claimed platform account we refuse to store, with a machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Platform:
    """One creator platform: the code we store, the label staff read, its own hosts.

    `hosts` are the only hosts a claimed profile URL may name. They are the platforms' own
    published addresses; a subdomain of one is accepted (`www.`), a look-alike is not.

    `checks_identity` is the whole reason a placed code proves anything (ADR 0055): these platforms
    check who a creator is **before** they may publish. Control of such
    an account therefore carries that fact. Where no platform stands behind the account — the
    creator's own website, or a platform we have not put on this list — a code in a bio proves only
    that somebody controls that page, so **the code cannot establish identity there**: those two
    take the video-call method alone, in the service and in a database constraint.
    """

    code: str
    label: str
    hosts: tuple[str, ...]
    checks_identity: bool = True


#: The fixed list. A platform is added here deliberately, with its hosts — never by free text,
#: because "which platform" is the fact the whole check rests on. `other_platform` exists for the
#: account that is somewhere else: it takes a handle and **no URL**, and staff ask for the address
#: in the conversation they are already having.
PLATFORMS: tuple[Platform, ...] = (
    Platform("fanhub", "FanHub", ("fanhub.example",)),
    Platform("fanbase", "Fanbase", ("fanbase.example",)),
    Platform("clipmarket", "ClipMarket", ("clipmarket.example",)),
    Platform("fancentral", "FanCentral", ("fancentral.example",)),
    Platform("loyalclub", "LoyalClub", ("loyalclub.example",)),
    Platform("patreon", "Patreon", ("patreon.com",)),
    Platform("instagram", "Instagram", ("instagram.com",)),
    Platform("x", "X", ("x.com", "twitter.com")),
    Platform("reddit", "Reddit", ("reddit.com",)),
    Platform("tiktok", "TikTok", ("tiktok.com",)),
    Platform("own_website", "the creator's own website", (), checks_identity=False),
    Platform("other_platform", "another platform (named in the note)", (), checks_identity=False),
)
PLATFORM_CODES: tuple[str, ...] = tuple(platform.code for platform in PLATFORMS)
PLATFORM_CHOICES: list[tuple[str, str]] = [(p.code, p.label) for p in PLATFORMS]
#: The platforms where a placed code establishes nothing about identity (see `Platform`). Sorted,
#: because a database constraint is generated from it and a set's order is not stable.
VIDEO_CALL_ONLY: tuple[str, ...] = tuple(
    sorted(platform.code for platform in PLATFORMS if not platform.checks_identity)
)
_BY_CODE = {platform.code: platform for platform in PLATFORMS}


def checks_identity(platform_code: str) -> bool:
    """Whether the platform itself checks identity before somebody may publish there."""
    return platform_for(platform_code).checks_identity


def platform_for(code: str) -> Platform:
    platform = _BY_CODE.get(code)
    if platform is None:
        raise InvalidClaimError("unknown_platform")
    return platform


def new_code() -> str:
    """A fresh one-time code. `secrets`, never `random`."""
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{CODE_PREFIX}{body}"


def looks_like_code(value: str) -> bool:
    """Whether a string has the shape of one of our codes. Says nothing about whether it is live."""
    return CODE_PATTERN.match(value) is not None


def expires_at(issued_at: datetime) -> datetime:
    return issued_at + CODE_TTL


def normalise_handle(raw: str) -> str:
    """The handle as we store it, or `invalid_handle`.

    A creator pastes "@name", " name " or a whole URL. The first two are the same handle; the third
    is refused, because a handle field holding a URL is exactly the confusion the separate URL field
    exists to avoid.
    """
    handle = raw.strip().lstrip("@").strip()
    if handle.endswith("/"):
        handle = handle[:-1]
    if not HANDLE_PATTERN.match(handle):
        raise InvalidClaimError("invalid_handle")
    return handle


def _host_of(url: str) -> str:
    """The host of an `https://` URL, lowercased, without credentials or port.

    Hand-parsed on purpose: `urllib.parse` is in the standard library next to `urllib.request`, and
    this module must not import anything that can open a socket (see the module docstring). The
    shapes we refuse outright are the ones that make a host ambiguous.
    """
    rest = url[len("https://") :]
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host or ":" in host or host == "":
        raise InvalidClaimError("invalid_profile_url")
    return host.lower().rstrip(".")


def checked_profile_url(platform_code: str, raw: str) -> str:
    """The profile URL as we store it: `https://`, one of this platform's own hosts, or refused.

    Empty stays empty — a URL is optional, and `own_website` / `other_platform` have no host list,
    so for them it is the only accepted value. **Nothing ever opens the result.**
    """
    url = raw.strip()
    if url == "":
        return ""
    platform = platform_for(platform_code)
    if not platform.hosts:
        raise InvalidClaimError("url_not_accepted_for_platform")
    if len(url) > MAX_PROFILE_URL or not url.startswith("https://"):
        raise InvalidClaimError("invalid_profile_url")
    if any(character.isspace() for character in url) or "\\" in url:
        raise InvalidClaimError("invalid_profile_url")
    host = _host_of(url)
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in platform.hosts):
        raise InvalidClaimError("host_not_allowed")
    return url
