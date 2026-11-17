from collections.abc import Callable

import pytest
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from billing.models import BillingProfile, Plan
from billing.tests.helpers import SELLER


@pytest.fixture
def seller(settings: Settings) -> Settings:
    for name, value in SELLER.items():
        setattr(settings, name, value)
    return settings


@pytest.fixture
def make_customer(db: None) -> Callable[..., Organisation]:
    def _make(
        name: str = "Studio",
        *,
        country: str = "DE",
        is_business: bool = True,
        vat_id: str = "",
    ) -> Organisation:
        organisation = Organisation.objects.create(name=name)
        BillingProfile.objects.create(
            organisation=organisation,
            legal_name=f"{name} GmbH",
            address="Example Road 2\n54321 Example Town",
            country=country,
            is_business=is_business,
            vat_id=vat_id,
        )
        return organisation

    return _make


@pytest.fixture
def monthly_plan(db: None) -> Plan:
    return Plan.objects.create(
        code="professional-monthly",
        name="Marceline Professional (monthly)",
        interval=Plan.Interval.MONTH,
        price_net_cents=10_000,
    )
