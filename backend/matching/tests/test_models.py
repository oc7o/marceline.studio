from collections.abc import Callable

import pytest
from django.db import IntegrityError, transaction

from index.models import Page
from matching.models import PairLabel
from matching.services import label_pair


def test_self_pair_is_rejected(make_page: Callable[[str], Page]) -> None:
    page = make_page("one")

    with pytest.raises(IntegrityError), transaction.atomic():
        PairLabel.objects.create(page_a=page, page_b=page)
    with pytest.raises(ValueError):
        label_pair(page, page, PairLabel.Label.SAME)


def test_duplicate_and_reversed_pairs_are_rejected(make_page: Callable[[str], Page]) -> None:
    low, high = make_page("one"), make_page("two")
    PairLabel.objects.create(page_a=low, page_b=high)

    with pytest.raises(IntegrityError), transaction.atomic():
        PairLabel.objects.create(page_a=low, page_b=high)
    with pytest.raises(IntegrityError), transaction.atomic():
        PairLabel.objects.create(page_a=high, page_b=low)


def test_label_pair_orders_and_updates(make_page: Callable[[str], Page]) -> None:
    low, high = make_page("one"), make_page("two")

    label_pair(high, low, PairLabel.Label.UNSURE)
    pair = label_pair(low, high, PairLabel.Label.SAME, min_distance=12)

    assert PairLabel.objects.count() == 1
    assert (pair.page_a_id, pair.page_b_id) == (low.pk, high.pk)
    assert pair.label == PairLabel.Label.SAME
    assert pair.min_distance == 12
