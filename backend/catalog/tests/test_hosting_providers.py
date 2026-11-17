"""Provider function, nexus, channel support and suggestion (ADR 0027 amendment 2026-09-17)."""

from datetime import UTC, datetime, timedelta

import pytest

from catalog.hosting.providers import (
    CHANNEL_KINDS,
    NEXUSES,
    PROVIDER_FUNCTIONS,
    TRANSFER_BASES,
    ChannelChoice,
    derive_function,
    hostname_suffixes,
    is_role_address,
    needs_recheck,
    nexus_of_country,
    parse_languages,
    resolve_nexus,
    suggest_channel,
    v1_support,
)
from catalog.models import (
    ComplaintChannel,
    ProviderFunction,
    ProviderFunctionOverride,
    ProviderNexus,
)


def test_model_choices_match_the_policy_codes() -> None:
    assert tuple(ProviderFunction.values) == PROVIDER_FUNCTIONS
    assert tuple(ProviderNexus.values) == NEXUSES
    assert tuple(ComplaintChannel.Kind.values) == CHANNEL_KINDS
    assert tuple(ComplaintChannel.TransferBasis.values) == TRANSFER_BASES
    assert tuple(ProviderFunctionOverride.Slot.values) == ("service", "network", "registrar")


@pytest.mark.parametrize(
    ("country", "expected"),
    [("DE", "eu_facing"), ("US", "us_nexus"), ("CH", "other"), ("EU", "unknown"), ("", "unknown")],
)
def test_nexus_of_country_is_a_location_hint(country: str, expected: str) -> None:
    assert nexus_of_country(country) == expected


def test_nexus_prefers_staff_then_establishment_then_allocation() -> None:
    assert resolve_nexus(
        staff_nexus="eu_facing", establishment_country="US", allocation_country="US"
    ) == ("eu_facing", "directory")
    assert resolve_nexus(staff_nexus="", establishment_country="US", allocation_country="NL") == (
        "us_nexus",
        "establishment_country",
    )
    assert resolve_nexus(staff_nexus="", establishment_country="", allocation_country="NL") == (
        "eu_facing",
        "allocation_country",
    )
    assert resolve_nexus(staff_nexus="", establishment_country="", allocation_country="") == (
        "unknown",
        "none",
    )


def test_function_is_derived_where_mechanical() -> None:
    assert derive_function("registrar", cdn="", override="", directory_default="") == (
        "registrar",
        "derived",
    )
    assert derive_function("network", cdn="cloudflare", override="", directory_default="") == (
        "reverse_proxy_cdn",
        "derived",
    )
    assert derive_function("network", cdn="", override="", directory_default="") == (
        "origin_host",
        "derived",
    )
    assert derive_function("service", cdn="", override="", directory_default="") == (
        "unknown",
        "none",
    )


def test_a_detected_cdn_is_never_upgraded_to_storing_by_derivation() -> None:
    for default in ("storing_cdn", "file_host", "origin_host"):
        function, _ = derive_function(
            "network", cdn="cloudflare", override="", directory_default=default
        )
        assert function == "reverse_proxy_cdn"
    # A directory default can never say storing either, CDN or not.
    assert derive_function("network", cdn="", override="", directory_default="storing_cdn") == (
        "origin_host",
        "derived",
    )


def test_a_staff_override_beats_every_derivation() -> None:
    assert derive_function(
        "network", cdn="cloudflare", override="storing_cdn", directory_default=""
    ) == ("storing_cdn", "override")
    assert derive_function(
        "service", cdn="", override="file_host", directory_default="user_content_platform"
    ) == ("file_host", "override")
    assert derive_function(
        "service", cdn="", override="", directory_default="user_content_platform"
    ) == ("user_content_platform", "directory")


