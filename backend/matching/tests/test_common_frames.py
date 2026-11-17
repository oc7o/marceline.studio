import hashlib
from dataclasses import replace
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching.common_frames import (
    COMMON_MIN_PAGES,
    CandidateFrame,
    build_common_frames,
    common_frame_ids,
    copy_frames_required,
    find_common_frames,
    label_common_group,
    load_candidate_frames,
    pages_showing,
    same_file_groups,
)
from matching.models import (
    Cluster,
    ClusterMember,
    CommonFrameGroup,
    MediaReuse,
    Reference,
    ReferenceFrameHash,
    ReferenceMatch,
)
from matching.references import match_references
from matching.tests.hashes import flip_bits, random_hashes

LOGO, SCENE_A, SCENE_B, *OTHERS = random_hashes(20, seed=5)


def candidates(count: int, value: bytes = LOGO) -> list[CandidateFrame]:
    frames = []
    for page in range(count):
        frames.append(CandidateFrame(page * 10 + 1, page, page % 2, flip_bits(value, 3, seed=page)))
        frames.append(CandidateFrame(page * 10 + 2, page, page % 2, OTHERS[page]))
    return frames


def test_logo_across_unrelated_pages_grouped() -> None:
    durations = {page: 600.0 + 400 * page for page in range(7)}
    anchored, proposed, dropped = find_common_frames(candidates(7), [], {}, durations)
    assert anchored == [] and dropped == 0
    assert len(proposed) == 1
    group = proposed[0]
    assert group.pages == set(range(7)) and group.sites == {0, 1}
    assert {frame.frame_id for frame, _ in group.members} == {p * 10 + 1 for p in range(7)}
    assert all(distance <= 6 for _, distance in group.members)
    # Too few pages: nothing is common.
    assert find_common_frames(candidates(COMMON_MIN_PAGES - 1), [], {}, durations)[1] == []


def test_same_video_copies_not_grouped() -> None:
    frames = candidates(6)
    # All pages in one cluster (or mirror group) are one video.
    assert find_common_frames(frames, [], dict.fromkeys(range(6), "cluster"), {})[1] == []
    # Unclustered copies: same duration and a second shared frame.
    copies = [
        CandidateFrame(page * 10 + index, page, 0, flip_bits(value, 2, seed=page))
        for page in range(6)
        for index, value in ((1, LOGO), (2, SCENE_A))
    ]
    same_length = dict.fromkeys(range(6), 1800.0)
    assert find_common_frames(copies, [], {}, same_length)[1] == []
    different_lengths = {page: 600.0 + 400 * page for page in range(6)}
    assert len(find_common_frames(copies, [], {}, different_lengths)[1]) == 2


def posters(count: int, value: bytes = LOGO) -> list[CandidateFrame]:
    """One frame per page — the shape 64 % of the index has (0061 review §2.1)."""
    return [
        CandidateFrame(page * 10 + 1, page, 0, flip_bits(value, 2, seed=page))
        for page in range(count)
    ]


def test_copy_frames_required_never_asks_for_frames_a_page_has_not() -> None:
    assert copy_frames_required(6, 6) == 2
    assert copy_frames_required(2, 9) == 2
    assert copy_frames_required(1, 6) == 1
    assert copy_frames_required(1, 1) == 1
    assert copy_frames_required(0, 0) == 1
    # Unknown counts must not relax the rule.
    assert copy_frames_required(None, None) == 2
    assert copy_frames_required(None, 1) == 1


def test_poster_only_copies_of_one_video_are_one_video() -> None:
    """One video reposted on pages that each hold a single poster is not a common frame.

    The operator rejected three such proposals as "same video" on 2026-09-18: their pages carry
    one frame each, so the two shared frames the copy rule used to demand were unreachable and
    every repost counted as its own video.
    """
    reposts = posters(8)
    assert find_common_frames(reposts, [], {}, dict.fromkeys(range(8), 2247.0))[1] == []
    # A page with a second frame joins the same video as long as one frame and the duration agree.
    mixed = reposts + [CandidateFrame(page * 10 + 2, page, 0, OTHERS[page]) for page in range(4)]
    assert find_common_frames(mixed, [], {}, dict.fromkeys(range(8), 2247.0))[1] == []
    # Different videos that merely share one frame (a watermark on eight posters) stay a group.
    lengths = {page: 600.0 + 400 * page for page in range(8)}
    proposed = find_common_frames(reposts, [], {}, lengths)[1]
    assert len(proposed) == 1 and proposed[0].videos == 8


