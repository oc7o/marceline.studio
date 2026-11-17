"""Which VAT an invoice carries. Pure: no database, no settings. Not tax advice.

Three supported cases, everything else refused with a code (`VatNotSupported`):

| Seller | Buyer | Treatment | Rate |
|---|---|---|---|
| small business (§ 19 UStG) | in Germany | `small_business` | 0 % |
| regular | in Germany (business or consumer) | `domestic` | 19 % |
| regular | EU business outside Germany, with a VAT ID of its country | `reverse_charge` | 0 % |

Refused on purpose, so nobody invoices them by accident:
- a **consumer outside Germany** (`consumer_outside_germany`): EU B2C digital services are taxed
  where the consumer lives, via the One-Stop-Shop — out of scope until it is built;
- a **business in the EU without a VAT ID** (`vat_id_missing`) or with one of another country or
  an impossible shape (`vat_id_invalid`): the reverse charge needs the customer's VAT ID;
- a **buyer outside the EU** (`outside_eu`): place-of-supply rules not built;
- reverse charge **without the seller's own VAT ID** (`seller_vat_id_missing`): § 14a UStG requires
  both on the invoice;
- a **small business selling abroad** (`small_business_cross_border`): confirm the tax treatment first.

A VAT ID is checked for shape and country only. Whether it is *valid* (VIES / BZSt confirmation)
is not checked here — that is a network call and a documented follow-up (ADR 0032).
"""

import re
from dataclasses import dataclass
from typing import Literal

DOMESTIC_COUNTRY = "DE"
DOMESTIC_RATE_BP = 1900

# EU member states by ISO 3166-1 alpha-2 code.
EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
        "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)  # fmt: skip
# VAT IDs use the ISO code except for Greece.
VAT_PREFIX = {"GR": "EL"}
VAT_ID_SHAPE = re.compile(r"^[A-Z]{2}[0-9A-Z+*]{2,12}$")

Treatment = Literal["domestic", "reverse_charge", "small_business"]
RefusalCode = Literal[
    "consumer_outside_germany",
    "vat_id_missing",
    "vat_id_invalid",
    "outside_eu",
    "seller_vat_id_missing",
    "small_business_cross_border",
]


class VatNotSupported(Exception):
    """This buyer cannot be invoiced (yet). `code` says why, for the admin and the command."""

    def __init__(self, code: RefusalCode) -> None:
        super().__init__(code)
        self.code: RefusalCode = code


@dataclass(frozen=True)
class VatDecision:
    treatment: Treatment
    rate_bp: int


def normalise_vat_id(value: str) -> str:
    """Upper case without spaces, dots or hyphens: "de 123.456.789" -> "DE123456789"."""
    return re.sub(r"[\s.\-]", "", value).upper()


def vat_id_fits_country(vat_id: str, country: str) -> bool:
    """The (normalised) VAT ID has a plausible shape and belongs to `country`."""
    prefix = VAT_PREFIX.get(country, country)
    return bool(VAT_ID_SHAPE.match(vat_id)) and vat_id.startswith(prefix)


def decide(
    *,
    seller_small_business: bool,
    seller_vat_id: str,
    buyer_country: str,
    buyer_is_business: bool,
    buyer_vat_id: str,
) -> VatDecision:
    """The VAT treatment for one invoice, or `VatNotSupported`."""
    country = buyer_country.upper()
    if country == DOMESTIC_COUNTRY:
        if seller_small_business:
            return VatDecision("small_business", 0)
        return VatDecision("domestic", DOMESTIC_RATE_BP)
    if seller_small_business:
        raise VatNotSupported("small_business_cross_border")
    if country not in EU_COUNTRIES:
        raise VatNotSupported("outside_eu")
    if not buyer_is_business:
        raise VatNotSupported("consumer_outside_germany")
    vat_id = normalise_vat_id(buyer_vat_id)
    if not vat_id:
        raise VatNotSupported("vat_id_missing")
    if not vat_id_fits_country(vat_id, country):
        raise VatNotSupported("vat_id_invalid")
    if not normalise_vat_id(seller_vat_id):
        raise VatNotSupported("seller_vat_id_missing")
    return VatDecision("reverse_charge", 0)


def vat_cents(net_cents: int, rate_bp: int) -> int:
    """VAT on a net total, rounded half away from zero to the cent (so a cancellation mirrors)."""
    magnitude = (abs(net_cents) * rate_bp + 5_000) // 10_000
    return magnitude if net_cents >= 0 else -magnitude
