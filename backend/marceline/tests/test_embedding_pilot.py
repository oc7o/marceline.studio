"""The pilot's frozen exit criterion and its bounded page set (ADR 0037).

The arithmetic is tested because the *decision* is the deliverable: whoever reads the numbers must
not be able to argue about what they mean. The verdict strings are part of that — a failure on
precision names the pre-registered fallback, and a failure on recall says drop it.
"""

from collections.abc import Callable
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from marceline import embedding_pilot as pilot
from matching.embedding import EMBEDDING_PIPELINE
from matching.models import Reference, ReferenceFrameHash
from matching.tests.hashes import flip_bits, random_hashes
from matching.tests.sketches import random_sketches

PDQ = random_hashes(6, seed=51)
SKETCHES = random_sketches(6, seed=52)


def measurement(**overrides: object) -> pilot.Measurement:
    values: dict[str, object] = {
        "lost_pairs": 100,
        "lost_pairs_mutual": 80,
        "embedded_pages": 50,
        "candidate_pairs": 100,
        "near_miss_total": 31,
        "near_miss_reached": 9,
        "pipeline": EMBEDDING_PIPELINE,
    }
    values.update(overrides)
    return pilot.Measurement(**values)  # type: ignore[arg-type]


def test_the_criterion_is_the_one_that_was_pre_registered() -> None:
    """These three numbers are the contract; changing them is changing the decision."""
    assert pilot.MIN_LOST_PAIR_RECALL == 0.50
    assert pilot.MAX_CANDIDATES_PER_PAGE == 5.0
    assert pilot.MIN_NEAR_MISS_REACHED == 5


def test_all_three_criteria_met_means_adopt() -> None:
    result = measurement()
    assert result.lost_pair_recall == 0.8
    assert result.candidates_per_page == 2.0
    assert (result.recall_passes, result.precision_passes, result.findings_passes) == (
        True,
        True,
        True,
    )
    assert result.adopt
    assert result.verdict().startswith("ADOPT")


def test_recall_below_the_bar_means_drop_the_signal() -> None:
    result = measurement(lost_pairs_mutual=49)
    assert not result.recall_passes
    assert not result.adopt
    assert result.verdict().startswith("DROP")
    assert "160 MB" in result.verdict()


def test_no_lost_pairs_at_all_is_not_a_pass() -> None:
    """Nothing measured is not the same as a bar cleared."""
    assert not measurement(lost_pairs=0, lost_pairs_mutual=0).recall_passes


def test_precision_below_the_bar_means_the_pre_registered_fallback() -> None:
    result = measurement(candidate_pairs=300)
    assert result.recall_passes
    assert not result.precision_passes
    assert result.candidates_per_page == 6.0
    assert result.verdict().startswith("FALLBACK")
    assert "own shortlist" in result.verdict()
    assert "Do not raise the precision bar" in result.verdict()


def test_too_few_new_findings_means_do_not_adopt() -> None:
    result = measurement(near_miss_reached=4)
    assert result.recall_passes and result.precision_passes
    assert not result.adopt
    assert result.verdict().startswith("DO NOT ADOPT")


def test_exactly_at_the_bar_passes() -> None:
    result = measurement(
        lost_pairs=100, lost_pairs_mutual=50, candidate_pairs=250, near_miss_reached=5
    )
    assert result.adopt


def test_the_lines_carry_every_number_and_its_bar() -> None:
    lines = measurement().lines()
    assert any("PASS" in line for line in lines)
    assert any("bar: >= 50%" in line for line in lines)
    assert any("bar: <= 5" in line for line in lines)
    assert lines[-1].startswith("ADOPT")
    assert EMBEDDING_PIPELINE in lines[0]


# --- the page set, against a small real index ---

pytestmark = pytest.mark.django_db


@pytest.fixture
def original() -> Reference:
    creator = Creator.objects.create(name="C")
    reference = Reference.objects.create(
        creator=creator, title="t", sha256="a" * 64, frame_fps=1.0, duration_s=5.0
    )
    ReferenceFrameHash.objects.create(
        reference=reference, frame_index=0, timestamp_s=0.0, pdq=PDQ[0], quality=90, dihedral=0
    )
    return reference