def test_multi_frame_pages_still_need_two_shared_frames() -> None:
    """Pages that have the frames to prove a copy must still show two of them."""
    frames = candidates(6)  # two frames per page, only the logo is shared
    proposed = find_common_frames(frames, [], {}, dict.fromkeys(range(6), 1800.0))[1]
    assert len(proposed) == 1 and proposed[0].videos == 6


def with_files(frames: list[CandidateFrame], files: int) -> list[CandidateFrame]:
    """The logo frame of page p comes from file `p % files`; other frames from their own file."""
    return [
        replace(frame, file_key=f"logo-{frame.page_id % files}")
        if frame.frame_id % 10 == 1
        else replace(frame, file_key=f"other-{frame.frame_id}")
        for frame in frames
    ]


def test_same_file_on_many_pages_is_one_occurrence() -> None:
    durations = {page: 600.0 + 400 * page for page in range(8)}
    # One placeholder image served on 8 video pages: one file, not a common frame.
    result = find_common_frames(with_files(candidates(8), 1), [], {}, durations)
    assert (result.proposed, result.dropped_same_file) == ([], 1)
    # Three files reposted across 8 pages are still only three occurrences.
    result = find_common_frames(with_files(candidates(8), 3), [], {}, durations)
    assert (result.proposed, result.dropped_same_file) == ([], 1)
    # Five distinct files (e.g. an intro re-encoded per upload) stay a group.
    result = find_common_frames(with_files(candidates(8), 5), [], {}, durations)
    assert len(result.proposed) == 1 and result.dropped_same_file == 0
    assert result.proposed[0].pages == set(range(8)) and result.proposed[0].videos == 5
    # Unknown files (no sha256) never merge pages.
    result = find_common_frames(candidates(8), [], {}, durations)
    assert len(result.proposed) == 1 and result.proposed[0].videos == 8


def test_same_file_groups_join_transitively_with_videos() -> None:
    frames = [
        CandidateFrame(1, 1, 0, LOGO, "a"),
        CandidateFrame(2, 2, 0, LOGO, "a"),
        CandidateFrame(3, 2, 0, LOGO, "b"),
        CandidateFrame(4, 3, 0, LOGO, "b"),
        CandidateFrame(5, 4, 0, LOGO, ""),
        CandidateFrame(6, 5, 0, LOGO, "c"),
        CandidateFrame(7, 6, 0, LOGO, ""),
    ]
    keys = same_file_groups([(frame, 0) for frame in frames], {5: "cluster", 6: "cluster"})
    assert keys[1] == keys[2] == keys[3]
    assert keys[5] == keys[6] != keys[1]
    assert 4 not in keys


def test_common_frame_ids_include_new_frames() -> None:
    frames = [(1, flip_bits(LOGO, 4)), (2, SCENE_A), (3, flip_bits(LOGO, 9))]
    assert common_frame_ids(frames, [(LOGO, 8)], [99]) == {1, 99}
    assert common_frame_ids(frames, [], [99]) == {99}


def page_with(
    site: Site, slug: str, values: list[bytes], duration: float, sha256: str = ""
) -> Page:
    page = Page.objects.create(
        site=site,
        title=slug,
        url=f"https://{site.domain}/{slug}",
        raw_extraction={"duration_s": duration},
    )
    media = Media.objects.create(
        page=page, kind="poster", url=f"https://{site.domain}/{slug}.jpg", sha256=sha256
    )
    for index, value in enumerate(values):
        FrameHash.objects.create(media=media, frame_index=index, pdq=value, quality=100)
    return page


@pytest.mark.django_db
def test_build_is_idempotent_and_keeps_human_labels(site: Site) -> None:
    pages = [
        page_with(site, f"intro-{i}", [flip_bits(LOGO, 2, seed=i), OTHERS[i]], 600 + 300 * i)
        for i in range(6)
    ]
    output = build_common_frames(dry_run=True)
    assert "Dry run" in output[-1]
    assert not CommonFrameGroup.objects.exists()
    buffer = StringIO()
    call_command("build_common_frames", "--min-pages", "5", "--max-distance", "8", stdout=buffer)
    assert "Proposed groups: 1 (1 created, 0 kept, 0 stale removed)" in buffer.getvalue()
    group = CommonFrameGroup.objects.get()
    assert (group.page_count, group.site_count, group.member_frame_count) == (6, 1, 6)
    assert group.kind == "unlabeled" and group.status == "proposed"
    assert group.sample_page_ids == [page.pk for page in pages]
    build_common_frames()
    assert CommonFrameGroup.objects.get().pk == group.pk

    label_common_group(group, "studio_intro", " Synthetic Studio ", "confirmed", label="Intro")
    group.refresh_from_db()
    assert (group.kind, group.studio_name, group.status, group.label) == (
        "studio_intro",
        "Synthetic Studio",
        "confirmed",
        "Intro",
    )
    assert group.labeled_at is not None
    newcomer = page_with(site, "new-upload", [flip_bits(LOGO, 5, seed=99)], 5000)
    output = build_common_frames()
    assert "Labeled groups kept: 1 (7 frames)" in output[1]
    group.refresh_from_db()
    assert (group.kind, group.studio_name, group.status, group.page_count) == (
        "studio_intro",
        "Synthetic Studio",
        "confirmed",
        7,
    )
    assert newcomer.pk in pages_showing(group)
    # A labeled group survives even when it no longer has enough pages.
    Page.objects.filter(pk__in=[page.pk for page in pages]).delete()
    build_common_frames()
    group.refresh_from_db()
    assert group.page_count == 1

    with pytest.raises(ValueError):
        label_common_group(group, "logo", "", "confirmed")
    with pytest.raises(ValueError):
        label_common_group(group, "generic", "", "maybe")
    with pytest.raises(ValueError):
        build_common_frames(min_pages=1)


