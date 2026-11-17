"""The do-not-contact record (issue #98, ADR 0047, docs/do-not-contact.md).

Every identifier here is invented, and none of it is stored anyway: what the tests check is that
two spellings of the same identifier collide, two different ones do not, and nothing on any path —
row, log line or error message — carries the identifier that was typed.
"""

from datetime import date
from io import StringIO

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory
from pytest_django.fixtures import Settings

from catalog.admin import DoNotContactRecordAdmin, DoNotContactRecordForm
from catalog.do_not_contact import (
    EMAIL,
    HANDLE,
    ORGANISATION,
    PHONE,
    POSTAL,
    MissingSecret,
    UnusableIdentifier,
    digest,
    normalise,
)
from catalog.models import Creator, DoNotContactRecord
from catalog.services import contact_check, record_no_contact

KEY = "test-key-not-a-real-one"

pytestmark = pytest.mark.django_db


# --- Normalisation: equivalent spellings collide, different people do not ----------------------


@pytest.mark.parametrize(
    ("kind", "one", "other"),
    [
        (EMAIL, " Post@Example.org ", "post@example.org"),
        (EMAIL, "post@example.org.", "post@EXAMPLE.ORG"),
        (POSTAL, "Musterstr. 1, 10115 Berlin", "Musterstraße 1\n10115  Berlin"),
        (POSTAL, "Musterstrasse 1, 10115 Berlin", "MUSTERSTR. 1 · 10115 Berlin"),
        (ORGANISATION, "Muster und Partner GmbH, Köln", "Muster Partner GmbH (Koeln)"),
        (HANDLE, "https://www.example.net/Somebody/", "example:@somebody"),
        (HANDLE, "example.net/somebody?tab=posts", "Example.net/Somebody"),
        (PHONE, "+49 30 1234 5678", "0049 (0)30 / 1234-5678"),
    ],
)
def test_equivalent_spellings_normalise_to_the_same_identifier(
    kind: str, one: str, other: str
) -> None:
    assert normalise(kind, one) == normalise(kind, other)


@pytest.mark.parametrize(
    ("kind", "one", "other"),
    [
        # Dot- and plus-folding is one provider's convention; applying it everywhere would put two
        # different mailboxes on one hash.
        (EMAIL, "a.post@example.org", "apost@example.org"),
        (EMAIL, "post+beta@example.org", "post@example.org"),
        (EMAIL, "post@example.org", "post@example.net"),
        (POSTAL, "Musterstr. 1, 10115 Berlin", "Musterstr. 2, 10115 Berlin"),
        (ORGANISATION, "Muster GmbH, Köln", "Muster GmbH, Bonn"),
        # A legal form is part of who somebody is, unlike a conjunction.
        (ORGANISATION, "Muster GmbH, Köln", "Muster GmbH & Co. KG, Köln"),
        # The same handle on two networks is usually two people.
        (HANDLE, "example.net/somebody", "other.net/somebody"),
        # Deliberate: a national spelling cannot be resolved without guessing a country, so it
        # stays its own identifier and both spellings are recorded (docs/do-not-contact.md).
        (PHONE, "030 12345678", "+49 30 12345678"),
    ],
)
def test_different_identifiers_stay_different(kind: str, one: str, other: str) -> None:
    assert normalise(kind, one) != normalise(kind, other)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (EMAIL, "  "),
        (EMAIL, "not-an-address"),
        (EMAIL, "@example.org"),
        (POSTAL, "!!!"),
        (HANDLE, "somebody"),  # no platform: would block a stranger of the same name
        (HANDLE, "example.net/"),
        (PHONE, "12"),
        ("nonsense", "whatever"),
    ],
)
def test_unusable_input_is_refused_rather_than_hashed(kind: str, value: str) -> None:
    with pytest.raises(UnusableIdentifier):
        normalise(kind, value)


def test_the_kind_is_part_of_the_hash() -> None:
    """A record of one kind must never block an identical string of another."""
    assert digest(EMAIL, "post@example.org", KEY) != digest(POSTAL, "post@example.org", KEY)


