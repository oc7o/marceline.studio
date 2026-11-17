from collections.abc import Callable
from io import StringIO

import pytest
from django.core.management import call_command

from catalog.models import Site
from index.models import FrameHash, Media, Page
from matching.models import PairLabel
from matching.services import (
    find_candidates,
    label_pair,
    labeled_pairs,
    load_page_hashes,
    propose_pairs,
)
from matching.tests.hashes import flip_bits, random_hashes

MakePage = Callable[[str], Page]


def add_frames(page: Page, hashes: list[bytes], quality: int = 90) -> None:
    media = Media.objects.create(page=page, kind=Media.Kind.POSTER, url=f"{page.url}/poster.jpg")
    for index, hash_ in enumerate(hashes):
        FrameHash.objects.create(media=media, frame_index=index, pdq=hash_, quality=quality)


@pytest.fixture
def pages(make_page: MakePage) -> dict[str, Page]:
    """p1 and p2 are duplicates on one site, p3 duplicates p1 on another site, p4 is unrelated."""
    x, y, z = random_hashes(3, seed=20)
    other_site = Site.objects.create(domain="mirror.test", name="Mirror")
    p1, p2, p4 = make_page("p1"), make_page("p2"), make_page("p4")
    p3 = Page.objects.create(site=other_site, url="https://mirror.test/p3", title="p3")
    add_frames(p1, [x, y])
    add_frames(p2, [flip_bits(x, 3), flip_bits(y, 5)])
    add_frames(p3, [flip_bits(x, 7)])
    add_frames(p4, [z])
    return {"p1": p1, "p2": p2, "p3": p3, "p4": p4}


def test_load_page_hashes_filters_quality(make_page: MakePage) -> None:
    page = make_page("q")
    good, bad = random_hashes(2, seed=21)
    add_frames(page, [good])
    add_frames(
        Page.objects.create(site=page.site, url="https://example.test/low"), [bad], quality=10
    )
    assert load_page_hashes() == {page.pk: [good]}
    assert load_page_hashes(min_quality=0)[page.pk] == [good]
    assert isinstance(load_page_hashes()[page.pk][0], bytes)


def test_find_candidates(pages: dict[str, Page]) -> None:
    p1, p2, p3 = pages["p1"], pages["p2"], pages["p3"]
    assert [(c.page_a, c.page_b) for c in find_candidates()] == [
        (p1.pk, p2.pk),
        (p1.pk, p3.pk),
        (p2.pk, p3.pk),
    ]
    assert [(c.page_a, c.page_b) for c in find_candidates(cross_site_only=True)] == [
        (p1.pk, p3.pk),
        (p2.pk, p3.pk),
    ]


def test_propose_pairs_never_overwrites_decisions(pages: dict[str, Page]) -> None:
    label_pair(pages["p1"], pages["p2"], PairLabel.Label.DIFFERENT)
    assert propose_pairs(find_candidates()) == 2
    assert propose_pairs(find_candidates()) == 0
    assert PairLabel.objects.get(page_a=pages["p1"], page_b=pages["p2"]).label == "different"
    assert PairLabel.objects.filter(label=PairLabel.Label.UNSURE).count() == 2


def test_labeled_pairs_recomputes_distances(pages: dict[str, Page]) -> None:
    label_pair(pages["p1"], pages["p2"], PairLabel.Label.SAME)
    label_pair(pages["p1"], pages["p4"], PairLabel.Label.DIFFERENT)
    label_pair(pages["p2"], pages["p3"], PairLabel.Label.UNSURE)
    pairs = sorted(labeled_pairs(), key=lambda pair: not pair.same)
    assert [(pair.same, pair.min_distance, pair.score) for pair in pairs[:1]] == [(True, 3, 1.0)]
    assert pairs[1].same is False
    assert pairs[1].min_distance is not None and pairs[1].min_distance > 31
    assert len(pairs) == 2


def test_find_duplicates_command(pages: dict[str, Page]) -> None:
    out = StringIO()
    call_command("find_duplicates", "--create-labels", "--limit", "2", stdout=out)
    output = out.getvalue()
    assert "3 candidate pairs, showing 2" in output
    assert "example.test: p1" in output
    assert "created 2 UNSURE pair labels" in output
    assert PairLabel.objects.count() == 2


def test_evaluate_threshold_command(pages: dict[str, Page]) -> None:
    out = StringIO()
    call_command("evaluate_threshold", stdout=out)
    assert "No same/different pair labels yet" in out.getvalue()

    label_pair(pages["p1"], pages["p2"], PairLabel.Label.SAME)
    label_pair(pages["p1"], pages["p4"], PairLabel.Label.DIFFERENT)
    out = StringIO()
    call_command("evaluate_threshold", "--max-threshold", "10", stdout=out)
    output = out.getvalue()
    assert "2 labeled pairs (1 same, 1 different)" in output
    assert "suggested threshold: 3" in output
