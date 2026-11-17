"""The *Unternehmer* evidence and the guard that reads it (issue #92).

A declaration alone does not convert a consumer into a business. What
is tested here is therefore the *evidence*: that a half-filled record is reported as incomplete,
that a misleading one cannot be stored at all, and that granting a customer access without one
says so — or refuses, where the operator asked for that.
"""

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts import business_status as rules
from accounts.admin import BusinessStatusEvidencedFilter, OrganisationAdmin
from accounts.declarations import (
    CURRENT_AGE_DECLARATION,
    CURRENT_BUSINESS_STATUS_DECLARATION,
    OPERATIVE_LANGUAGE,
    age_declaration_versions,
    declaration_versions,
)
from accounts.models import BusinessStatus, Membership, Organisation


@pytest.fixture
def organisation(db: None) -> Organisation:
    return Organisation.objects.create(name="Alpha Studio")


@pytest.fixture
def staff(db: None) -> User:
    return User.objects.create_user("operator", is_staff=True)


def evidenced(organisation: Organisation, staff: User, **overrides: object) -> BusinessStatus:
    """A complete, EU record — the state onboarding is supposed to reach."""
    fields: dict[str, object] = {
        "evidence_kind": BusinessStatus.EvidenceKind.COMMERCIAL_REGISTER,
        "evidence_note": "HRB 12345, Amtsgericht Charlottenburg",
        "country": "DE",
        "checked_by": staff,
        "checked_at": timezone.now(),
        "declaration_version": CURRENT_BUSINESS_STATUS_DECLARATION.version,
        "declaration_text": CURRENT_BUSINESS_STATUS_DECLARATION.text(),
        "declared_at": timezone.now(),
        "age_declaration_version": CURRENT_AGE_DECLARATION.version,
        "age_declaration_text": CURRENT_AGE_DECLARATION.text(),
        "age_declared_at": timezone.now(),
    }
    fields.update(overrides)
    return BusinessStatus.objects.create(organisation=organisation, **fields)


# --- what counts as evidenced ------------------------------------------------------------------


def test_an_organisation_with_no_record_is_not_evidenced(organisation: Organisation) -> None:
    assert rules.business_status_codes(organisation) == [rules.NO_RECORD]
    assert organisation.business_status_evidenced is False


def test_a_complete_record_is_evidenced(organisation: Organisation, staff: User) -> None:
    evidenced(organisation, staff)

    assert rules.business_status_codes(organisation) == []
    assert organisation.business_status_evidenced is True


def test_every_missing_piece_is_named_in_a_fixed_order(organisation: Organisation) -> None:
    record = BusinessStatus.objects.create(organisation=organisation)

    assert record.codes() == [
        rules.NO_EVIDENCE_KIND,
        rules.NOT_CHECKED,
        rules.COUNTRY_MISSING,
        rules.DECLARATION_MISSING,
        rules.AGE_DECLARATION_MISSING,
    ]
    assert organisation.business_status_evidenced is False


def test_other_without_a_note_and_vat_without_a_number_are_not_evidence(
    organisation: Organisation, staff: User
) -> None:
    record = evidenced(
        organisation, staff, evidence_kind=BusinessStatus.EvidenceKind.OTHER, evidence_note=""
    )

    assert record.codes() == [rules.EVIDENCE_NOTE_MISSING]

    record.evidence_kind = BusinessStatus.EvidenceKind.VAT_ID
    record.evidence_note = "checked in VIES"
    assert record.codes() == [rules.VAT_ID_MISSING]

    record.vat_id = "DE123456789"
    assert record.codes() == []


def test_a_record_nobody_signed_off_on_is_not_evidenced(
    organisation: Organisation, staff: User
) -> None:
    record = evidenced(organisation, staff, checked_at=None)

    assert record.codes() == [rules.NOT_CHECKED]


def test_losing_the_staff_account_does_not_unevidence_an_organisation(
    organisation: Organisation, staff: User
) -> None:
    """`checked_by` is SET_NULL, so a deleted staff account must not rewrite history."""
    evidenced(organisation, staff)
    staff.delete()
    organisation.refresh_from_db()

    record = BusinessStatus.objects.get(organisation=organisation)
    assert record.checked_by_id is None
    assert record.codes() == []
    assert organisation.business_status_evidenced is True


# --- the EU flag -------------------------------------------------------------------------------


def test_a_country_outside_the_eu_is_flagged_but_still_evidence(
    organisation: Organisation, staff: User
) -> None:
    """The pilot is EU-only (issue #95 item 7), but that is scope, not a gap in the evidence."""
    record = evidenced(organisation, staff, country="CH")

    assert record.codes() == [rules.OUTSIDE_EU]
    assert organisation.business_status_evidenced is True
    assert rules.OUTSIDE_EU not in rules.MISSING_CODES


