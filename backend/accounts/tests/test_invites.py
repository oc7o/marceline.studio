"""Issuing and redeeming an invite: the guarantees, one test each (issue #115, ADR 0052).

The HTTP half — the identical neutral answer, throttling, CSRF, tenancy — is
`marceline/tests/test_app_invites.py`. What is tested here is the model and the service: single
use, expiry, revocation, what is stored, and the refusal while no document version is published.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts import invites, onboarding
from accounts.declarations import (
    CURRENT_AGE_DECLARATION,
    CURRENT_BUSINESS_STATUS_DECLARATION,
    Declaration,
)
from accounts.models import (
    BusinessStatus,
    CustomerInvite,
    DocumentAcceptance,
    Membership,
    Organisation,
)
from accounts.onboarding import Step

#: A plausible `ONBOARDING_DOCUMENTS` value. The digests are made up — nothing here fetches a
#: document, and a real one is a published artefact the operator pins in `.env`.
DOCUMENTS = (
    "pilot_agreement:2026-10-01:" + "a" * 64 + ":https://example.invalid/pilot,"
    "service_privacy_notice:2026-10-01:" + "b" * 64 + ":https://example.invalid/privacy"
)


@pytest.fixture
def enabled(settings: Settings) -> None:
    settings.ONBOARDING_INVITES_ENABLED = True
    settings.ONBOARDING_DOCUMENTS = DOCUMENTS


def _issue(**kwargs: object) -> tuple[CustomerInvite, str]:
    defaults: dict[str, object] = {
        "email": "creator@example.invalid",
        "organisation_name": "Studio",
    }
    defaults.update(kwargs)
    return invites.issue(**defaults)  # type: ignore[arg-type]


def _redeem(invite: CustomerInvite) -> User:
    invites.set_password(invite, "a-long-enough-passphrase")
    invites.record_declarations(invite)
    invites.record_acceptances(invite, language="de")
    assert invites.record_country(invite, "DE") is True
    return invites.complete(invite)


# --- the flag -----------------------------------------------------------------------------------


def test_issuing_refuses_while_the_feature_is_off(db: None) -> None:
    with pytest.raises(invites.InviteRefused):
        _issue()


def test_a_token_does_not_work_while_the_feature_is_off(db: None, settings: Settings) -> None:
    settings.ONBOARDING_INVITES_ENABLED = True
    _, token = _issue()
    settings.ONBOARDING_INVITES_ENABLED = False
    assert invites.lookup(token) is None


# --- the token ----------------------------------------------------------------------------------


def test_only_the_digest_is_stored(db: None, enabled: None) -> None:
    invite, token = _issue()
    assert invite.token_hash == onboarding.token_hash(token)
    assert token not in invite.token_hash
    # Nothing on the row can reproduce the token: it exists once, in the caller's hands.
    assert token not in str(invite.__dict__)


def test_a_malformed_token_is_refused_without_a_query(db: None, enabled: None) -> None:
    assert invites.lookup("not a token") is None
    assert invites.lookup("") is None


# --- single use, expiry, revocation --------------------------------------------------------------


def test_an_invite_is_single_use(db: None, enabled: None) -> None:
    invite, token = _issue()
    _redeem(invite)
    assert invites.lookup(token) is None
    with pytest.raises(invites.InviteRefused):
        invites.complete(invite)


def test_an_expired_invite_is_refused(db: None, enabled: None) -> None:
    _, token = _issue(expires_at=timezone.now() - timedelta(minutes=1))
    assert invites.lookup(token) is None


def test_a_revoked_invite_is_refused(db: None, enabled: None) -> None:
    invite, token = _issue()
    invites.revoke(invite, reason="the operator changed his mind")
    assert invites.lookup(token) is None
    assert invite.revoked_reason  # staff-only; the customer is never told which failure it was


def test_the_expiry_defaults_to_the_setting(db: None, enabled: None, settings: Settings) -> None:
    settings.ONBOARDING_INVITE_EXPIRY_DAYS = 3
    invite, _ = _issue()
    assert invite.expires_at - timezone.now() < timedelta(days=3)
    assert invite.expires_at - timezone.now() > timedelta(days=2)


# --- the steps -----------------------------------------------------------------------------------


def test_the_step_is_derived_and_advances_in_order(db: None, enabled: None) -> None:
    invite, _ = _issue()
    assert invites.step_of(invite) is Step.PASSWORD
    invites.set_password(invite, "a-long-enough-passphrase")
    assert invites.step_of(invite) is Step.DECLARATION
    invites.record_declarations(invite)
    assert invites.step_of(invite) is Step.DOCUMENTS
    invites.record_acceptances(invite, language="de")
    assert invites.step_of(invite) is Step.COUNTRY
    invites.record_country(invite, "AT")
    assert invites.step_of(invite) is Step.COMPLETE
    invites.complete(invite)
    invite.refresh_from_db()
    assert invites.step_of(invite) is Step.DONE


def test_a_weak_password_is_refused_with_codes_not_sentences(db: None, enabled: None) -> None:
    invite, _ = _issue()
    problems = invites.password_problems("12345", invite.email)
    assert problems
    assert all(problem.startswith("password_") for problem in problems), problems


def test_the_declaration_is_stored_verbatim_with_its_version(db: None, enabled: None) -> None:
    invite, _ = _issue()
    invites.record_declarations(invite)
    assert invite.declaration_version == CURRENT_BUSINESS_STATUS_DECLARATION.version
    assert invite.declaration_text == CURRENT_BUSINESS_STATUS_DECLARATION.text()
    assert invite.declared_at is not None


def test_the_age_declaration_is_stored_verbatim_with_its_version(db: None, enabled: None) -> None:
    """Issue #123: the same record as the *Unternehmer* one — version, wording, moment."""
    invite, _ = _issue()
    invites.record_declarations(invite)
    assert invite.age_declaration_version == CURRENT_AGE_DECLARATION.version
    assert invite.age_declaration_text == CURRENT_AGE_DECLARATION.text()
    assert invite.age_declared_at is not None
    # A declaration, not a verification: there is nowhere for a birth date to go.
    assert not [field.name for field in CustomerInvite._meta.fields if "birth" in field.name]


