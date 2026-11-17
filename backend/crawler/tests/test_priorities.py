from crawler.sites.base import (
    DEEP_DETAIL_PRIORITY,
    DETAIL_PRIORITY,
    PAGINATION_PRIORITY,
    SEED_PRIORITY,
    TOP_DETAIL_COUNT,
    TOP_DETAIL_PRIORITY,
    detail_priority,
)


def test_priorities_schedule_breadth_first() -> None:
    assert (
        SEED_PRIORITY
        > TOP_DETAIL_PRIORITY
        > DETAIL_PRIORITY
        > DEEP_DETAIL_PRIORITY
        > PAGINATION_PRIORITY
    )


def test_detail_priority_on_first_page_depends_on_position() -> None:
    assert detail_priority(0, first_page=True) == TOP_DETAIL_PRIORITY
    assert detail_priority(TOP_DETAIL_COUNT - 1, first_page=True) == TOP_DETAIL_PRIORITY
    assert detail_priority(TOP_DETAIL_COUNT, first_page=True) == DETAIL_PRIORITY


def test_detail_priority_on_later_pages_is_deep() -> None:
    assert detail_priority(0, first_page=False) == DEEP_DETAIL_PRIORITY
    assert detail_priority(40, first_page=False) == DEEP_DETAIL_PRIORITY