def test_the_eu_is_twenty_seven_states_and_greece_is_gr() -> None:
    assert len(rules.EU_MEMBER_STATES) == 27
    assert "GR" in rules.EU_MEMBER_STATES
    # EL is the VAT prefix for Greece, not a country code, and the UK left.
    assert "EL" not in rules.EU_MEMBER_STATES
    assert "GB" not in rules.EU_MEMBER_STATES


# --- what may not be stored --------------------------------------------------------------------


def test_the_country_is_normalised_on_the_way_in(organisation: Organisation) -> None:
    """A staff member types `de`; the stored value is the ISO code."""
    record = BusinessStatus(organisation=organisation, country=" de ")
    record.clean()

    assert record.country == "DE"


def test_a_country_that_is_not_an_iso_code_is_refused(organisation: Organisation) -> None:
    record = BusinessStatus(organisation=organisation, country="D1")

    with pytest.raises(ValidationError) as refusal:
        record.full_clean()
    assert "country" in refusal.value.message_dict


def test_the_database_refuses_a_country_that_is_not_two_capitals(
    organisation: Organisation,
) -> None:
    """The form is not the only writer; the constraint is."""
    with pytest.raises(IntegrityError), transaction.atomic():
        BusinessStatus.objects.create(organisation=organisation, country="de")


def test_the_vat_id_is_stored_without_spaces_and_upper_cased(
    organisation: Organisation,
) -> None:
    record = BusinessStatus(organisation=organisation, vat_id=" de 123 456 789 ")
    record.clean()

    assert record.vat_id == "DE123456789"


def test_a_declaration_is_stored_whole_or_not_at_all(organisation: Organisation) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        BusinessStatus.objects.create(
            organisation=organisation,
            declaration_version=CURRENT_BUSINESS_STATUS_DECLARATION.version,
            declaration_text="",
            declared_at=timezone.now(),
        )


def test_an_unknown_declaration_version_is_refused(organisation: Organisation, staff: User) -> None:
    """A stored version must name a wording that can still be read back."""
    record = evidenced(organisation, staff, declaration_version="unternehmer-1999-01-01")

    with pytest.raises(ValidationError) as refusal:
        record.full_clean()
    assert "declaration_version" in refusal.value.message_dict


def test_a_superseded_wording_stays_valid_and_the_german_is_operative() -> None:
    assert CURRENT_BUSINESS_STATUS_DECLARATION.version in declaration_versions()
    assert OPERATIVE_LANGUAGE == "de"
    assert "§ 14 BGB" in CURRENT_BUSINESS_STATUS_DECLARATION.text()


# --- the majority declaration (issue #123) -------------------------------------------------------


def test_an_age_declaration_is_stored_whole_or_not_at_all(organisation: Organisation) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        BusinessStatus.objects.create(
            organisation=organisation,
            age_declaration_version=CURRENT_AGE_DECLARATION.version,
            age_declaration_text="",
            age_declared_at=timezone.now(),
        )


def test_an_unknown_age_declaration_version_is_refused(
    organisation: Organisation, staff: User
) -> None:
    record = evidenced(organisation, staff, age_declaration_version="volljaehrigkeit-1999-01-01")

    with pytest.raises(ValidationError) as refusal:
        record.full_clean()
    assert "age_declaration_version" in refusal.value.message_dict


def test_a_record_without_a_declared_age_is_not_evidenced(
    organisation: Organisation, staff: User
) -> None:
    record = evidenced(
        organisation,
        staff,
        age_declaration_version="",
        age_declaration_text="",
        age_declared_at=None,
    )

    assert record.codes() == [rules.AGE_DECLARATION_MISSING]
    assert organisation.business_status_evidenced is False


def test_the_age_declaration_has_a_german_operative_wording_and_no_birth_date(
    organisation: Organisation,
) -> None:
    assert CURRENT_AGE_DECLARATION.version in age_declaration_versions()
    assert "volljährig" in CURRENT_AGE_DECLARATION.text()
    assert CURRENT_AGE_DECLARATION.text("en")
    # The record is the declaration itself: no field anywhere holds a date of birth (issue #123).
    assert not [field.name for field in BusinessStatus._meta.fields if "birth" in field.name]


# --- the guard ---------------------------------------------------------------------------------


def test_the_guard_warns_by_default(organisation: Organisation) -> None:
    message = rules.check_before_access(organisation)

    assert message is not None
    assert str(organisation.pk) in message
    assert rules.NO_RECORD in message
    assert rules.guard_mode() == rules.GUARD_WARN


