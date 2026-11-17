import io
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from PIL import ImageOps

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching import references as references_module
from matching.admin import ReferenceAdmin, ReferenceMatchAdmin
from matching.creators import creator_names
from matching.fingerprint import PIPELINE, PIPELINE_V0, pipeline_label
from matching.frames import iter_video_frames
from matching.incremental import fingerprint, scan_settings
from matching.m2 import m2_report
from matching.management.commands.match_references import Command as MatchReferences
from matching.models import Cluster, ClusterMember, Reference, ReferenceFrameHash, ReferenceMatch
from matching.pdq import MIN_QUALITY, hash_image
from matching.references import LOOSE_MAX_DISTANCE, STRICT_MAX_DISTANCE, select_references
from matching.tests.images import synthetic_image
from matching.tests.test_frames import make_video

pytestmark = pytest.mark.django_db


@pytest.fixture
def original(tmp_path: Path) -> Path:
    make_video(tmp_path, "testsrc=duration=3:size=320x240:rate=10")
    return tmp_path / "clip.mp4"


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Synthetic Creator", aliases=["Alias"])


def run(name: str, *args: str) -> str:
    output = io.StringIO()
    call_command(name, *args, stdout=output)
    return output.getvalue()


def test_import(original: Path, creator: Creator) -> None:
    before = original.read_bytes()
    run(
        "add_reference",
        "--creator",
        "aLIas",
        "--file",
        str(original),
        "--fps",
        "2",
        "--title",
        "Original",
        "--source-note",
        "Synthetic test",
    )
    reference = Reference.objects.get()
    assert reference.creator == creator
    assert reference.title == "Original"
    assert reference.source_note == "Synthetic test"
    assert reference.duration_s == pytest.approx(3)
    assert reference.frame_fps == 2
    assert reference.frames.count() == 6 * 8
    assert set(reference.frames.values_list("dihedral", flat=True)) == set(range(8))
    assert set(reference.frames.values_list("timestamp_s", flat=True)) == {0, 0.5, 1, 1.5, 2, 2.5}
    assert all(len(row.pdq) == 32 for row in reference.frames.all())
    assert original.read_bytes() == before
    assert list(original.parent.iterdir()) == [original]
    with pytest.raises(CommandError, match="already been imported"):
        run("add_reference", "--creator", creator.name, "--file", str(original), "--fps", "2")
    assert Reference.objects.count() == 1
    # Another sampling rate is another grid of the same file: it imports beside (ADR 0022).
    run("add_reference", "--creator", creator.name, "--file", str(original))
    assert list(Reference.objects.order_by("frame_fps").values_list("frame_fps", flat=True)) == [
        1.0,
        2.0,
    ]


def test_unknown_creator_and_invalid_import(original: Path, creator: Creator) -> None:
    with pytest.raises(CommandError, match="creator"):
        run("add_reference", "--creator", "unknown", "--file", str(original))
    for rate in ("0", "-1", "nan", "inf"):
        with pytest.raises(CommandError, match="fps"):
            run("add_reference", "--creator", creator.name, "--file", str(original), "--fps", rate)
    original.write_text("invalid synthetic input")
    with pytest.raises(CommandError, match="ffprobe"):
        run("add_reference", "--creator", creator.name, "--file", str(original))
    assert not Reference.objects.exists()


def test_several_creators(original: Path, creator: Creator) -> None:
    """`--creator` is repeatable: the first is primary, the rest co-performers (issue #64)."""
    partner = Creator.objects.create(name="Carla Bremer")
    run(
        "add_reference",
        "--creator",
        "aLIas",
        "--creator",
        "Carla Bremer",
        # A repeated name collapses instead of being stored twice.
        "--creator",
        "Alias",
        "--file",
        str(original),
    )
    reference = Reference.objects.get()
    assert reference.creator == creator
    assert set(reference.creators.values_list("pk", flat=True)) == {creator.pk, partner.pk}
    assert creator_names(reference) == ["Synthetic Creator", "Carla Bremer"]
    # Any performer finds the original; somebody else does not.
    assert list(select_references(creator="Carla Bremer")) == [reference]
    assert list(select_references(creator="aLIas")) == [reference]
    Creator.objects.create(name="Someone Else")
    assert list(select_references(creator="Someone Else")) == []
    # An unknown co-performer fails the whole import rather than dropping a name.
    with pytest.raises(CommandError, match="creator"):
        run(
            "add_reference",
            "--creator",
            creator.name,
            "--creator",
            "nobody",
            "--file",
            str(original),
        )
    assert Reference.objects.count() == 1