@pytest.mark.django_db
def test_reposted_placeholder_file_not_proposed(site: Site) -> None:
    placeholder = hashlib.sha256(b"placeholder").hexdigest()
    pages = [
        page_with(site, f"video-{i}", [flip_bits(LOGO, 1, seed=i)], 600 + 300 * i, placeholder)
        for i in range(5)
    ]
    # A mirror CDN copy found by URL key: nothing downloaded, so no sha256 of its own.
    reused = page_with(site, "video-reused", [LOGO], 4000)
    MediaReuse.objects.create(
        media=reused.media.get(),
        source=pages[0].media.get(),
        reason=MediaReuse.Reason.URL_KEY,
        key="cdn/placeholder.jpg",
    )
    assert {frame.file_key for frame in load_candidate_frames()} == {placeholder}

    output = build_common_frames(dry_run=True)
    assert "Proposed groups: 0 " in output[2]
    assert "Dropped as one file on several pages (same sha256 counts once): 1 " in output[3]
    assert not CommonFrameGroup.objects.exists()

    # Distinct files carrying the same frame (an intro in each upload) are still common.
    for i in range(5):
        page_with(site, f"intro-{i}", [flip_bits(LOGO, 2, seed=50 + i)], 700 + 300 * i, f"{i:064x}")
    output = build_common_frames()
    assert "Proposed groups: 1 " in output[2] and ": 0 candidate groups" in output[3]
    assert CommonFrameGroup.objects.get().page_count == 11


@pytest.mark.django_db
def test_include_inactive_widens_the_labeling_queue_only(site: Site) -> None:
    """Branding on untargeted pages (issue #71) is invisible to the daily rebuild; a reviewer can
    ask for it explicitly (docs/matching/0061-common-frames-review.md)."""
    pages = [
        page_with(site, f"intro-{i}", [flip_bits(LOGO, 2, seed=i)], 600 + 300 * i, f"{i:064x}")
        for i in range(5)
    ]
    Page.objects.filter(pk__in=[page.pk for page in pages[:2]]).update(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED
    )

    assert len(load_candidate_frames()) == 3
    assert len(load_candidate_frames(include_inactive=True)) == 5
    assert "Proposed groups: 0 " in build_common_frames()[2]
    assert not CommonFrameGroup.objects.exists()

    lines = build_common_frames(include_inactive=True)
    assert "active and inactive pages" in lines[0]
    assert "Proposed groups: 1 " in lines[2]
    group = CommonFrameGroup.objects.get()
    assert group.page_count == 5 and group.status == CommonFrameGroup.Status.PROPOSED

    buffer = StringIO()
    call_command("build_common_frames", "--include-inactive", "--dry-run", stdout=buffer)
    assert "active and inactive pages" in buffer.getvalue()


@pytest.mark.django_db
def test_include_inactive_proposal_needs_labeling_before_the_next_scheduled_run(
    site: Site,
) -> None:
    """issue #16: the scheduler always runs the active-only default (ADR 0012); an unlabeled
    proposal that only exists because of inactive-page frames cannot be reused there and is swept
    away as stale on the very next run, same as any other unlabeled group going stale
    (`test_stale_unlabeled_groups_removed`). A group a human labels first survives instead, because
    labeled groups are rebuild anchors regardless of where their frames were found."""
    pages = [
        page_with(site, f"intro-{i}", [flip_bits(LOGO, 2, seed=i)], 600 + 300 * i, f"{i:064x}")
        for i in range(5)
    ]
    Page.objects.filter(pk__in=[page.pk for page in pages[:2]]).update(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED
    )

    lines = build_common_frames(include_inactive=True)
    assert "Proposed groups: 1 " in lines[2]
    group = CommonFrameGroup.objects.get()
    assert group.status == CommonFrameGroup.Status.PROPOSED and group.labeled_at is None

    # Left unlabeled, the scheduled active-only rebuild cannot reuse it (only 3 active pages, one
    # below min_pages) and deletes it as stale.
    assert "1 stale removed" in build_common_frames()[2]
    assert not CommonFrameGroup.objects.exists()

    # Labeled first, the same group survives the active-only rebuild as an anchor.
    lines = build_common_frames(include_inactive=True)
    group = CommonFrameGroup.objects.get()
    label_common_group(group, "studio_intro", "Synthetic Studio", "confirmed", label="Intro")
    output = build_common_frames()
    assert "Labeled groups kept: 1 " in output[1]
    group.refresh_from_db()
    assert group.status == CommonFrameGroup.Status.CONFIRMED


