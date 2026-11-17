"""The read-only mirror measurement of issue #8 (`manage.py mirror_report`).

The command exists to answer the issue's first acceptance criterion from the live index, which no
test can stand in for; what a test can do is prove the three counters mean what they say. Each
scenario is built from synthetic hashes: a page frame that only a flipped reference orientation
reaches, and a page pair that only the derived mirror links.
"""

from collections.abc import Callable

import pytest
from PIL import Image

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching.hamming import to_words
from matching.mirrors import (
    mirror_report,
    mirrored_page_pairs,
    orientation_wins,
    page_frames,
    reference_coverage,
)
from matching.models import Reference, ReferenceFrameHash
from matching.pdq import MIRROR_ORIENTATION, dihedral_from_hash, hash_image
from matching.tests.hashes import flip_bits, random_hashes
from matching.tests.images import synthetic_image

pytestmark = pytest.mark.django_db


def _reference(name: str = "Ref", digest: str = "a") -> Reference:
    creator = Creator.objects.create(name=name)
    return Reference.objects.create(
        creator=creator, title="t", sha256=digest * 64, frame_fps=1.0, duration_s=10.0
    )


def _frame(page: Page, pdq: bytes, index: int = 0) -> FrameHash:
    media, _ = Media.objects.get_or_create(
        page=page,
        url=f"https://cdn.example.test/{page.pk}.png",
        defaults={"kind": Media.Kind.POSTER},
    )
    return FrameHash.objects.create(media=media, frame_index=index, pdq=pdq, quality=100)


def test_reference_coverage_names_references_without_an_expansion() -> None:
    expanded = _reference("A", "a")
    plain = _reference("B", "b")
    hashes = random_hashes(8, seed=1)
    for orientation, value in enumerate(hashes):
        ReferenceFrameHash.objects.create(
            reference=expanded,
            frame_index=0,
            dihedral=orientation,
            quality=100,
            timestamp_s=0.0,
            pdq=value,
        )
    ReferenceFrameHash.objects.create(
        reference=plain, frame_index=0, dihedral=0, quality=100, timestamp_s=0.0, pdq=hashes[0]
    )
    coverage = reference_coverage()
    assert coverage.per_orientation[0] == 2
    assert coverage.per_orientation[7] == 1
    assert coverage.references_expanded == 1
    assert coverage.references_unflipped_only == [plain.pk]
    assert f"unflipped only: 1 (ids [{plain.pk}])" in "\n".join(coverage.lines())


def test_a_page_frame_only_a_flipped_orientation_reaches_is_counted(
    make_page: Callable[[str], Page],
) -> None:
    reference = _reference()
    unrelated, mirrored = random_hashes(2, seed=2)
    ReferenceFrameHash.objects.create(
        reference=reference, frame_index=0, dihedral=0, quality=100, timestamp_s=0.0, pdq=unrelated
    )
    ReferenceFrameHash.objects.create(
        reference=reference,
        frame_index=0,
        dihedral=MIRROR_ORIENTATION,
        quality=100,
        timestamp_s=0.0,
        pdq=mirrored,
    )
    page = make_page("mirrored")
    _frame(page, flip_bits(mirrored, 4, seed=9))

    page_ids, hashes = page_frames()
    wins = orientation_wins(page_ids, to_words(hashes), max_distance=36)
    assert wins.frames_with_a_hit == 1
    assert wins.best_orientation[MIRROR_ORIENTATION] == 1
    assert wins.only_flipped[MIRROR_ORIENTATION] == 1
    assert wins.only_flipped_pages == {page.pk}


def test_an_unflipped_hit_is_not_counted_as_flip_only(make_page: Callable[[str], Page]) -> None:
    reference = _reference()
    value = random_hashes(1, seed=3)[0]
    for orientation in range(8):
        ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=0,
            dihedral=orientation,
            quality=100,
            timestamp_s=0.0,
            pdq=flip_bits(value, orientation, seed=orientation),
        )
    page = make_page("plain")
    _frame(page, flip_bits(value, 2, seed=1))

    page_ids, hashes = page_frames()
    wins = orientation_wins(page_ids, to_words(hashes), max_distance=36)
    assert wins.frames_with_a_hit == 1
    assert sum(wins.only_flipped.values()) == 0
    assert not wins.only_flipped_pages


def test_the_derived_mirror_links_a_mirrored_page_pair(
    make_page: Callable[[str], Page], site: Site
) -> None:
    """Two pages showing the same picture, one of them mirrored: invisible to today's search."""
    image = synthetic_image(5)
    stored = hash_image(image).hash
    flipped = hash_image(image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)).hash
    first, second = make_page("a"), make_page("b")
    other = Site.objects.create(domain="other.test", name="Other")
    Page.objects.filter(pk=second.pk).update(site=other)
    _frame(first, stored)
    _frame(second, flipped)

    page_ids, hashes = page_frames()
    result = mirrored_page_pairs(page_ids, hashes, max_distance=36)
    assert result.plain_pairs == 0
    assert result.mirror_only_pairs == 1
    assert result.mirror_only_cross_site == 1
    assert result.mirror_only_unclustered == 1
    assert result.examples[0][:2] == (min(first.pk, second.pk), max(first.pk, second.pk))


def test_a_pair_the_plain_search_already_finds_is_not_reported_twice(
    make_page: Callable[[str], Page],
) -> None:
    value = random_hashes(1, seed=6)[0]
    first, second = make_page("a"), make_page("b")
    _frame(first, value)
    _frame(second, flip_bits(value, 3, seed=2))

    page_ids, hashes = page_frames()
    result = mirrored_page_pairs(page_ids, hashes, max_distance=36)
    assert result.plain_pairs == 1
    assert result.mirror_only_pairs == 0


def test_inactive_pages_are_left_out(make_page: Callable[[str], Page]) -> None:
    page = make_page("inactive")
    _frame(page, random_hashes(1, seed=7)[0])
    Page.objects.filter(pk=page.pk).update(active=False, inactive_reason="untargeted")
    assert page_frames() == ([], [])


def test_the_report_runs_on_an_empty_index_and_writes_nothing() -> None:
    lines = mirror_report()
    assert lines[0].startswith("Mirrored frames in the index")
    assert "Reference hashes: 0" in "\n".join(lines)
    assert not FrameHash.objects.exists()
    assert not ReferenceFrameHash.objects.exists()


def test_the_mirror_of_a_mirror_is_the_stored_hash() -> None:
    """The measurement only makes sense if the derivation is symmetric."""
    value = random_hashes(1, seed=8)[0]
    assert (
        dihedral_from_hash(dihedral_from_hash(value, MIRROR_ORIENTATION), MIRROR_ORIENTATION)
        == value
    )
