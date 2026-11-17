"""Which operator-mail kinds still mail immediately (2026-09-19): core/operator_mail.py."""

from pytest_django.fixtures import Settings

from core import operator_mail


def test_empty_by_default_means_nothing_is_immediate(settings: Settings) -> None:
    settings.OPERATOR_MAIL_IMMEDIATE = ""

    assert operator_mail.immediate_kinds() == frozenset()
    for kind in operator_mail.KINDS:
        assert not operator_mail.is_immediate(kind)


def test_a_comma_list_names_more_than_one_kind(settings: Settings) -> None:
    settings.OPERATOR_MAIL_IMMEDIATE = "safety_incident, adapter_health"

    assert operator_mail.immediate_kinds() == {
        operator_mail.SAFETY_INCIDENT,
        operator_mail.ADAPTER_HEALTH,
    }
    assert operator_mail.is_immediate(operator_mail.SAFETY_INCIDENT)
    assert operator_mail.is_immediate(operator_mail.ADAPTER_HEALTH)
    assert not operator_mail.is_immediate(operator_mail.TDM_RESERVATION)


def test_blank_entries_and_whitespace_are_ignored(settings: Settings) -> None:
    settings.OPERATOR_MAIL_IMMEDIATE = " tdm_reservation ,, "

    assert operator_mail.immediate_kinds() == {operator_mail.TDM_RESERVATION}


def test_an_unrecognised_entry_never_turns_on_a_real_kind(settings: Settings) -> None:
    """Not a crash: a typo in `.env` should not take the crawler down, and must not accidentally
    switch a real kind on (it is not a substring or prefix match)."""
    settings.OPERATOR_MAIL_IMMEDIATE = "not_a_real_kind"

    for kind in operator_mail.KINDS:
        assert not operator_mail.is_immediate(kind)