def test_the_guard_refuses_when_the_operator_asks_for_it(
    organisation: Organisation, settings: Settings
) -> None:
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "refuse"

    with pytest.raises(rules.BusinessStatusNotEvidenced):
        rules.check_before_access(organisation)


def test_a_typo_in_the_setting_reads_as_warn(
    organisation: Organisation, settings: Settings
) -> None:
    """A guard that refuses every membership because of a typo would look like a bug."""
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "refues"

    assert rules.guard_mode() == rules.GUARD_WARN
    assert rules.check_before_access(organisation) is not None


def test_being_outside_the_eu_is_never_a_refusal(
    organisation: Organisation, staff: User, settings: Settings
) -> None:
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "refuse"
    evidenced(organisation, staff, country="CH")

    message = rules.check_before_access(organisation)

    assert message is not None and rules.OUTSIDE_EU in message


def test_a_sandbox_organisation_is_never_asked_for_evidence(db: None) -> None:
    """ADR 0035: the demo dataset is no contracting party; its warning would only be noise."""
    sandbox = Organisation.objects.create(name="Demo", is_sandbox=True)

    assert rules.business_status_codes(sandbox) == []
    assert sandbox.business_status_evidenced is True
    assert rules.check_before_access(sandbox) is None


def test_the_message_never_carries_the_evidence_note(
    organisation: Organisation, staff: User
) -> None:
    """Findings name identifiers, never content: the note is staff prose about a document."""
    evidenced(
        organisation, staff, evidence_note="HRB 12345, Amtsgericht Charlottenburg", country=""
    )

    message = rules.check_before_access(organisation)

    assert message is not None
    assert "HRB 12345" not in message


# --- the guard where access is granted ---------------------------------------------------------


def test_a_membership_form_refuses_an_unevidenced_organisation(
    organisation: Organisation, settings: Settings
) -> None:
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "refuse"
    membership = Membership(organisation=organisation, user=User.objects.create_user("customer"))

    with pytest.raises(ValidationError) as refusal:
        membership.full_clean()
    assert "organisation" in refusal.value.message_dict


def test_a_membership_form_passes_once_the_evidence_is_on_file(
    organisation: Organisation, staff: User, settings: Settings
) -> None:
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "refuse"
    evidenced(organisation, staff)
    membership = Membership(organisation=organisation, user=User.objects.create_user("customer"))

    membership.full_clean()  # no exception


def test_warn_mode_lets_the_membership_through(
    organisation: Organisation, settings: Settings
) -> None:
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "warn"
    membership = Membership(organisation=organisation, user=User.objects.create_user("customer"))

    membership.full_clean()  # no exception; the admin shows a message instead


def test_the_seeding_path_is_never_refused(organisation: Organisation, settings: Settings) -> None:
    """`seed_test_customer` and `seed_demo_dataset` use `create`/`get_or_create`, not `full_clean`,
    and the command passes `enforce=False` on top of that."""
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "refuse"

    Membership.objects.create(
        organisation=organisation, user=User.objects.create_user("test-customer")
    )
    assert rules.check_before_access(organisation, enforce=False) is not None


# --- the admin filter --------------------------------------------------------------------------


def filtered(chosen: str) -> list[str]:
    """The names the admin's "business status" filter shows for one choice."""
    site = admin.AdminSite()
    model_admin = OrganisationAdmin(Organisation, site)
    request = RequestFactory().get("/")
    list_filter = BusinessStatusEvidencedFilter(
        request, {"business_status": [chosen]}, Organisation, model_admin
    )
    assert list_filter.value() == chosen, "Django's filter parameter shape changed"
    queryset = list_filter.queryset(request, Organisation.objects.all())
    assert queryset is not None
    return sorted(queryset.values_list("name", flat=True))


def test_the_admin_filter_finds_the_organisations_still_missing_evidence(
    organisation: Organisation, staff: User
) -> None:
    evidenced(organisation, staff)
    Organisation.objects.create(name="Bravo Creator")
    outside = Organisation.objects.create(name="Charlie GmbH")
    evidenced(outside, staff, country="CH")
    Organisation.objects.create(name="Demo", is_sandbox=True)

    assert filtered("not_evidenced") == ["Bravo Creator"]
    # The sandbox organisation counts as evidenced: it is no contracting party (ADR 0035).
    assert filtered("evidenced") == ["Alpha Studio", "Charlie GmbH", "Demo"]
    assert filtered("outside_eu") == ["Charlie GmbH"]