@pytest.mark.django_db
def test_stale_unlabeled_groups_removed(site: Site) -> None:
    for i in range(5):
        page_with(site, f"intro-{i}", [flip_bits(LOGO, 2, seed=i)], 600 + 300 * i)
    build_common_frames()
    assert CommonFrameGroup.objects.count() == 1
    FrameHash.objects.filter(media__page__title="intro-0").delete()
    assert "1 stale removed" in build_common_frames()[2]
    assert not CommonFrameGroup.objects.exists()


@pytest.mark.django_db
def test_common_frames_discounted_in_matching(site: Site) -> None:
    creator = Creator.objects.create(name="Synthetic Creator")
    reference = Reference.objects.create(
        creator=creator, title="Original", sha256=hashlib.sha256(b"o").hexdigest(), frame_fps=1
    )
    for second, value in ((0, LOGO), (40, SCENE_A)):
        ReferenceFrameHash.objects.create(
            reference=reference, frame_index=second, timestamp_s=second, pdq=value, quality=100
        )
    page = page_with(site, "leak", [flip_bits(LOGO, 4), flip_bits(SCENE_A, 6)], 1000)
    match_references([reference])
    match = ReferenceMatch.objects.get()
    assert (match.strength, match.evidence_frames, match.common_frames) == ("strong", 2, 0)

    cluster = Cluster.objects.create()
    for i in range(6):
        other = page_with(site, f"intro-{i}", [flip_bits(LOGO, 3, seed=i)], 300 + 500 * i)
        # The creator's other videos carry the studio intro too; linked pages never make the
        # reference frame itself shared (ADR 0010), so only the common group discounts it.
        other.creators.add(creator)
        if i < 2:
            ClusterMember.objects.create(cluster=cluster, page=other, evidence="manual")
    build_common_frames()
    group = CommonFrameGroup.objects.get()
    assert page.pk in pages_showing(group)
    # A proposal is only a guess: it discounts nothing until a human confirms it.
    match_references([reference])
    match.refresh_from_db()
    assert (match.strength, match.evidence_frames, match.common_frames) == ("strong", 2, 0)

    label_common_group(group, "studio_intro", "Synthetic Studio", "proposed")
    match_references([reference])
    match.refresh_from_db()
    assert (match.strength, match.common_frames) == ("strong", 0)

    label_common_group(group, "studio_intro", "Synthetic Studio", "confirmed")
    match_references([reference])
    match.refresh_from_db()
    assert (match.strength, match.evidence_frames, match.common_frames) == ("weak", 1, 1)
    assert match.matched_seconds == [40] and match.common_seconds == [0]

    label_common_group(group, "not_common", "", "confirmed")
    match_references([reference])
    match.refresh_from_db()
    assert (match.strength, match.common_frames) == ("strong", 0)
    label_common_group(group, "watermark", "", "rejected")
    match_references([reference])
    match.refresh_from_db()
    assert match.common_frames == 0


@pytest.mark.django_db
def test_admin_pages(site: Site, admin_client: Client) -> None:
    for i in range(5):
        page_with(site, f"intro-{i}", [flip_bits(LOGO, 2, seed=i)], 600 + 300 * i)
    build_common_frames()
    group = CommonFrameGroup.objects.get()
    for url in (
        "/admin/matching/commonframegroup/",
        f"/admin/matching/commonframegroup/{group.pk}/change/",
        f"/admin/matching/commonframemember/?group__id__exact={group.pk}",
        "/admin/matching/confidencesettings/",
    ):
        assert admin_client.get(url).status_code == 200
    response = admin_client.post(
        f"/admin/matching/commonframegroup/{group.pk}/change/",
        {"label": "Card", "kind": "generic", "studio_name": "", "status": "confirmed"},
    )
    assert response.status_code == 302
    group.refresh_from_db()
    assert group.kind == "generic" and group.labeled_at is not None
