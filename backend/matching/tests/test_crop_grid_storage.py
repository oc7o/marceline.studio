"""One packed blob per reference moment, and the constraints that keep it readable (ADR 0039).

The storage decision this exercises: the grid is **one `bytea` on the `dihedral = 0` row**, not 256
rows. `matching_referenceframehash` costs 486 bytes per row, so 19.7 M grid rows would be 9.6 GB of
table; the blob is 8.2 KB per moment and 629 MB for the whole current library
(`docs/matching/reference-crop-grid.md` §8.2). Postgres TOASTs a value that size out of line, so the
hot table — the one `matching.reference_hashes` streams on every match run — stays as narrow as it
was, and a query that does not name the column never touches the grid at all.
"""

import pytest
from django.db import IntegrityError, transaction

from catalog.models import Creator
from matching import crop_grid
from matching.fingerprint import CROP_GRID_PIPELINE
from matching.models import Reference, ReferenceFrameHash

pytestmark = pytest.mark.django_db


@pytest.fixture
def reference() -> Reference:
    creator = Creator.objects.create(name="Grid Tester", needs_review=False)
    return Reference.objects.create(
        creator=creator,
        title="original",
        duration_s=10.0,
        sha256="d" * 64,
        frame_fps=1.0,
        pipeline=CROP_GRID_PIPELINE,
    )


def blob(crops: int = crop_grid.STAGE1_CROPS) -> bytes:
    return b"".join(bytes([index % 256]) * 32 for index in range(crops))


def test_a_moment_stores_its_whole_grid_in_one_column(reference: Reference) -> None:
    grid = blob(256)
    ReferenceFrameHash.objects.create(
        reference=reference,
        frame_index=0,
        timestamp_s=0.0,
        pdq=grid[:32],
        quality=90,
        crop_grid=grid,
    )
    stored = ReferenceFrameHash.objects.get(reference=reference, frame_index=0)
    assert bytes(stored.crop_grid or b"") == grid
    assert len(grid) == 8192
    # The reader agrees with the writer about the layout, prefix included.
    assert crop_grid.corner_hashes(bytes(stored.crop_grid or b""))[0] == bytes(stored.pdq)
    assert crop_grid.best_distance(bytes(stored.pdq), bytes(stored.crop_grid or b"")) == 0


def test_the_hot_query_never_loads_the_grid(reference: Reference) -> None:
    """`load_reference_hashes` names its columns, so a grid costs a match run nothing."""
    ReferenceFrameHash.objects.create(
        reference=reference,
        frame_index=0,
        timestamp_s=0.0,
        pdq=blob(1),
        quality=90,
        crop_grid=blob(256),
    )
    from matching.reference_hashes import load_reference_hashes

    snapshot = load_reference_hashes()
    assert len(snapshot) == 1
    assert not hasattr(snapshot, "crop_grid")


def test_a_grid_on_a_flipped_orientation_is_refused_by_the_database(reference: Reference) -> None:
    """Mirroring stays the job of the eight dihedral hashes; the grid is the unflipped moment."""
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=0,
            timestamp_s=0.0,
            pdq=bytes(32),
            quality=90,
            dihedral=3,
            crop_grid=blob(),
        )


def test_a_grid_that_is_not_whole_hashes_is_refused_by_the_database(reference: Reference) -> None:
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=1,
            timestamp_s=1.0,
            pdq=bytes(32),
            quality=90,
            crop_grid=blob() + b"\x00" * 7,
        )


def test_a_grid_too_short_for_the_stage_one_prefix_is_refused_by_the_database(
    reference: Reference,
) -> None:
    """Stage 1 reads the first 16 entries; a shorter blob would make it search other crops."""
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=2,
            timestamp_s=2.0,
            pdq=bytes(32),
            quality=90,
            crop_grid=blob(crop_grid.STAGE1_CROPS - 1),
        )


def test_no_grid_at_all_is_the_normal_case(reference: Reference) -> None:
    row = ReferenceFrameHash.objects.create(
        reference=reference, frame_index=3, timestamp_s=3.0, pdq=bytes(32), quality=90, dihedral=5
    )
    assert row.crop_grid is None
