"""Copy extent in `match_references` (issue #62): stored, recomputed incrementally, no filter."""

import hashlib

import pytest

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching.copy_extent import CLASSIFIER_VERSION, requires_exception_review
from matching.models import Reference, ReferenceFrameHash, ReferenceMatch
from matching.references import match_references, refresh_copy_extents
from matching.tests.hashes import flip_bits, random_hashes

pytestmark = pytest.mark.django_db

DURATION_S = 600
# One distinct hash every ten seconds of each synthetic original.
FIRST = random_hashes(60, seed=621)
SECOND = random_hashes(60, seed=622)


def original(creator: Creator, title: str, hashes: list[bytes]) -> Reference:
    reference = Reference.objects.create(
        creator=creator,
        title=title,
        sha256=hashlib.sha256(title.encode()).hexdigest(),
        frame_fps=1,
        duration_s=DURATION_S,
    )
    ReferenceFrameHash.objects.bulk_create(
        ReferenceFrameHash(
            reference=reference,
            frame_index=10 * i,
            timestamp_s=10 * i,
            pdq=value,
            quality=100,
        )
        for i, value in enumerate(hashes)
    )
    return reference


def page(site: Site, slug: str, duration_s: float | None) -> Page:
    raw = {} if duration_s is None else {"duration_s": duration_s}
    return Page.objects.create(
        site=site, title=slug, url=f"https://{site.domain}/{slug}", raw_extraction=raw
    )


def hover(target: Page, hashes: list[bytes], start: int = 1) -> None:
    """Ordered hover frames `thumb_N.jpg`, close to (not equal to) the original's hashes."""
    for offset, value in enumerate(hashes):
        media = Media.objects.create(
            page=target,
            kind="frame",
            url=f"https://cdn.test/{target.pk}/thumb_{start + offset}.jpg",
        )
        FrameHash.objects.create(media=media, pdq=flip_bits(value, 4, seed=offset), quality=100)


def poster(target: Page, value: bytes) -> None:
    media = Media.objects.create(
        page=target, kind="poster", url=f"https://cdn.test/{target.pk}.jpg"
    )
    FrameHash.objects.create(media=media, pdq=flip_bits(value, 4), quality=100)


def extent(reference: Reference, target: Page) -> tuple[str, str]:
    row = ReferenceMatch.objects.get(reference=reference, page=target)
    assert row.copy_extent_version == CLASSIFIER_VERSION
    return row.copy_extent, row.copy_extent_reason


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Synthetic Creator")


def test_matching_stores_the_extent_and_hides_no_match(creator: Creator, site: Site) -> None:
    first = original(creator, "First", FIRST)
    full = page(site, "full", DURATION_S)
    hover(full, FIRST[2:58:6])  # 20 s .. 560 s: 0.90 of the original
    clip = page(site, "clip", 120)
    hover(clip, FIRST[20:32:2])  # 200 s .. 300 s of a two-minute page
    still = page(site, "still", 90)
    poster(still, FIRST[30])

    match_references(Reference.objects.all())

    full_row = ReferenceMatch.objects.get(reference=first, page=full)
    assert extent(first, full) == ("full_copy", "full_span")
    assert full_row.copy_span_s == 541
    assert full_row.copy_coverage == pytest.approx(541 / DURATION_S, abs=1e-4)
    assert requires_exception_review(full_row) is False
    assert extent(first, clip) == ("excerpt", "page_shorter")
    # A poster of a page far shorter than the original says which moment, not how much of it.
    assert extent(first, still) == ("unknown", "no_timeline")
    assert ReferenceMatch.objects.get(reference=first, page=still).copy_coverage is None
    # Clips are leaks: every page is still a match, with the band matching gives any match.
    assert set(ReferenceMatch.objects.values_list("page_id", flat=True)) == {
        full.pk,
        clip.pk,
        still.pk,
    }
    assert requires_exception_review(ReferenceMatch.objects.get(page=clip)) is True


def test_a_confirmed_excerpt_stays_confirmed_and_visible(creator: Creator, site: Site) -> None:
    first = original(creator, "First", FIRST)
    clip = page(site, "clip", 120)
    hover(clip, FIRST[20:32:2])
    match_references(Reference.objects.all())
    row = ReferenceMatch.objects.get(page=clip)
    ReferenceMatch.objects.filter(pk=row.pk).update(status="confirmed", decided_by="human")

    match_references(Reference.objects.all())

    row.refresh_from_db()
    assert (row.status, row.copy_extent) == ("confirmed", "excerpt")
    assert ReferenceMatch.objects.filter(reference=first).count() == 1


def test_a_subset_run_updates_the_other_originals_on_its_pages(
    creator: Creator, site: Site
) -> None:
    first = original(creator, "First", FIRST)
    compilation = page(site, "compilation", None)
    hover(compilation, FIRST[2:58:6])
    match_references(Reference.objects.all())
    assert extent(first, compilation) == ("full_copy", "full_span")

    second = original(creator, "Second", SECOND)
    hover(compilation, SECOND[10:40:6], start=20)
    # Only the new original is matched, yet the first original's row on that page learns of it.
    match_references(Reference.objects.filter(pk=second.pk))

    assert extent(first, compilation) == ("compilation_candidate", "other_originals")
    assert extent(second, compilation) == ("compilation_candidate", "other_originals")
    assert ReferenceMatch.objects.get(reference=first, page=compilation).copy_other_originals == 1

    # A human rejects the second original there: it no longer makes the page a compilation.
    ReferenceMatch.objects.filter(reference=second).update(status="rejected", decided_by="human")
    match_references(Reference.objects.filter(pk=second.pk))
    assert extent(first, compilation) == ("full_copy", "full_span")


def test_a_pipeline_twin_of_the_same_file_is_not_another_original(
    creator: Creator, site: Site
) -> None:
    first = original(creator, "First", FIRST)
    twin = Reference.objects.create(
        creator=creator,
        title="First on the newer grid",
        sha256=first.sha256,
        pipeline="ffmpeg-rgb512-pdq-v1",
        frame_fps=1,
        duration_s=DURATION_S,
    )
    ReferenceFrameHash.objects.bulk_create(
        ReferenceFrameHash(
            reference=twin, frame_index=10 * i, timestamp_s=10 * i, pdq=value, quality=100
        )
        for i, value in enumerate(FIRST)
    )
    full = page(site, "full", DURATION_S)
    hover(full, FIRST[2:58:6])

    match_references(Reference.objects.all())

    assert extent(first, full) == ("full_copy", "full_span")
    assert extent(twin, full) == ("full_copy", "full_span")


def test_rows_of_another_classifier_version_are_reclassified(creator: Creator, site: Site) -> None:
    first = original(creator, "First", FIRST)
    clip = page(site, "clip", 120)
    hover(clip, FIRST[20:32:2])
    match_references(Reference.objects.all())
    row = ReferenceMatch.objects.get(page=clip)
    updated_at = row.updated_at
    ReferenceMatch.objects.filter(pk=row.pk).update(
        copy_extent="full_copy", copy_extent_version=CLASSIFIER_VERSION - 1
    )
    row.refresh_from_db()
    assert requires_exception_review(row) is True  # stale rows are not trusted

    assert refresh_copy_extents(set()) == 1
    assert extent(first, clip) == ("excerpt", "page_shorter")
    row.refresh_from_db()
    assert row.updated_at == updated_at  # a derived value is not a decision
    assert refresh_copy_extents({clip.pk}) == 0
