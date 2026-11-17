"""Pure Telegram link parsing for channel candidates; no I/O, never fetches Telegram."""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

TELEGRAM_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})
# First path segments that are Telegram features or official pages, not channel usernames.
RESERVED_PATHS = frozenset(
    {
        "a",  # web client
        "addemoji",
        "addlist",  # shared chat folder links
        "addstickers",
        "addtheme",
        "apps",
        "bg",  # chat backgrounds
        "blog",
        "boost",
        "c",  # private channel post links (t.me/c/<internal id>/<post>)
        "confirmphone",
        "contact",
        "dl",
        "faq",
        "giftcode",
        "invoice",
        "iv",  # instant view
        "k",  # web client
        "login",
        "m",  # business chat links
        "nft",
        "premium",
        "privacy",
        "proxy",
        "setlanguage",
        "share",
        "socks",
        "telegram",  # official channel
        "tos",
        "web",
        "z",  # web client
    }
)
INVITE_PREFIX = "joinchat"
USERNAME_RE = re.compile(r"[a-z][a-z0-9_]{3,31}")
INVITE_CODE_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
POST_ID_RE = re.compile(r"[0-9]{1,12}")


@dataclass(frozen=True)
class TelegramLink:
    """A public channel username, or the SHA-256 of a private invite code (never the code)."""

    private_invite: bool
    identity: str
    post_id: int | None = None


def hash_invite(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def parse_telegram_link(value: str) -> TelegramLink | None:
    """Parse t.me / telegram.me channel, /s/ preview and invite links; None for anything else.

    Usernames are lowercased (Telegram usernames are case-insensitive); invite codes are
    case-sensitive and are hashed as-is. Bots (usernames ending in "bot") and RESERVED_PATHS
    are ignored. Query strings and fragments are dropped.
    """
    try:
        parts = urlsplit(value.strip() if "://" in value else "//" + value.strip())
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if parts.scheme not in {"", "http", "https"} or host not in TELEGRAM_HOSTS:
        return None
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    if not segments:
        return None
    first = segments[0]
    if first.startswith("+") or first.lower() == INVITE_PREFIX:
        code = first[1:] if first.startswith("+") else (segments[1] if len(segments) > 1 else "")
        if not INVITE_CODE_RE.fullmatch(code):
            return None
        return TelegramLink(private_invite=True, identity=hash_invite(code))
    if first.lower() == "s":
        segments = segments[1:]
        if not segments:
            return None
    username = segments[0].lower()
    if (
        not USERNAME_RE.fullmatch(username)
        or username in RESERVED_PATHS
        or username.endswith("bot")
        or username.endswith("_")
        or "__" in username
    ):
        return None
    post_id = None
    if len(segments) > 1 and POST_ID_RE.fullmatch(segments[1]):
        post_id = int(segments[1])
    return TelegramLink(private_invite=False, identity=username, post_id=post_id)