def test_the_key_decides_the_hash() -> None:
    assert digest(EMAIL, "post@example.org", KEY) != digest(EMAIL, "post@example.org", "other-key")


# --- An empty secret refuses, rather than hashing with an empty key ----------------------------


def test_digest_refuses_an_empty_secret() -> None:
    with pytest.raises(MissingSecret):
        digest(EMAIL, "post@example.org", "")


def test_recording_refuses_an_empty_secret(settings: Settings) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = ""
    with pytest.raises(MissingSecret):
        record_no_contact(
            kind=EMAIL,
            identifier="post@example.org",
            received_on=date(2026, 9, 18),
            channel="letter",
            recorded_by="operator",
        )
    assert DoNotContactRecord.objects.count() == 0


def test_checking_refuses_an_empty_secret(settings: Settings) -> None:
    """A check that cannot see the records would wave everybody through."""
    settings.DO_NOT_CONTACT_HMAC_KEY = ""
    with pytest.raises(MissingSecret):
        contact_check(EMAIL, ["post@example.org"])


# --- Recording and checking --------------------------------------------------------------------


def record(
    identifier: str, kind: str = EMAIL, *, creator: Creator | None = None
) -> DoNotContactRecord:
    row, _ = record_no_contact(
        kind=kind,
        identifier=identifier,
        received_on=date(2026, 9, 18),
        channel="letter",
        recorded_by="operator",
        creator=creator,
        secret=KEY,
    )
    return row


def test_a_recorded_no_blocks_every_spelling_of_that_identifier() -> None:
    record(" Post@Example.org ")
    assert contact_check(EMAIL, ["post@example.org"], KEY) == [True]
    assert contact_check(EMAIL, ["other@example.org"], KEY) == [False]


def test_the_row_holds_no_identifier() -> None:
    row = record("post@example.org")
    stored = " ".join(str(value) for value in row.__dict__.values())
    assert "post@example.org" not in stored
    assert "example.org" not in stored
    assert row.identifier_hash != ""


def test_recording_the_same_identifier_twice_changes_nothing() -> None:
    first = record("post@example.org")
    second, created = record_no_contact(
        kind=EMAIL,
        identifier="POST@example.org",
        received_on=date(2026, 9, 20),
        channel="email",
        recorded_by="somebody else",
        secret=KEY,
    )
    assert not created
    assert second.pk == first.pk
    assert second.received_on == date(2026, 9, 18)
    assert DoNotContactRecord.objects.count() == 1


def test_check_answers_by_position() -> None:
    record("blocked@example.org")
    answers = contact_check(
        EMAIL, ["allowed@example.org", "blocked@example.org", "also-allowed@example.org"], KEY
    )
    assert answers == [False, True, False]


def test_a_record_of_one_kind_does_not_block_another_kind() -> None:
    record("Muster GmbH, Köln", kind=ORGANISATION)
    assert contact_check(ORGANISATION, ["muster gmbh koeln"], KEY) == [True]
    assert contact_check(POSTAL, ["Muster GmbH, Köln"], KEY) == [False]


# --- The record outlives everything it was linked to --------------------------------------------


def test_deleting_the_linked_creator_leaves_the_record_standing() -> None:
    creator = Creator.objects.create(name="Invented Prospect")
    row = record("post@example.org", creator=creator)
    creator.delete()
    row.refresh_from_db()
    assert row.creator_id is None
    assert contact_check(EMAIL, ["post@example.org"], KEY) == [True]


# --- The check command --------------------------------------------------------------------------


def test_the_command_prints_positions_and_never_an_identifier(settings: Settings) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = KEY
    record("blocked@example.org")
    out = StringIO()
    with pytest.raises(CommandError) as raised:
        call_command("may_contact", "allowed@example.org", "blocked@example.org", stdout=out)
    printed = out.getvalue() + str(raised.value)
    assert "1: allowed" in printed
    assert "2: blocked" in printed
    for secret_bit in ("blocked@example.org", "allowed@example.org", "example.org"):
        assert secret_bit not in printed


def test_the_command_is_quiet_and_zero_when_nobody_objected(settings: Settings) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = KEY
    out = StringIO()
    call_command("may_contact", "allowed@example.org", stdout=out)
    assert "1: allowed" in out.getvalue()


