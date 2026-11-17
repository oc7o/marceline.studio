"""Recognise block, challenge, login and age-gate pages so the crawler skips them.

Age gates come in two kinds (ADR 0007, amendment 2026-09-15):

- `age_self_declaration`: an "I am old enough" click-through that withholds the page until the visitor
  confirms. The crawler may send the site's own confirmation cookie only for sites the operator
  approved (`catalog.Site.age_confirmation`); if the gate is still shown, the page is skipped.
- `age_verification_provider`: real age verification (third-party providers, ID/selfie/card
  checks). Always skipped, never automated, with or without approval.

Client-side overlays on pages whose server already sends the full HTML are not gates at all.
"""

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

CLOUDFLARE_BLOCK_MARKERS = (
    "used Cloudflare to restrict access",
    "Attention Required! | Cloudflare",
    'id="cf-error-details"',
)
CHALLENGE_MARKERS = (
    "Just a moment...",
    "challenge-platform",
    "cf-turnstile",
    "g-recaptcha",
    "h-captcha",
)
BLOCK_STATUSES = frozenset({401, 403, 429, 503})
LOGIN_PATH_PREFIXES = ("/login", "/signin", "/sign-in", "/signup", "/auth/")

AGE_SELF_DECLARATION = "age_self_declaration"
AGE_VERIFICATION_PROVIDER = "age_verification_provider"

# Known third-party age-verification services, by the domains their scripts, iframes and redirects
# load from. A page referencing any of them is verification, not a click-through.
AGE_VERIFICATION_PROVIDERS: Mapping[str, tuple[str, ...]] = {
    "AgeGate": ("agegate.example",),
    "Yoti": ("yoti.com", "yotiapp.com"),
    "VerifyMy": ("verifymy.io", "verifymyage.com", "verifymyage.co.uk"),
    "AgeChecked": ("agechecked.com",),
    "ok.cam": ("ok.cam",),
    "AgeKey": ("agekey.example",),
    "AgePass": ("agepass.example",),
    "Veriff": ("veriff.com", "veriff.me"),
    "Ondato": ("ondato.com",),
    "Persona": ("withpersona.com",),
}
# Verification methods without a named provider; only counted on pages with an age marker.
AGE_VERIFICATION_METHOD_MARKERS = (
    "upload your id",
    "upload a photo of your id",
    "government-issued id",
    "government issued id",
    "id document",
    "take a selfie",
    "selfie verification",
    "facial age estimation",
    "age estimation",
    "verify with your credit card",
    "credit card verification",
    "verify with a credit card",
)
# Click-through wording, markup and the cookie names sites set when "yes" is clicked.
AGE_GATE_MARKERS = (
    "age-gate",
    "age_gate",
    "agegate",
    "age-verification",
    "age_verification",
    "age verification",
    "verify your age",
    "confirm your age",
    "are you 18",
    "are you over 18",
    "i am 18",
    "i am over 18",
    "i'm 18",
    "18 years or older",
    "18 years of age",
    "adults only",
    "popup_age",
    "age-modal",
    "agemodal",
    "age-check",
)
AGE_COOKIE_NAMES = (
    "age_verified",
    "ageverified",
    "age_confirmed",
    "ageconfirm",
    "kt_agecheck",
    "agecheck",
    "over18",
    "is_adult",
)
# A self-declaration gate withholds the page: it has an age marker and almost no links. Pages with
# more links already carry the content (a client-side overlay, a footer notice) and are not gates.
MAX_GATE_PAGE_LINKS = 20

_LINK = re.compile(r"<a\s[^>]*href", re.IGNORECASE)
_PASSWORD_INPUT = re.compile(r"<input\s[^>]*type\s*=\s*[\"']?password", re.IGNORECASE)
_PROVIDER_PATTERNS = {
    name: re.compile(
        "|".join(rf"(?<![a-z0-9-]){re.escape(domain)}(?![a-z0-9-])" for domain in domains)
    )
    for name, domains in AGE_VERIFICATION_PROVIDERS.items()
}


def detect_block(
    status: int,
    headers: Mapping[str, str],
    body: str,
    url: str,
    *,
    link_count: int | None = None,
) -> str | None:
    """Return a short reason when the response is a block/challenge/login/age gate, else None.

    Age-gate reasons start with `AGE_SELF_DECLARATION` or `AGE_VERIFICATION_PROVIDER`. They are
    only reported for non-error responses; 4xx/5xx keep their status handling.

    `link_count`, when given, overrides the `<a href>` count `classify_age_gate` would otherwise
    take from `body` (issue #78): a caller that hands over a truncated document — the headless
    capture path caps what it ships to Python — but has counted links over the whole page passes
    that count here instead of letting a truncated body undercount them. The HTTP crawler path
    passes the full body and no override, so its behaviour is unchanged.
    """
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return "Cloudflare challenge"
    head = body[:50_000]
    if status in BLOCK_STATUSES:
        if any(marker in head for marker in CLOUDFLARE_BLOCK_MARKERS):
            return f"Cloudflare block (HTTP {status})"
        if any(marker in head for marker in CHALLENGE_MARKERS):
            return f"challenge or CAPTCHA page (HTTP {status})"
    if urlsplit(url).path.lower().startswith(LOGIN_PATH_PREFIXES):
        return "redirected to a login page"
    if status < 400:
        return classify_age_gate(body, link_count=link_count)
    return None


def classify_age_gate(body: str, *, link_count: int | None = None) -> str | None:
    """`age_verification_provider: <name>`, `age_self_declaration`, or None (no gate).

    `link_count` overrides the count derived from `body` — see `detect_block`.
    """
    lower = body.lower()
    for name, pattern in _PROVIDER_PATTERNS.items():
        if pattern.search(lower):
            return f"{AGE_VERIFICATION_PROVIDER}: {name}"
    if not any(marker in lower for marker in AGE_GATE_MARKERS + AGE_COOKIE_NAMES):
        return None
    links = link_count if link_count is not None else len(_LINK.findall(lower))
    if links > MAX_GATE_PAGE_LINKS:
        return None
    if any(marker.lower() in lower for marker in CHALLENGE_MARKERS):
        return None  # a CAPTCHA, not an age gate: reported as a challenge
    if _PASSWORD_INPUT.search(lower):
        return f"{AGE_VERIFICATION_PROVIDER}: login-based check"
    if any(marker in lower for marker in AGE_VERIFICATION_METHOD_MARKERS):
        return f"{AGE_VERIFICATION_PROVIDER}: ID/selfie/credit-card check"
    return AGE_SELF_DECLARATION
