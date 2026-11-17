"""Which notice regime a hosting country implies. Pure; the mapping is code and is tested.

- `eu` — the host sits in the European Union: a notice under DSA Art. 16, with the statutory duty
  to act on it.
- `us` — the United States and its territories: DMCA §512 notice and takedown, with counter-notice
  risk.
- `other` — any other country: no regime we can name for the customer; cooperation varies.
- `unknown` — no country, or a registry placeholder that is not a country.

The class follows the country of the **IP allocation**. Behind a CDN that is the CDN's country,
not the origin's: the CDN is who a notice goes to, and `cdn` on the record says the origin is
hidden (ADR 0027).
"""

from typing import Literal

Jurisdiction = Literal["eu", "us", "other", "unknown"]

#: The 27 member states, plus territories that are part of the Union under their own ISO code:
#: Åland (FI) and the outermost regions French Guiana, Guadeloupe, Martinique, Mayotte, Réunion
#: and Saint-Martin (FR). Overseas countries and territories (e.g. New Caledonia) are not EU.
EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
        "AX", "GF", "GP", "MQ", "YT", "RE", "MF",
    }
)  # fmt: skip
#: The United States and the territories where federal copyright law, and so §512, applies.
US_COUNTRIES = frozenset({"US", "PR", "GU", "VI", "AS", "MP"})
#: Registry codes that name a region or nothing at all. RIPE uses "EU" for pan-European space,
#: which says nothing about where the machine is.
NOT_A_COUNTRY = frozenset({"EU", "AP", "ZZ", "XX", "QO"})


def jurisdiction_of(country: str | None) -> Jurisdiction:
    code = (country or "").strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha() or code in NOT_A_COUNTRY:
        return "unknown"
    if code in EU_COUNTRIES:
        return "eu"
    if code in US_COUNTRIES:
        return "us"
    return "other"
