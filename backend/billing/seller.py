"""Who issues the invoices: read from settings (environment), never from the repository.

Name, address, tax number and bank account are deployment data (issue #35) and must never be
committed. `seller_problems` lists what is missing as codes, and
issuing refuses while the list is not empty — a development box without the variables cannot issue
an invoice at all.
"""

import re
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings

from billing.vat import normalise_vat_id


@dataclass(frozen=True)
class Seller:
    name: str
    address: tuple[str, ...]
    tax_number: str
    vat_id: str
    email: str
    iban: str
    bic: str
    bank: str
    # None while the operator has not decided (BILLING_SMALL_BUSINESS unset).
    small_business: bool | None

    def snapshot(self) -> dict[str, Any]:
        """What an issued invoice keeps (`Invoice.seller`)."""
        data = asdict(self)
        data["address"] = list(self.address)
        return data


def _bool_or_none(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def normalise_iban(value: str) -> str:
    return re.sub(r"\s", "", value).upper()


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 shape and mod-97 check digits."""
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


def seller_from_settings() -> Seller:
    address = tuple(
        line.strip() for line in str(settings.BILLING_SELLER_ADDRESS).split("|") if line.strip()
    )
    return Seller(
        name=str(settings.BILLING_SELLER_NAME).strip(),
        address=address,
        tax_number=str(settings.BILLING_SELLER_TAX_NUMBER).strip(),
        vat_id=normalise_vat_id(str(settings.BILLING_SELLER_VAT_ID)),
        email=str(settings.BILLING_SELLER_EMAIL).strip(),
        iban=normalise_iban(str(settings.BILLING_SELLER_IBAN)),
        bic=str(settings.BILLING_SELLER_BIC).strip().upper(),
        bank=str(settings.BILLING_SELLER_BANK).strip(),
        small_business=_bool_or_none(str(settings.BILLING_SMALL_BUSINESS)),
    )


def seller_problems(seller: Seller) -> list[str]:
    """Everything an invoice needs from the seller and does not have, by setting name."""
    problems = []
    if not seller.name:
        problems.append("BILLING_SELLER_NAME missing")
    if not seller.address:
        problems.append("BILLING_SELLER_ADDRESS missing")
    # § 14 (4) Nr. 2 UStG: the tax number or the VAT ID.
    if not seller.tax_number and not seller.vat_id:
        problems.append("BILLING_SELLER_TAX_NUMBER or BILLING_SELLER_VAT_ID missing")
    if not seller.iban:
        problems.append("BILLING_SELLER_IBAN missing")
    elif not iban_is_valid(seller.iban):
        problems.append("BILLING_SELLER_IBAN invalid")
    if seller.small_business is None:
        problems.append("BILLING_SMALL_BUSINESS not decided (true or false)")
    return problems