def test_co_performer_pages_are_creator_linked(
    original: Path, creator: Creator, site: Site
) -> None:
    """The "creator-linked pages NOT matched" recall helper covers every performer."""
    partner = Creator.objects.create(name="Carla Bremer")
    run(
        "add_reference",
        "--creator",
        creator.name,
        "--creator",
        partner.name,
        "--file",
        str(original),
    )
    unmatched = page_frame(site, "partner", hash_image(synthetic_image(99)).hash).media.page
    unmatched.creators.add(partner)
    output = run("match_references", "--creator", partner.name)
    assert "1 creator-linked pages NOT matched" in output
    # Only the count by default; the list of pages needs -v 2.
    assert unmatched.url not in output and "  NOT matched:" not in output
    assert unmatched.url in run("match_references", "--creator", partner.name, "-v", "2")


def test_no_dihedral_and_no_cap(original: Path, creator: Creator) -> None:
    run(
        "add_reference",
        "--creator",
        creator.name,
        "--file",
        str(original),
        "--fps",
        "30",
        "--no-dihedral",
    )
    reference = Reference.objects.get()
    assert reference.frames.count() == 90
    assert set(reference.frames.values_list("dihedral", flat=True)) == {0}


def test_low_quality(tmp_path: Path, creator: Creator) -> None:
    make_video(tmp_path, "color=c=black:size=320x240:duration=2")
    run("add_reference", "--creator", creator.name, "--file", str(tmp_path / "clip.mp4"))
    assert Reference.objects.count() == 1
    assert not ReferenceFrameHash.objects.exists()


def page_frame(site: Site, slug: str, value: bytes, quality: int = 100) -> FrameHash:
    page = Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")
    media = Media.objects.create(page=page, kind="frame", url=f"https://{site.domain}/{slug}.png")
    return FrameHash.objects.create(media=media, pdq=value, quality=quality)


def test_matching_and_report(original: Path, creator: Creator, site: Site) -> None:
    run("add_reference", "--creator", creator.name, "--file", str(original))
    reference = Reference.objects.get()
    _, image = next(iter(iter_video_frames(original, fps=1)))
    normal = page_frame(site, "normal", hash_image(image).hash)
    second_site = Site.objects.create(domain="second.test")
    mirrored = page_frame(second_site, "mirror", hash_image(ImageOps.mirror(image)).hash)
    unrelated = page_frame(site, "unrelated", hash_image(synthetic_image(99)).hash)
    unrelated.media.page.creators.add(creator)
    low = page_frame(site, "low", bytes(normal.pdq), quality=0)
    FrameHash.objects.create(media=normal.media, frame_index=1, pdq=unrelated.pdq, quality=100)
    cluster = Cluster.objects.create()
    for frame in (normal, mirrored, unrelated):
        ClusterMember.objects.create(cluster=cluster, page=frame.media.page, evidence="manual")
    output = run("match_references", "--creator", "alias", "--verbosity", "2")
    assert (
        "2 pages matched (0 strong, 2 weak), 1 clusters matched; "
        "1 creator-linked pages NOT matched" in output
    )
    assert unrelated.media.page.url in output
    assert not ReferenceMatch.objects.filter(page=low.media.page).exists()
    match = ReferenceMatch.objects.get(page=normal.media.page)
    assert match.best_distance == 0
    assert match.matched_frames == 1 and match.page_frames == 2
    assert match.cluster == cluster
    assert match.best_reference_timestamp_s == 0
    assert ReferenceMatch.objects.filter(page=mirrored.media.page).exists()
    match.status = "confirmed"
    match.save()
    mirrored_match = ReferenceMatch.objects.get(page=mirrored.media.page)
    mirrored_match.status = "rejected"
    mirrored_match.save()
    run("match_references", "--reference", str(reference.pk))
    match.refresh_from_db()
    mirrored_match.refresh_from_db()
    assert match.status == "confirmed"
    assert mirrored_match.status == "rejected"
    report = run("reference_report", "--creator", "ALIAS")
    assert "1 confirmed, 1 rejected (false matches), 0 proposed (unreviewed)" in report
    assert f"Cluster {cluster.pk}: 1/3 member pages matched" in report
    assert "Total:" in report
    ReferenceFrameHash.objects.all().delete()
    run("match_references")
    assert ReferenceMatch.objects.count() == 2  # Human decisions survive loss of evidence.


