"""Customer-facing ids are opaque public ids, never primary keys (issue #77, ADR 0033).

Four things are pinned here: the columns fill themselves (on create, and for existing rows when the
column is added); an original's id belongs to the file as its organisation owns it and is written
when the owner is set; no customer response carries an integer id of anything; and a malformed,
integer, unknown or foreign id all get the byte-identical 404.
"""

from collections.abc import Callable, Iterator
from importlib import import_module
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.utils import timezone

from accounts.models import Organisation
from core import public_ids
from index.models import Page
from marceline.app.ownership import Via, owned_original, reassign, register
from marceline.models import OriginalOwnership, OriginalPublicId
from marceline.tests.app_helpers import (
    APP,
    UNKNOWN_ORIGINAL,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.models import Reference, ReferenceLeak

migration = import_module("marceline.migrations.0004_backfill_original_public_ids")


@pytest.fixture
def alpha(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def owner(alpha: Organisation) -> User:
    return add_member(alpha, "alpha-owner")


@pytest.fixture
def customer(owner: User) -> Client:
    return client_for(owner)


@pytest.fixture
def original(owner: User, make_page: Callable[[str], Page]) -> Reference:
    """An owned original with one confirmed copy that came down: every endpoint has data."""
    reference = make_reference(owner, title="Alpha's own original")
    page = make_page("alpha-leak")
    page.availability = Page.Availability.REMOVED
    page.availability_checked_at = timezone.now()
    page.save(update_fields=["availability", "availability_checked_at"])
    ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")
    return reference


# --- the columns ---------------------------------------------------------------------------------


def test_organisations_and_pages_get_an_id_on_create(
    db: None, make_page: Callable[[str], Page]
) -> None:
    first, second = make_organisation("Alpha Studio"), make_organisation("Bravo Creator")
    page = make_page("a-page")

    assert public_ids.is_valid("org", first.public_id)
    assert first.public_id != second.public_id
    assert public_ids.is_valid("page", page.public_id)
    page.refresh_from_db()
    assert public_ids.is_valid("page", page.public_id)


def test_the_database_fills_rows_written_without_the_orm(db: None) -> None:
    """A stale worker process that predates the column still inserts, and gets an id."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_organisation (name, segment, created_at) "
            "VALUES ('Raw One', 'professional', now()), ('Raw Two', 'professional', now())"
        )
    ids = list(Organisation.objects.values_list("public_id", flat=True))

    assert len(ids) == 2 and len(set(ids)) == 2
    assert all(public_ids.is_valid("org", value) for value in ids)


def test_adding_the_column_backfills_every_existing_row_with_its_own_id(
    db: None, make_page: Callable[[str], Page]
) -> None:
    """What `index/0016` and `accounts/0003` do to live tables: one fresh id per existing row."""
    pages = [make_page(f"existing-{number}") for number in range(5)]
    make_organisation("Existing Studio")
    with connection.cursor() as cursor:
        # The test transaction holds deferred foreign-key checks; ALTER TABLE refuses to run then.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with connection.schema_editor() as editor:
        for model in (Page, Organisation):
            field = model._meta.get_field("public_id")
            editor.remove_field(model, field)
            editor.add_field(model, field)

    backfilled = dict(Page.objects.values_list("pk", "public_id"))
    assert sorted(backfilled) == sorted(page.pk for page in pages)
    assert len(set(backfilled.values())) == 5
    assert all(public_ids.is_valid("page", value) for value in backfilled.values())
    assert all(backfilled[page.pk] != page.public_id for page in pages), "new, not the old ones"
    assert public_ids.is_valid("org", Organisation.objects.get().public_id)


def test_the_backfill_gives_every_owned_file_one_id_per_organisation(
    alpha: Organisation, owner: User
) -> None:
    first = make_reference(owner, title="first")
    twin = Reference.objects.create(
        creator=first.creator, title="first (2 fps)", sha256=first.sha256, frame_fps=2
    )
    OriginalOwnership.objects.create(reference=twin, organisation=alpha, assigned_via="import")
    second = make_reference(owner, title="second")
    make_reference(None, title="unassigned")
    OriginalPublicId.objects.all().delete()
    kept = OriginalPublicId.objects.create(organisation=alpha, sha256=second.sha256)

    counts = migration.backfill(OriginalOwnership, OriginalPublicId)

    assert counts == {"created": 1, "already_present": 1}
    rows = dict(OriginalPublicId.objects.values_list("sha256", "public_id"))
    assert set(rows) == {first.sha256, second.sha256}
    assert rows[second.sha256] == kept.public_id
    assert migration.backfill(OriginalOwnership, OriginalPublicId) == {
        "created": 0,
        "already_present": 2,
    }


# --- an original's id ----------------------------------------------------------------------------


def test_registering_an_owner_writes_the_id(alpha: Organisation) -> None:
    imported = make_reference(None, title="Operator import")
    assert not OriginalPublicId.objects.exists()

    register(imported, alpha, via=Via.IMPORT)

    row = OriginalPublicId.objects.get()
    assert (row.organisation, row.sha256) == (alpha, imported.sha256)
    assert public_ids.is_valid("orig", row.public_id)


def test_a_moved_file_gets_a_new_id_and_its_old_one_stops_answering(
    alpha: Organisation, owner: User, customer: Client, original: Reference
) -> None:
    bravo = make_organisation("Bravo Creator")
    bravo_owner = client_for(add_member(bravo, "bravo-owner"))
    old = public_id_of(original)

    reassign([original], bravo, via=Via.COMMAND)
    new = public_id_of(original)

    assert new != old
    assert OriginalPublicId.objects.filter(organisation=bravo, sha256=original.sha256).exists()
    for endpoint in ("originals", "cases"):
        missing = customer.get(f"{APP}/{endpoint}/{UNKNOWN_ORIGINAL}")
        assert customer.get(f"{APP}/{endpoint}/{old}").content == missing.content
        assert bravo_owner.get(f"{APP}/{endpoint}/{old}").status_code == 404
        assert bravo_owner.get(f"{APP}/{endpoint}/{new}").status_code == 200

    reassign([original], alpha, via=Via.COMMAND)
    assert public_id_of(original) == old, "back home, the old id again"


def test_an_ownership_row_written_elsewhere_still_gets_an_id_on_read(
    alpha: Organisation, customer: Client
) -> None:
    reference = make_reference(None, title="Fixed by hand in the database")
    OriginalOwnership.objects.create(reference=reference, organisation=alpha, assigned_via="admin")

    [row] = customer.get(f"{APP}/originals").json()

    assert row["id"] == OriginalPublicId.objects.get(sha256=reference.sha256).public_id
    assert owned_original(alpha, row["id"]) is not None


# --- no primary key leaves, and every bad id is the same 404 ------------------------------------


def id_fields(node: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for name, value in node.items():
            yield from id_fields(value, name)
    elif isinstance(node, list):
        for item in node:
            yield from id_fields(item, key)
    elif key == "id" or key.endswith("_id"):
        yield key, node


PREFIX_OF_FIELD = {"page_id": "page", "original_id": "orig"}


def test_no_customer_response_carries_an_integer_id(
    alpha: Organisation, customer: Client, original: Reference
) -> None:
    public_id = public_id_of(original)
    responses = {
        path: customer.get(f"{APP}/{path}").json()
        for path in (
            "me",
            "originals",
            "findings",
            "cases",
            "removals",
            f"originals/{public_id}",
            f"cases/{public_id}",
        )
    }

    seen: set[str] = set()
    for path, body in responses.items():
        for name, value in id_fields(body):
            seen.add(name)
            prefix = PREFIX_OF_FIELD.get(name) or ("org" if path == "me" else "orig")
            assert public_ids.is_valid(prefix, value), (path, name, value)
    assert seen == {"id", "page_id", "original_id"}
    assert responses["me"]["organisation"]["id"] == alpha.public_id


@pytest.mark.parametrize("endpoint", ["originals", "cases"])
def test_malformed_integer_unknown_and_foreign_ids_are_one_and_the_same_404(
    endpoint: str, customer: Client, original: Reference
) -> None:
    theirs = make_reference(
        add_member(make_organisation("Bravo Creator"), "bravo-owner"), title="Bravo's"
    )
    mine = public_id_of(original)
    missing = customer.get(f"{APP}/{endpoint}/{UNKNOWN_ORIGINAL}")
    bad = (
        str(original.pk),  # the old integer URL of an original this organisation owns
        "0",
        "-1",
        "99999999999999999999999999",
        "abc",
        mine.upper(),
        mine[:-1],
        mine + "0",
        mine.replace("orig_", "page_"),
        original.sha256,
        public_ids.missing("orig"),
        public_id_of(theirs),
    )

    assert missing.status_code == 404
    for value in bad:
        response = customer.get(f"{APP}/{endpoint}/{value}")
        assert response.status_code == 404, value
        assert response.content == missing.content, value
        assert response["Content-Type"] == missing["Content-Type"], value
    assert customer.get(f"{APP}/{endpoint}/{mine}").status_code == 200
