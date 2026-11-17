"""The supported VAT cases, and the refused ones staying refused."""

import pytest

from billing.vat import VatDecision, VatNotSupported, decide, normalise_vat_id, vat_cents


def _decide(
    *,
    small_business: bool = False,
    seller_vat_id: str = "DE000000000",
    country: str = "DE",
    business: bool = True,
    vat_id: str = "",
) -> VatDecision:
    return decide(
        seller_small_business=small_business,
        seller_vat_id=seller_vat_id,
        buyer_country=country,
        buyer_is_business=business,
        buyer_vat_id=vat_id,
    )


def test_german_business_pays_19_percent() -> None:
    assert _decide() == VatDecision("domestic", 1900)


def test_german_consumer_pays_19_percent_too() -> None:
    assert _decide(business=False) == VatDecision("domestic", 1900)


def test_eu_business_with_vat_id_gets_reverse_charge() -> None:
    assert _decide(country="AT", vat_id="ATU12345678") == VatDecision("reverse_charge", 0)


def test_greek_vat_ids_use_el() -> None:
    assert _decide(country="GR", vat_id="EL123456789").treatment == "reverse_charge"
    with pytest.raises(VatNotSupported) as refused:
        _decide(country="GR", vat_id="GR123456789")
    assert refused.value.code == "vat_id_invalid"


def test_vat_ids_are_normalised_before_checking() -> None:
    assert normalise_vat_id(" at u-123.456 78 ") == "ATU12345678"
    assert _decide(country="AT", vat_id="at u123 45678").treatment == "reverse_charge"


def test_small_business_charges_no_vat_in_germany() -> None:
    assert _decide(small_business=True, business=False) == VatDecision("small_business", 0)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        # EU consumers would need OSS: explicitly out of scope.
        ({"country": "FR", "business": False}, "consumer_outside_germany"),
        (
            {"country": "FR", "business": False, "vat_id": "FR12345678901"},
            "consumer_outside_germany",
        ),
        ({"country": "NL"}, "vat_id_missing"),
        ({"country": "NL", "vat_id": "DE123456789"}, "vat_id_invalid"),
        ({"country": "NL", "vat_id": "NL1"}, "vat_id_invalid"),
        ({"country": "CH", "vat_id": "CHE123456789"}, "outside_eu"),
        ({"country": "US"}, "outside_eu"),
        ({"country": "AT", "vat_id": "ATU12345678", "seller_vat_id": ""}, "seller_vat_id_missing"),
        (
            {"country": "AT", "vat_id": "ATU12345678", "small_business": True},
            "small_business_cross_border",
        ),
    ],
)
def test_unsupported_cases_are_refused_with_a_code(kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(VatNotSupported) as refused:
        _decide(**kwargs)  # type: ignore[arg-type]
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("net", "vat"),
    [(10_000, 1_900), (14_900, 2_831), (1, 0), (3, 1), (5_263, 1_000), (0, 0)],
)
def test_vat_is_rounded_half_up_to_the_cent(net: int, vat: int) -> None:
    assert vat_cents(net, 1900) == vat


def test_vat_on_a_negative_amount_mirrors_the_positive_one() -> None:
    for net in (1, 3, 5_263, 14_900, 99_999):
        assert vat_cents(-net, 1900) == -vat_cents(net, 1900)