def frame(page: Page, pdq: bytes, index: int = 0, sketch: bytes | None = None) -> FrameHash:
    media = Media.objects.create(
        page=page,
        url=f"https://cdn.example.test/{page.pk}-{index}.png",
        kind=Media.Kind.POSTER,
        status=Media.Status.DONE,
        eligible_at=timezone.now(),
        sha256=f"{page.pk:064d}",
    )
    return FrameHash.objects.create(
        media=media,
        frame_index=index,
        pdq=pdq,
        quality=90,
        embedding=sketch,
        embedding_pipeline=EMBEDDING_PIPELINE if sketch else "",
    )


def test_the_near_miss_band_is_what_the_matcher_cannot_reach(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    close = make_page("close")
    near = make_page("near")
    far = make_page("far")
    frame(close, flip_bits(PDQ[0], 10, seed=1))  # inside the matcher's threshold
    frame(near, flip_bits(PDQ[0], 40, seed=2))  # the band this pilot is about
    frame(far, PDQ[3])  # unrelated
    found = pilot.near_miss_pages()
    assert set(found) == {near.pk}
    assert pilot.NEAR_MISS_MIN <= found[near.pk] <= pilot.NEAR_MISS_MAX


def test_cross_site_pairs_exclude_mirrors_of_the_same_file(
    make_page: Callable[[str], Page],
) -> None:
    other = Site.objects.create(domain="other.test", name="Other")
    here = make_page("here")
    there = Page.objects.create(
        site=other, url="https://other.test/there", title="there", safety_score=1.0
    )
    frame(here, PDQ[1])
    frame(there, flip_bits(PDQ[1], 20, seed=3))
    pairs = pilot.cross_site_pairs()
    assert len(pairs) == 1
    assert {pairs[0].first_page, pairs[0].second_page} == {here.pk, there.pk}
    assert pairs[0].distance == 20
    # Same bytes on two sites is a mirror, not two encodes: not a pair for this measurement.
    Media.objects.update(sha256="c" * 64)
    assert pilot.cross_site_pairs() == []


def test_the_plan_says_when_the_reference_side_is_not_ready(
    original: Reference, make_page: Callable[[str], Page]
) -> None:
    frame(make_page("p"), flip_bits(PDQ[0], 40, seed=4))
    plan = pilot.plan()
    assert plan.references_total == 1
    assert plan.references_embedded == 0
    assert any("no original carries sketches yet" in line for line in plan.lines())


def test_the_plan_counts_the_media_per_site_against_its_budget(
    original: Reference, make_page: Callable[[str], Page], site: Site
) -> None:
    page = make_page("p")
    frame(page, flip_bits(PDQ[0], 40, seed=5))
    Site.objects.filter(pk=site.pk).update(daily_media_request_budget=10)
    plan = pilot.plan()
    assert plan.media_per_site == {site.domain: 1}
    assert plan.budget_per_site == {site.domain: 10}
    # Exactly this run's 10 % share of the budget: allowed.
    assert plan.over_budget() == {}
    # Half the budget: over the share, and the plan says so rather than spending it.
    Site.objects.filter(pk=site.pk).update(daily_media_request_budget=5)
    assert pilot.plan().over_budget() == {site.domain: (1, 0)}


def test_the_fetch_stage_refuses_without_apply() -> None:
    with pytest.raises(Exception, match="pass --apply"):
        call_command("embedding_pilot", "--stage", "fetch", stdout=StringIO())


def test_the_plan_stage_writes_nothing(original: Reference) -> None:
    out = StringIO()
    call_command("embedding_pilot", "--stage", "plan", stdout=out)
    assert "Nothing was requested or written." in out.getvalue()


def test_the_measure_stage_reports_no_data_rather_than_a_verdict_of_nothing(
    original: Reference,
) -> None:
    out = StringIO()
    call_command("embedding_pilot", "--stage", "measure", stdout=out)
    text = out.getvalue()
    assert "0 = 0.0%" in text.replace("0.0 %", "0.0%")
    assert "DROP" in text
