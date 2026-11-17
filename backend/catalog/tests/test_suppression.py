"""The suppression marker itself (docs/suppression.md): what it excludes and what it enforces.

Names here are invented. The whole point of the feature is that a real person's name should not
be in our data once they ask, so a test never needs one.
"""

from datetime import date

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog.admin import CreatorAdmin
from catalog.models import Creator, CreatorSuppression
from catalog.suppression import (
    active_suppressions,
    is_suppressed,
    suppressed_creator_ids,
    suppressed_names,
    suppression_of,
    without_suppressed,
)

pytestmark = pytest.mark.django_db


def suppress(creator: Creator, *, requested_on: date | None = None) -> CreatorSuppression:
    """The marker alone, without the deletions (`marceline/tests/test_suppression.py`)."""
    requested_on = requested_on or date(2026, 9, 17)
    return CreatorSuppression.objects.create(
        creator=creator,
        requested_on=requested_on,
        answer_due_on=date(requested_on.year, requested_on.month + 1, requested_on.day),
    )


@pytest.fixture
def creator(db: None) -> Creator:
    return Creator.objects.create(name="Nora Vale", aliases=["Nora V"])


def test_usable_excludes_suppressed_and_unreviewed_names(creator: Creator) -> None:
    other = Creator.objects.create(name="Dana Quill")
    typed = Creator.objects.create(name="Typo Name", needs_review=True)
    suppression = suppress(creator)

    assert set(Creator.objects.usable()) == {other}
    assert set(Creator.objects.all()) == {creator, other, typed}
    assert is_suppressed(creator) and not is_suppressed(other)
    assert suppressed_creator_ids() == {creator.pk}
    assert without_suppressed([creator.pk, other.pk]) == [other.pk]
    assert list(active_suppressions()) == [suppression]
    assert creator.suppressed
    # A creator with no suppression row at all is never reported as suppressed: the obvious
    # `filter(suppression__lifted_at__isnull=True)` is a left join and would match every one.
    assert suppressed_names([creator.name, other.name, "Nobody Here"]) == [creator.name]
    assert suppressed_names([other.name, typed.name]) == []


def test_a_lifted_suppression_stops_excluding_but_stays_as_the_record(creator: Creator) -> None:
    suppression = suppress(creator)

    suppression.lifted_at = timezone.now()
    suppression.lift_reason = "withdrawn by the person"
    suppression.save()

    assert list(Creator.objects.usable()) == [creator]
    assert suppressed_creator_ids() == set()
    assert not is_suppressed(creator)
    assert suppression_of(creator) == suppression  # the row is kept
    assert not Creator.objects.get(pk=creator.pk).suppressed


def test_lifting_without_a_reason_is_refused_by_the_database(creator: Creator) -> None:
    suppression = suppress(creator)
    suppression.lifted_at = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        suppression.save()


def test_a_suppressed_creator_cannot_be_deleted(creator: Creator) -> None:
    suppress(creator)

    with pytest.raises(ProtectedError), transaction.atomic():
        creator.delete()

    assert Creator.objects.filter(pk=creator.pk).exists()


def test_creator_changelist_reads_the_marker_in_one_query(creator: Creator) -> None:
    """The Suppressed column must not cost a query per row.

    `Creator.suppressed` follows the reverse one-to-one, so the changelist ran one extra query
    per creator on the page — 38 of 43 queries measured on the development database, and it grows
    with every prospect and watchlist name.
    """
    suppress(creator)
    for index in range(20):
        Creator.objects.create(name=f"Invented Name {index}")
    request = RequestFactory().get("/admin/catalog/creator/")
    request.user = User.objects.create_superuser("suppression-admin")
    admin = CreatorAdmin(Creator, AdminSite())
    changelist = admin.get_changelist_instance(request)

    with CaptureQueriesContext(connection) as queries:
        rows = list(changelist.get_queryset(request))
        flags = [admin.is_suppressed(row) for row in rows]

    assert len(rows) == 21
    assert sum(flags) == 1
    assert len(queries) == 1
