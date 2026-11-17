"""Licensed partners: normalisation and matching a URL against them (ADR 0034)."""

import pytest
from django.contrib.auth.models import User

from accounts.models import Organisation
from marceline.rights import services
from marceline.rights.partners import (
    InvalidDomainError,
    host_matches,
    normalise_domain,
    normalise_path_prefix,
    partner_for_url,
    path_matches,
)
from marceline.tests.app_helpers import add_member, make_organisation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("https://www.Example.com:8443/path?q=1", "example.com"),
        ("shop.example.com.", "shop.example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_domains_are_normalised(raw: str, expected: str) -> None:
    assert normalise_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "localhost", "not a domain", "exa mple.com", "http://"])
def test_non_domains_are_refused(raw: str) -> None:
    with pytest.raises(InvalidDomainError):
        normalise_domain(raw)


def test_a_domain_covers_its_subdomains_only() -> None:
    assert host_matches("example.com", "example.com")
    assert host_matches("cdn.example.com", "example.com")
    assert not host_matches("badexample.com", "example.com")
    assert not host_matches("example.com.evil.test", "example.com")


def test_a_path_prefix_matches_whole_segments() -> None:
    assert normalise_path_prefix(" studio/ ") == "/studio"
    assert path_matches("/studio", "/studio")
    assert path_matches("/Studio/videos/1", "/studio")
    assert not path_matches("/studio-two", "/studio")
    assert path_matches("/anything", "")


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


def test_partner_for_url_finds_the_most_specific_partner(
    organisation: Organisation, member: User
) -> None:
    actor = services.Actor.customer(member)
    platform = services.add_partner(
        organisation,
        services.PartnerInput(kind="platform", name="Clip platform", domain="clips.example"),
        actor,
    )
    storefront = services.add_partner(
        organisation,
        services.PartnerInput(
            kind="platform", name="Our store", domain="clips.example", path_prefix="/our-studio"
        ),
        actor,
    )
    services.add_partner(
        organisation, services.PartnerInput(kind="distributor", name="DVD Co"), actor
    )
    other = make_organisation("Other")
    services.add_partner(
        other,
        services.PartnerInput(kind="domain", name="Their site", domain="theirs.example"),
        services.Actor.customer(add_member(other, "other@example.test")),
    )

    assert partner_for_url(organisation, "https://clips.example/our-studio/video/9") == storefront
    assert partner_for_url(organisation, "https://www.clips.example/someone-else") == platform
    assert partner_for_url(organisation, "clips.example") == platform
    assert partner_for_url(organisation, "https://theirs.example/x") is None
    assert partner_for_url(organisation, "https://pirate.example/clips.example") is None
    assert partner_for_url(organisation, "not a url") is None


@pytest.mark.parametrize(
    ("partner", "code"),
    [
        (services.PartnerInput(kind="domain", name="No domain"), "partner_domain_required"),
        (
            services.PartnerInput(kind="affiliate", name=" ", domain="a.example"),
            "partner_name_required",
        ),
        (services.PartnerInput(kind="reseller", name="X"), "unknown_partner_kind"),
        (
            services.PartnerInput(kind="distributor", name="X", path_prefix="/x"),
            "partner_domain_required",
        ),
    ],
)
def test_incomplete_partners_are_refused(
    organisation: Organisation, member: User, partner: services.PartnerInput, code: str
) -> None:
    with pytest.raises(services.RightsError) as error:
        services.add_partner(organisation, partner, services.Actor.customer(member))

    assert error.value.code == code