def test_stale_proposals_scoped(original: Path, creator: Creator, site: Site) -> None:
    run("add_reference", "--creator", creator.name, "--file", str(original))
    reference = Reference.objects.get()
    frame = reference.frames.first()
    assert frame is not None
    page_frame(site, "match", bytes(frame.pdq))
    run("match_references")
    assert ReferenceMatch.objects.count() == 1
    reference.frames.all().delete()
    run("match_references", "--reference", str(reference.pk))
    assert not ReferenceMatch.objects.exists()
    with pytest.raises(CommandError):
        run("match_references", "--reference", "999999")
    with pytest.raises(CommandError):
        run("match_references", "--max-distance", "257")


def test_import_failure_rolls_back(original: Path, creator: Creator) -> None:
    with (
        patch(
            "matching.references.ReferenceFrameHash.objects.bulk_create",
            side_effect=ValueError("failure"),
        ),
        pytest.raises(CommandError, match="failure"),
    ):
        run("add_reference", "--creator", creator.name, "--file", str(original))
    assert not Reference.objects.exists()


def test_constraints_and_admin(original: Path, creator: Creator, site: Site) -> None:
    run("add_reference", "--creator", creator.name, "--file", str(original))
    reference = Reference.objects.get()
    for value, orientation in ((b"short", 0), (bytes(32), 8)):
        with pytest.raises(IntegrityError), transaction.atomic():
            ReferenceFrameHash.objects.create(
                reference=reference,
                frame_index=100,
                timestamp_s=100,
                pdq=value,
                quality=100,
                dihedral=orientation,
            )
    frame = reference.frames.first()
    assert frame is not None
    page_frame(site, "match", bytes(frame.pdq))
    run("match_references")
    request = RequestFactory().get("/admin/")
    admin = ReferenceAdmin(Reference, AdminSite())
    assert not admin.has_add_permission(request)
    assert admin.frames_count(admin.get_queryset(request).get()) == 3
    assert admin.matches_count(admin.get_queryset(request).get()) == 1
    match_admin = ReferenceMatchAdmin(ReferenceMatch, AdminSite())
    match = ReferenceMatch.objects.get()
    assert match_admin.frame_ratio(match) == "1/1"
    assert "https://example.test/match" in match_admin.page_url(match)


def test_scope_quality_and_current_clusters(creator: Creator, site: Site) -> None:
    value = hash_image(synthetic_image(42)).hash
    references = [
        Reference.objects.create(
            creator=creator, title=f"Original {i}", sha256=str(i) * 64, frame_fps=1
        )
        for i in range(2)
    ]
    for reference in references:
        for orientation in range(8):
            ReferenceFrameHash.objects.create(
                reference=reference,
                frame_index=0,
                timestamp_s=2,
                pdq=value,
                quality=70,
                dihedral=orientation,
            )
    frame = page_frame(site, "shared", value, quality=80)
    run("match_references")
    assert ReferenceMatch.objects.count() == 2
    assert set(ReferenceMatch.objects.values_list("matched_frames", flat=True)) == {1}
    assert "2 proposed (unreviewed)" in run("reference_report")
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=frame.media.page, evidence="manual")
    run("match_references", "--reference", str(references[0].pk))
    assert ReferenceMatch.objects.get(reference=references[0]).cluster == cluster
    assert ReferenceMatch.objects.get(reference=references[1]).cluster is None
    # Report follows current memberships, even for a match not refreshed by this run.
    assert run("reference_report").count(f"Cluster {cluster.pk}: 1/1") == 3
    run("match_references", "--reference", str(references[0].pk), "--min-quality", "71")
    assert not ReferenceMatch.objects.filter(reference=references[0]).exists()
    assert ReferenceMatch.objects.filter(reference=references[1]).exists()


def test_stream_cleanup(original: Path) -> None:
    import subprocess

    real_popen = subprocess.Popen
    with patch("matching.frames.subprocess.Popen", wraps=real_popen) as popen:
        frames = iter(iter_video_frames(original, fps=1))
        next(frames)
        # Drop the generator before exhausting the video; its finally block reaps ffmpeg.
        del frames
        assert popen.call_count == 1


