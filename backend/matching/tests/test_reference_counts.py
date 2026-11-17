"""Per-original counters: right numbers, and never through a join.

The join form (`Count("frames__frame_index", distinct=True)` beside `Count("matches")`) multiplies
each original's frame-hash rows by its match rows. Measured on the development database
(44 originals, 614,360 reference frame hashes) the Reference changelist's one query took 3,680 ms
and the whole page 4,657 ms; with the scalar subqueries it is 217 ms of SQL and 270 ms.
The SQL assertions below are what keeps the fan-out from coming back — a wrong number is not how
this regresses, a slow plan is.
"""

import hashlib
from collections.abc import Callable

import pytest
from django.contrib.admin.sites import AdminSite
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from catalog.models import Creator
from index.models import Page
from matching.admin import ReferenceAdmin
from matching.models import Reference, ReferenceFrameHash, ReferenceMatch
from matching.reference_counts import (
    frame_positions_by_reference,
    reference_frame_positions,
    reference_match_count,
)

pytestmark = pytest.mark.django_db

FAN_OUT_JOINS = ('JOIN "matching_referenceframehash"', 'JOIN "matching_referencematch"')


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Synthetic Creator")


def make_reference(creator: Creator, title: str, *, positions: int, shared: int) -> Reference:
    """An original whose every sampled position is stored in all eight orientations."""
    reference = Reference.objects.create(
        creator=creator,
        title=title,
        sha256=hashlib.sha256(title.encode()).hexdigest(),
        frame_fps=1,
    )
    for position in range(positions):
        for dihedral in range(8):
            ReferenceFrameHash.objects.create(
                reference=reference,
                frame_index=position,
                timestamp_s=position,
                pdq=bytes((position, dihedral)) + bytes(30),
                quality=100,
                dihedral=dihedral,
                shared=position < shared,
            )
    return reference


def add_match(reference: Reference, page: Page) -> ReferenceMatch:
    return ReferenceMatch.objects.create(
        reference=reference,
        page=page,
        best_distance=0,
        matched_frames=1,
        page_frames=1,
        best_reference_timestamp_s=0.0,
    )


def test_positions_count_moments_not_rows(creator: Creator) -> None:
    reference = make_reference(creator, "a", positions=3, shared=1)
    row = Reference.objects.annotate(
        positions=reference_frame_positions(), shared=reference_frame_positions(shared=True)
    ).get(pk=reference.pk)
    assert ReferenceFrameHash.objects.filter(reference=reference).count() == 24
    assert row.positions == 3
    assert row.shared == 1


def test_counters_are_zero_without_rows(creator: Creator) -> None:
    reference = Reference.objects.create(
        creator=creator, title="empty", sha256="e" * 64, frame_fps=1
    )
    row = Reference.objects.annotate(
        positions=reference_frame_positions(),
        shared=reference_frame_positions(shared=True),
        match_rows=reference_match_count(),
    ).get(pk=reference.pk)
    assert (row.positions, row.shared, row.match_rows) == (0, 0, 0)


def test_matches_are_counted_per_original(
    creator: Creator, make_page: Callable[[str], Page]
) -> None:
    reference = make_reference(creator, "a", positions=2, shared=0)
    other = make_reference(creator, "b", positions=1, shared=0)
    for index in range(3):
        add_match(reference, make_page(f"p{index}"))
    counts = dict(
        Reference.objects.annotate(match_rows=reference_match_count()).values_list(
            "pk", "match_rows"
        )
    )
    assert counts == {reference.pk: 3, other.pk: 0}


def test_frame_positions_by_reference_is_one_query(creator: Creator) -> None:
    first = make_reference(creator, "a", positions=3, shared=1)
    second = make_reference(creator, "b", positions=2, shared=0)
    with CaptureQueriesContext(connection) as queries:
        positions = frame_positions_by_reference([first.pk, second.pk])
    assert positions == {first.pk: (3, 1), second.pk: (2, 0)}
    assert len(queries) == 1
    for join in FAN_OUT_JOINS:
        assert join not in queries[0]["sql"]


def test_changelist_queryset_joins_neither_frames_nor_matches(creator: Creator) -> None:
    make_reference(creator, "a", positions=2, shared=1)
    admin = ReferenceAdmin(Reference, AdminSite())
    sql = str(admin.get_queryset(RequestFactory().get("/admin/")).query)
    for join in FAN_OUT_JOINS:
        assert join not in sql, f"the changelist fans out over {join} again"


def test_changelist_counts_survive_several_matches(
    creator: Creator, make_page: Callable[[str], Page]
) -> None:
    """A join would count each frame position once per match row; the counters must not move."""
    reference = make_reference(creator, "a", positions=4, shared=2)
    admin = ReferenceAdmin(Reference, AdminSite())
    request = RequestFactory().get("/admin/")
    before = admin.get_queryset(request).get(pk=reference.pk)
    assert (admin.frames_count(before), admin.shared_frames_count(before)) == (4, 2)
    for index in range(2):
        add_match(reference, make_page(f"p{index}"))
    after = admin.get_queryset(request).get(pk=reference.pk)
    assert (admin.frames_count(after), admin.shared_frames_count(after)) == (4, 2)
    assert admin.matches_count(after) == 2
