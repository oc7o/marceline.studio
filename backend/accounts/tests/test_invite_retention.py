"""The deletion clock on an unredeemed invite (ADR 0052 amendment).

Direct row construction throughout, not `accounts.invites.issue`/`complete`: this module's clock
does not care whether `ONBOARDING_INVITES_ENABLED` is on, only about the timestamps on the row, so
tests build exactly the states the module's own docstring names.
"""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from accounts.invite_retention import apply_retention
from accounts.models import CustomerInvite, DocumentAcceptance, Organisation

NOW = timezone.now()


def _invite(*, token_hash: str, **kwargs: object) -> CustomerInvite:
    defaults: dict[str, object] = {
        "email": "creator@example.invalid",
        "organisation_name": "Studio",
        "note": "a staff note",
    }
    defaults.update(kwargs)
    return CustomerInvite.objects.create(token_hash=token_hash, **defaults)


def _open(**kwargs: object) -> CustomerInvite:
    """An invite still inside its expiry window, not revoked, not redeemed."""
    kwargs.setdefault("expires_at", NOW + timedelta(days=5))
    return _invite(token_hash="a" * 64, **kwargs)


def _expired(days_ago: float, **kwargs: object) -> CustomerInvite:
    kwargs.setdefault("expires_at", NOW - timedelta(days=days_ago))
    return _invite(token_hash="b" * 64, **kwargs)


def _revoked(days_ago: float, **kwargs: object) -> CustomerInvite:
    kwargs.setdefault("expires_at", NOW + timedelta(days=30))  # not itself expired
    kwargs.setdefault("revoked_at", NOW - timedelta(days=days_ago))
    kwargs.setdefault("revoked_reason", "requested by the creator")
    return _invite(token_hash="c" * 64, **kwargs)


def _redeemed(organisation: Organisation) -> CustomerInvite:
    return _invite(
        token_hash="d" * 64,
        organisation=organisation,
        organisation_name="",
        expires_at=NOW - timedelta(days=100),
        redeemed_at=NOW - timedelta(days=90),
    )


# --- an open invite is never touched, whatever the retain period -------------------------------


def test_a_still_open_invite_is_never_a_candidate(db: None) -> None:
    invite = _open()

    report = apply_retention(now=NOW, dry_run=True, retain_days=0)

    assert report.due == 0
    assert report.clocked == 0
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()


# --- expiry starts the clock --------------------------------------------------------------------


def test_an_expired_invite_is_kept_inside_the_retention_window(db: None) -> None:
    invite = _expired(days_ago=5)

    report = apply_retention(now=NOW, dry_run=True, retain_days=30)

    assert report.clocked == 1
    assert report.due == 0
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()


def test_an_expired_invite_past_the_window_is_deleted_on_apply(db: None) -> None:
    invite = _expired(days_ago=31)

    dry = apply_retention(now=NOW, dry_run=True, retain_days=30)
    assert dry.due == 1
    assert dry.deleted_ids == []
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()

    applied = apply_retention(now=NOW, dry_run=False, retain_days=30)
    assert applied.due == 1
    assert applied.deleted_ids == [invite.pk]
    assert not CustomerInvite.objects.filter(pk=invite.pk).exists()


def test_apply_is_idempotent(db: None) -> None:
    _expired(days_ago=31)

    first = apply_retention(now=NOW, dry_run=False, retain_days=30)
    second = apply_retention(now=NOW, dry_run=False, retain_days=30)

    assert first.due == 1
    assert second.due == 0
    assert second.deleted_ids == []


# --- revocation starts the clock, and wins over expiry ------------------------------------------


def test_a_revoked_invite_uses_the_revocation_timestamp_not_expires_at(db: None) -> None:
    # expires_at is 30 days out (still "unexpired"), but revoked 31 days ago: due anyway.
    invite = _revoked(days_ago=31)

    report = apply_retention(now=NOW, dry_run=False, retain_days=30)

    assert report.due == 1
    assert report.deleted_ids == [invite.pk]


def test_a_revoked_invite_inside_the_window_is_kept(db: None) -> None:
    invite = _revoked(days_ago=5)

    report = apply_retention(now=NOW, dry_run=False, retain_days=30)

    assert report.due == 0
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()


# --- a redeemed invite is never this module's concern --------------------------------------------


def test_a_redeemed_invite_is_never_a_candidate(db: None) -> None:
    organisation = Organisation.objects.create(name="Studio")
    invite = _redeemed(organisation)

    report = apply_retention(now=NOW, dry_run=False, retain_days=0)

    assert report.due == 0
    assert report.clocked == 0
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()


# --- a handed-over redemption is never deleted, whatever its own clock says ----------------------


def test_a_handed_over_invite_past_its_window_is_kept_and_reported(db: None) -> None:
    invite = _expired(days_ago=60, handed_over_at=NOW - timedelta(days=45), submission_id="sub_abc")

    report = apply_retention(now=NOW, dry_run=False, retain_days=30)

    assert report.due == 0
    assert report.in_flight == 1
    assert report.deleted_ids == []
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()


# --- what deletion takes with it, and what it does not -------------------------------------------


def test_document_acceptances_survive_deletion(db: None) -> None:
    invite = _expired(days_ago=31)
    acceptance = DocumentAcceptance.objects.create(
        invite=invite,
        document_id="pilot_agreement",
        version="2026-10-01",
        sha256="f" * 64,
        accepted_at=NOW - timedelta(days=40),
    )

    apply_retention(now=NOW, dry_run=False, retain_days=30)

    acceptance.refresh_from_db()
    assert acceptance.invite_id is None


def test_dry_run_deletes_nothing_at_all(db: None) -> None:
    _expired(days_ago=60)
    _revoked(days_ago=60)

    report = apply_retention(now=NOW, dry_run=True, retain_days=30)

    assert report.due == 2
    assert report.deleted_ids == []
    assert CustomerInvite.objects.count() == 2


# --- the management command ----------------------------------------------------------------------


def test_command_defaults_to_a_dry_run_and_prints_counts_only(db: None) -> None:
    invite = _expired(days_ago=31)
    out = StringIO()

    call_command("invite_retention", stdout=out)

    text = out.getvalue()
    assert "dry run" in text
    assert "to delete: 1 invite" in text
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()
    assert invite.email not in text
    assert invite.note not in text
    assert invite.token_hash not in text


def test_command_apply_deletes_and_is_idempotent(db: None) -> None:
    invite = _expired(days_ago=31)
    out = StringIO()

    call_command("invite_retention", "--apply", stdout=out)
    assert not CustomerInvite.objects.filter(pk=invite.pk).exists()
    assert "deleted: 1 invite" in out.getvalue()

    out2 = StringIO()
    call_command("invite_retention", "--apply", stdout=out2)
    assert "deleted: 0 invite" in out2.getvalue()


def test_command_retain_days_flag_overrides_the_setting(db: None) -> None:
    invite = _expired(days_ago=10)  # inside the default 30-day window
    out = StringIO()

    call_command("invite_retention", "--retain-days", "5", stdout=out)

    assert "to delete: 1 invite" in out.getvalue()
    assert CustomerInvite.objects.filter(pk=invite.pk).exists()  # dry run: nothing deleted