def test_report_names_the_grid_a_file_cannot_retire(creator: Creator, site: Site) -> None:
    """One file on two grids: the report says which row finds pages the other misses (issue #73).

    The v0 and v1 rows of an original sample the same seconds half a second apart, so which row a
    page's single poster lands on is decided per page and neither row can be retired on principle.
    """
    digest = "c" * 64
    both = hash_image(synthetic_image(11)).hash
    only_v1 = hash_image(synthetic_image(12)).hash
    rows = {}
    for tag, values in ((PIPELINE_V0, [both]), (PIPELINE, [both, only_v1])):
        reference = Reference.objects.create(
            creator=creator, title="Original", sha256=digest, frame_fps=1, pipeline=tag
        )
        rows[tag] = reference
        for index, value in enumerate(values):
            ReferenceFrameHash.objects.create(
                reference=reference, frame_index=index, timestamp_s=index, pdq=value, quality=100
            )
    shared_page = page_frame(site, "on-both-grids", both)
    v1_page = page_frame(site, "on-the-v1-grid", only_v1)
    run("match_references")
    v0, v1 = rows[PIPELINE_V0], rows[PIPELINE]
    # The same-file exemption of ADR 0022 holds: neither row calls the other's frame common footage.
    assert not ReferenceFrameHash.objects.filter(shared=True).exists()
    assert {(m.reference_id, m.page_id) for m in ReferenceMatch.objects.all()} == {
        (v0.pk, shared_page.media.page_id),
        (v1.pk, shared_page.media.page_id),
        (v1.pk, v1_page.media.page_id),
    }
    report = run("reference_report")
    assert f"file {digest[:12]}: 2 grids, 2 page(s) in the union" in report
    assert f"reference {v0.pk} on {pipeline_label(PIPELINE_V0)}" in report
    assert "1 page(s) matched, none of its own" in report
    assert f"reference {v1.pk} on {pipeline_label(PIPELINE)}" in report
    assert f"2 page(s) matched, 1 of its own: {v1_page.media.page_id}" in report
    # One grid per file says nothing about retirement, so the section stays out of the report.
    v0.delete()
    assert "grids," not in run("reference_report")


def test_one_strict_threshold_everywhere() -> None:
    """`STRICT_MAX_DISTANCE` is the reference matcher's only strict threshold (ADR 0015).

    Every entry point that admits a page frame as evidence on its own must read it - the CLI, the
    M2 measurement and the staff overview included - or the matcher and the numbers reported about
    it silently measure different things. Duplicate detection and cluster building are a separate
    decision and keep `matching.duplicates.DEFAULT_MAX_DISTANCE`; this test asserts they are
    allowed to differ, not that they must.
    """
    assert STRICT_MAX_DISTANCE == 36
    # A strict frame is evidence on its own; a loose one needs a timeline or corroboration.
    assert STRICT_MAX_DISTANCE <= LOOSE_MAX_DISTANCE

    parser = MatchReferences().create_parser("manage.py", "match_references")
    defaults = parser.parse_args([])
    assert defaults.max_distance == STRICT_MAX_DISTANCE
    assert defaults.loose_max_distance == LOOSE_MAX_DISTANCE

    assert signature(m2_report).parameters["max_distance"].default == STRICT_MAX_DISTANCE
    for name in (
        "find_shared_frames",
        "shared_frame_keys",
        "mark_shared_frames",
        "PageEvidence",
        "aggregate_matches",
        "aggregate_hash_matches",
        "match_references",
    ):
        function = getattr(references_module, name)
        assert signature(function).parameters["max_distance"].default == STRICT_MAX_DISTANCE, name


def test_a_strict_threshold_change_needs_no_full_rescan() -> None:
    """The candidate cache is keyed on the scan *radius*, which is the larger of the two distances.

    So moving the strict threshold anywhere at or below `LOOSE_MAX_DISTANCE` leaves the cache
    valid: the same (frame, reference, distance) triples are within reach, and every run
    re-aggregates evidence from them. Only a change to the loose distance (or the quality floor,
    the hash size or the search backend) rebuilds it. Measured on the live index on 2026-09-18:
    a full rescan costs 113 s, against a 3 h scheduler cadence.
    """

    def settings_at(strict: int) -> dict[str, object]:
        return scan_settings(radius=max(strict, LOOSE_MAX_DISTANCE), min_quality=MIN_QUALITY)

    assert fingerprint(settings_at(31)) == fingerprint(settings_at(STRICT_MAX_DISTANCE))
    assert fingerprint(settings_at(STRICT_MAX_DISTANCE)) != fingerprint(
        scan_settings(radius=LOOSE_MAX_DISTANCE - 1, min_quality=MIN_QUALITY)
    )