@pytest.mark.parametrize(
    ("function", "kind", "expected"),
    [
        ("origin_host", "dsa_art16", "primary"),
        ("file_host", "dmca_512c", "primary"),
        ("user_content_platform", "provider_copyright_policy_dmca_style", "primary"),
        ("origin_host", "abuse_email", "primary"),
        ("storing_cdn", "web_form", "primary"),
        ("origin_host", "dmca_512d", "not_supported"),
        ("origin_host", "forwarding_only", "forwarding_only"),
        ("search_engine", "dmca_512d", "delisting_draft"),
        ("search_engine", "web_form", "delisting_draft"),
        ("search_engine", "dsa_art16", "not_supported"),
        ("search_engine", "abuse_email", "not_supported"),
        ("reverse_proxy_cdn", "abuse_email", "forwarding_only"),
        ("reverse_proxy_cdn", "dsa_art16", "forwarding_only"),
        ("reverse_proxy_cdn", "dmca_512d", "not_supported"),
        ("registrar", "abuse_email", "additional_contact_only"),
        ("payment", "web_form", "additional_contact_only"),
        ("advertiser", "provider_copyright_policy", "additional_contact_only"),
        ("unknown", "dsa_art16", "needs_classification"),
        ("other", "abuse_email", "needs_classification"),
        ("origin_host", "none_known", "not_supported"),
        ("registrar", "none_known", "not_supported"),
        ("origin_host", "carrier_pigeon", "not_supported"),
    ],
)
def test_v1_support_follows_the_recipient_table(function: str, kind: str, expected: str) -> None:
    assert v1_support(function, kind) == expected


def test_every_pair_has_a_support_code() -> None:
    for function in PROVIDER_FUNCTIONS:
        for kind in CHANNEL_KINDS:
            assert v1_support(function, kind)


def choice(kind: str, support: str = "primary", **kwargs: bool) -> ChannelChoice:
    return ChannelChoice(
        kind=kind,
        support=support,  # type: ignore[arg-type]
        from_directory=kwargs.get("from_directory", True),
        recheck_due=kwargs.get("recheck_due", False),
    )


def test_suggestion_prefers_support_then_the_nexus_order() -> None:
    channels = [choice("abuse_email"), choice("dmca_512c"), choice("dsa_art16")]
    assert suggest_channel(channels, "eu_facing") == 2
    assert suggest_channel(channels, "us_nexus") == 1
    assert suggest_channel(channels, "unknown") == 2
    mixed = [choice("dsa_art16", "forwarding_only"), choice("abuse_email", "primary")]
    assert suggest_channel(mixed, "eu_facing") == 1


def test_suggestion_prefers_directory_and_fresh_channels() -> None:
    assert (
        suggest_channel(
            [choice("abuse_email", from_directory=False), choice("abuse_email")], "other"
        )
        == 1
    )
    assert suggest_channel([choice("web_form", recheck_due=True), choice("web_form")], "other") == 1


def test_no_suggestion_without_a_usable_channel() -> None:
    assert suggest_channel([], "eu_facing") is None
    assert suggest_channel([choice("none_known", "not_supported")], "eu_facing") is None


def test_recheck_is_due_after_the_window() -> None:
    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert not needs_recheck(now - timedelta(days=89), now, 90)
    assert needs_recheck(now - timedelta(days=90), now, 90)


@pytest.mark.parametrize(
    "email",
    [
        "abuse@example.net",
        "dmca-notices@example.net",
        "legalteam@example.net",
        "copyright.agent@example.net",
        "ip@example.net",
        "noc@example.net",
    ],
)
def test_role_addresses_are_accepted(email: str) -> None:
    assert is_role_address(email)


@pytest.mark.parametrize(
    "email", ["jane.doe@example.net", "philip@example.net", "jdoe@example.net", "abuse", "@x.net"]
)
def test_personal_looking_addresses_are_refused(email: str) -> None:
    assert not is_role_address(email)


def test_languages() -> None:
    assert parse_languages("") == []
    assert parse_languages("en, DE,en") == ["en", "de"]
    assert parse_languages("english") is None


def test_hostname_suffixes_most_specific_first() -> None:
    assert hostname_suffixes("a.cdn.example.com") == [
        "a.cdn.example.com",
        "cdn.example.com",
        "example.com",
    ]
    assert hostname_suffixes("example.com") == ["example.com"]