def test_the_command_reads_stdin(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = KEY
    record("blocked@example.org")
    monkeypatch.setattr("sys.stdin", StringIO("allowed@example.org\nblocked@example.org\n"))
    out = StringIO()
    with pytest.raises(CommandError):
        call_command("may_contact", stdout=out)
    assert out.getvalue().splitlines()[:2] == ["1: allowed", "2: blocked"]


def test_the_command_fails_on_an_identifier_it_cannot_check(settings: Settings) -> None:
    """An unchecked identifier is not an allowed one."""
    settings.DO_NOT_CONTACT_HMAC_KEY = KEY
    out = StringIO()
    with pytest.raises(CommandError):
        call_command("may_contact", "not-an-address", stdout=out)
    assert "1: unusable" in out.getvalue()
    assert "not-an-address" not in out.getvalue()


def test_the_command_refuses_without_a_key(settings: Settings) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = ""
    with pytest.raises(CommandError, match="DO_NOT_CONTACT_HMAC_KEY"):
        call_command("may_contact", "post@example.org", stdout=StringIO())


# --- The staff screen ---------------------------------------------------------------------------


def admin_screen() -> DoNotContactRecordAdmin:
    return DoNotContactRecordAdmin(DoNotContactRecord, AdminSite())


def test_the_record_cannot_be_deleted_in_the_admin() -> None:
    screen = admin_screen()
    request = RequestFactory().get("/")
    assert screen.has_delete_permission(request) is False
    assert screen.has_delete_permission(request, DoNotContactRecord()) is False


def test_the_admin_never_offers_the_hash() -> None:
    screen = admin_screen()
    request = RequestFactory().get("/")
    shown = set(screen.list_display) | set(screen.get_readonly_fields(request))
    shown |= {name for _, options in screen.get_fieldsets(request) for name in options["fields"]}
    assert "identifier_hash" not in shown


def test_an_existing_row_can_no_longer_be_asked_for_the_identifier() -> None:
    """Nothing can re-type or reveal it: the row is the hash, and only the note stays editable."""
    screen = admin_screen()
    request = RequestFactory().get("/")
    row = record("post@example.org")
    editable = {
        name
        for _, options in screen.get_fieldsets(request, row)
        for name in options["fields"]
        if name not in screen.get_readonly_fields(request, row)
    }
    assert editable == {"note"}


def test_the_admin_form_hashes_what_is_typed_and_stores_none_of_it(settings: Settings) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = KEY
    form = DoNotContactRecordForm(
        data={
            "identifier": "Post@Example.org",
            "identifier_kind": EMAIL,
            "received_on": "2026-09-18",
            "channel": "letter",
            "recorded_by": "operator",
            "note": "objection to the letter",
        }
    )
    assert form.is_valid(), form.errors
    row = form.save()
    assert row.identifier_hash == digest(EMAIL, "post@example.org", KEY)
    assert "example.org" not in " ".join(str(value) for value in row.__dict__.values())
    assert contact_check(EMAIL, ["post@example.org"], KEY) == [True]


def test_the_admin_form_says_so_instead_of_recording_twice(settings: Settings) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = KEY
    record("post@example.org")
    form = DoNotContactRecordForm(
        data={
            "identifier": "post@example.org",
            "identifier_kind": EMAIL,
            "received_on": "2026-09-19",
            "channel": "email",
            "recorded_by": "operator",
        }
    )
    assert not form.is_valid()
    assert "already recorded" in str(form.errors["identifier"])
    assert DoNotContactRecord.objects.count() == 1


def test_the_admin_form_refuses_without_a_key(settings: Settings) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = ""
    form = DoNotContactRecordForm(
        data={
            "identifier": "post@example.org",
            "identifier_kind": EMAIL,
            "received_on": "2026-09-18",
            "channel": "letter",
            "recorded_by": "operator",
        }
    )
    assert not form.is_valid()
    assert "DO_NOT_CONTACT_HMAC_KEY" in str(form.errors)
    assert DoNotContactRecord.objects.count() == 0