def test_the_declarations_are_one_moment(db: None, enabled: None) -> None:
    """One screen, one step, one timestamp: a half-signed pair is not a state the flow has."""
    invite, _ = _issue()
    invites.record_declarations(invite)
    assert invite.declared_at == invite.age_declared_at


def test_an_age_declaration_of_an_old_version_does_not_count(
    db: None, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superseded wording sends the redemption back to the declaration step (issue #123).

    Stored rows keep it — `age_declaration_versions()` never drops a version — but it is a record
    to read back, never a signature that stands in for today's text.
    """
    invite, _ = _issue()
    invites.set_password(invite, "a-long-enough-passphrase")
    invites.record_declarations(invite)
    assert invites.step_of(invite) is Step.DOCUMENTS
    newer = Declaration(version="volljaehrigkeit-2099-01-01", texts={"de": "Neu.", "en": "New."})
    monkeypatch.setattr(invites, "CURRENT_AGE_DECLARATION", newer)
    assert invites.step_of(invite) is Step.DECLARATION


# --- the documents -------------------------------------------------------------------------------


def test_no_published_version_means_no_acceptance_and_no_completion(
    db: None, settings: Settings
) -> None:
    settings.ONBOARDING_INVITES_ENABLED = True
    settings.ONBOARDING_DOCUMENTS = ""
    invite, _ = _issue()
    invites.set_password(invite, "a-long-enough-passphrase")
    invites.record_declarations(invite)
    with pytest.raises(onboarding.DocumentsNotPublished):
        invites.record_acceptances(invite, language="de")
    # The flow therefore cannot reach the end while the documents are unreviewed drafts.
    assert invites.step_of(invite) is Step.DOCUMENTS


def test_half_a_document_list_is_as_good_as_none(db: None, settings: Settings) -> None:
    settings.ONBOARDING_DOCUMENTS = (
        "pilot_agreement:2026-10-01:" + "a" * 64 + ":https://x.invalid/p"
    )
    with pytest.raises(onboarding.DocumentsNotPublished):
        onboarding.published_documents()


def test_a_document_entry_without_a_digest_is_refused(settings: Settings) -> None:
    settings.ONBOARDING_DOCUMENTS = "pilot_agreement:2026-10-01:not-a-digest:https://x.invalid/p"
    with pytest.raises(onboarding.DocumentsNotPublished):
        onboarding.published_documents()


def test_an_acceptance_records_the_version_and_the_digest(db: None, enabled: None) -> None:
    invite, _ = _issue()
    rows = invites.record_acceptances(invite, language="de")
    assert {row.document_id for row in rows} == set(onboarding.REQUIRED_DOCUMENTS)
    for row in rows:
        assert row.version == "2026-10-01"
        assert len(row.sha256) == 64
        assert row.language == "de"
        assert row.accepted_at is not None


def test_acceptances_are_attached_to_the_account_on_completion(db: None, enabled: None) -> None:
    invite, _ = _issue()
    user = _redeem(invite)
    assert DocumentAcceptance.objects.filter(user=user).count() == len(
        onboarding.REQUIRED_DOCUMENTS
    )


# --- the country ---------------------------------------------------------------------------------


def test_a_non_eu_answer_is_counted_and_never_stored(db: None, enabled: None) -> None:
    invite, token = _issue()
    invites.set_password(invite, "a-long-enough-passphrase")
    invites.record_declarations(invite)
    invites.record_acceptances(invite, language="de")
    assert invites.record_country(invite, "CH") is False
    assert invite.country == ""
    assert invite.outside_eu_answers == 1
    # The invite survives it: a typo on a select box must not cost somebody their invitation.
    assert invites.lookup(token) is not None
    assert invites.step_of(invite) is Step.COUNTRY
    # And the same link can still be answered correctly.
    assert invites.record_country(invite, "DE") is True


# --- completion ----------------------------------------------------------------------------------


def test_completion_creates_the_account_and_scopes_it(db: None, enabled: None) -> None:
    invite, _ = _issue(organisation_name="A New Studio")
    user = _redeem(invite)
    assert user.username == "creator@example.invalid"
    membership = Membership.objects.get(user=user)
    assert membership.organisation.name == "A New Studio"
    status = BusinessStatus.objects.get(organisation=membership.organisation)
    assert status.country == "DE"
    assert status.declaration_version == CURRENT_BUSINESS_STATUS_DECLARATION.version
    # Both declarations move onto the organisation's record, so staff can read what was signed and
    # when without opening the invite (issue #123).
    assert status.age_declaration_version == CURRENT_AGE_DECLARATION.version
    assert status.age_declaration_text == CURRENT_AGE_DECLARATION.text()
    assert status.age_declared_at is not None
    # The evidence stays a staff step: the account exists and is *not* evidenced yet (issue #92).
    assert membership.organisation.business_status_evidenced is False


def test_an_invite_into_an_existing_organisation_grants_nothing_elsewhere(
    db: None, enabled: None
) -> None:
    a = Organisation.objects.create(name="Org A")
    Organisation.objects.create(name="Org B")
    invite, _ = _issue(organisation=a, organisation_name="")
    user = _redeem(invite)
    memberships = Membership.objects.filter(user=user)
    assert [membership.organisation_id for membership in memberships] == [a.pk]


def test_the_password_hash_is_moved_to_the_user_and_cleared(db: None, enabled: None) -> None:
    invite, _ = _issue()
    user = _redeem(invite)
    invite.refresh_from_db()
    assert invite.password_hash == ""
    assert user.check_password("a-long-enough-passphrase")


def test_completion_refuses_when_the_evidence_guard_is_set_to_refuse(
    db: None, enabled: None, settings: Settings
) -> None:
    settings.ONBOARDING_BUSINESS_STATUS_GUARD = "refuse"
    invite, _ = _issue()
    invites.set_password(invite, "a-long-enough-passphrase")
    invites.record_declarations(invite)
    invites.record_acceptances(invite, language="de")
    invites.record_country(invite, "DE")
    with pytest.raises(ValidationError):
        invites.complete(invite)
    assert not User.objects.filter(username=invite.email).exists()


def test_an_address_that_already_has_an_account_is_reported_not_silently_merged(
    db: None, enabled: None
) -> None:
    User.objects.create_user(username="creator@example.invalid")
    invite, _ = _issue()
    invites.set_password(invite, "a-long-enough-passphrase")
    invites.record_declarations(invite)
    invites.record_acceptances(invite, language="de")
    invites.record_country(invite, "DE")
    with pytest.raises(invites.AccountAlreadyExists):
        invites.complete(invite)


# --- the row's own rules -------------------------------------------------------------------------


def test_an_invite_leads_to_exactly_one_destination(db: None) -> None:
    with transaction.atomic(), pytest.raises(IntegrityError):
        CustomerInvite.objects.create(
            email="x@example.invalid",
            organisation_name="",
            token_hash="0" * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
    organisation = Organisation.objects.create(name="Both")
    with transaction.atomic(), pytest.raises(IntegrityError):
        CustomerInvite.objects.create(
            email="x@example.invalid",
            organisation=organisation,
            organisation_name="Also this",
            token_hash="1" * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )


def test_counts_are_counts(db: None, enabled: None) -> None:
    _issue()
    redeemed, _ = _issue(email="second@example.invalid")
    _redeem(redeemed)
    _issue(email="third@example.invalid", expires_at=timezone.now() - timedelta(days=1))
    counts = invites.counts()
    assert counts["open"] == 1
    assert counts["redeemed"] == 1
    assert counts["expired"] == 1
