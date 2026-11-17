"""Breadth-first listing walks with a title gate (`crawler/listing_walk.py`, ADR 0029 amendment).

The pure half is the gate and what one walked listing page contributes; the database half is how a
walk is started, handed down, resumed and reported.
"""

import pytest
from django.utils import timezone

from catalog.models import Creator, Site
from crawler import services
from crawler.listing_walk import WALK_KEY, TitleGate, walk_context, walk_links, walk_of
from crawler.models import CrawlTask
from crawler.sites.base import MediaSpec, ParseResult, TaskSpec
from index.models import Media, Page

DETAIL = CrawlTask.Kind.DETAIL
LISTING = CrawlTask.Kind.LISTING
CATALOGUE = "https://site01.example/videos"


def gate_for(*names: str) -> TitleGate:
    return TitleGate.for_creators([Creator(name=name) for name in names])


def card(path: str, text: str) -> TaskSpec:
    return TaskSpec(f"https://site01.example{path}", DETAIL, 10, text=text)


def page_ahead(number: int) -> TaskSpec:
    return TaskSpec(f"{CATALOGUE}?page={number}", LISTING, 0)


def test_gate_admits_a_name_written_as_whole_words() -> None:
    gate = gate_for("Sasha Meyer")

    assert gate.admits("SomeUploader - clip with Sasha Meyer and friends")
    assert gate.admits("SASHA MEYER, uncut")
    assert not gate.admits("SomeUploader - Katja im Garten")


def test_gate_admits_a_handle_spelling_inside_a_card_title() -> None:
    """Cards read `Handle - Title`, so the name may be one word of a longer string
    (`names_person`'s separator-less case, applied per word)."""
    gate = gate_for("Sasha Meyer")

    assert gate.admits("SashaMeyer - Hotel")
    assert gate.admits("sasha-meyer - Hotel")
    assert gate.admits("sasha_meyer - Hotel")


def test_gate_folds_case_and_accents_like_the_rest_of_the_crawler() -> None:
    gate = gate_for("Eléna Märsh")

    assert gate.admits("Some Uploader - with elena marsh")
    assert gate.admits("ElenaMarsh - solo")


def test_gate_never_admits_a_substring_of_a_longer_name() -> None:
    gate = gate_for("Ann")

    assert not gate.admits("AnnabelMuster - Neues Video")
    assert gate.admits("Uploader - Ann is here")


def test_gate_admits_a_card_with_no_text_and_admits_nothing_without_creators() -> None:
    """Err towards fetching where the adapter gave us nothing to judge; with nobody to hunt for
    there is nothing to fetch either."""
    assert gate_for("Sasha Meyer").admits("   ")
    assert not gate_for().admits("SomeUploader - clip")


def test_a_walked_page_contributes_gated_cards_and_the_next_page_only() -> None:
    """The paginator lists the far end too (`…`, 3239, 3240); a walk takes one step at a time."""
    links = [
        card("/video/1/a", "SomeUploader - Katja"),
        card("/video/2/b", "SashaMeyer - Hotel"),
        card("/video/3/c", "OtherUploader - egal"),
        page_ahead(2),
        page_ahead(3),
        page_ahead(3240),
    ]

    plan = walk_links(links, gate_for("Sasha Meyer"))

    assert [spec.url for spec in plan.links] == [
        "https://site01.example/video/2/b",
        f"{CATALOGUE}?page=2",
    ]
    assert (plan.cards, plan.admitted, plan.ahead) == (3, 1, 1)


def test_walk_context_round_trips_and_ignores_anything_else() -> None:
    assert walk_of(walk_context(CATALOGUE)) == CATALOGUE
    assert walk_of({"poster": "x"}) is None
    assert walk_of(None) is None


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


def listing_result(*links: TaskSpec) -> ParseResult:
    return ParseResult(links=tuple(links))


def walk_task(site: Site, url: str = CATALOGUE, **fields: object) -> CrawlTask:
    return CrawlTask.objects.create(
        site=site, url=url, kind=LISTING, context=walk_context(CATALOGUE), **fields
    )


def test_a_walked_listing_page_enqueues_only_the_cards_the_gate_admits(site01: Site) -> None:
    Creator.objects.create(name="Sasha Meyer")
    task = walk_task(site01)

    services.store_parse_result(
        task,
        listing_result(
            card("/video/1/a", "SomeUploader - Katja"),
            card("/video/2/b", "SashaMeyer - Hotel"),
            page_ahead(2),
            page_ahead(3240),
        ),
    )

    assert set(CrawlTask.objects.exclude(pk=task.pk).values_list("url", flat=True)) == {
        "https://site01.example/video/2/b",
        f"{CATALOGUE}?page=2",
    }


def test_a_walk_hands_itself_down_to_the_next_page_only(site01: Site) -> None:
    """The successor is part of the same walk, so the gate stays on for the whole chain; the
    detail task it enqueues carries the provenance but is a leaf."""
    Creator.objects.create(name="Sasha Meyer")
    task = walk_task(site01)

    services.store_parse_result(
        task, listing_result(card("/video/2/b", "SashaMeyer - Hotel"), page_ahead(2))
    )

    successor = CrawlTask.objects.get(url=f"{CATALOGUE}?page=2")
    assert walk_of(successor.context) == CATALOGUE
    assert successor.discovered_from_id == task.pk


def test_an_ordinary_listing_task_is_never_gated(site01: Site) -> None:
    """Only a walk is gated: a creator's own catalogue and every chain of an adapter without the
    `card_text` capability enqueue every card and every page ahead, as they always did."""
    Creator.objects.create(name="Sasha Meyer")
    task = CrawlTask.objects.create(site=site01, url=CATALOGUE, kind=LISTING)

    services.store_parse_result(
        task,
        listing_result(
            card("/video/1/a", "SomeUploader - Katja"),
            card("/video/2/b", "SashaMeyer - Hotel"),
            page_ahead(2),
            page_ahead(3240),
        ),
    )

    assert CrawlTask.objects.exclude(pk=task.pk).count() == 4
    assert not CrawlTask.objects.filter(context__has_key=WALK_KEY).exists()


def test_a_dropped_card_still_refreshes_the_media_link_of_an_indexed_page(site01: Site) -> None:
    """Dropping a card only means not fetching it. A page we already have keeps getting fresh
    links for its media off the card (no request; the gate of ADR 0018 still decides the rest)."""
    Creator.objects.create(name="Sasha Meyer")
    page = Page.objects.create(site=site01, url="https://site01.example/video/1/a", title="old")
    dropped = TaskSpec(
        page.url,
        DETAIL,
        10,
        (MediaSpec(Media.Kind.POSTER, "https://site01.example/1.jpg"),),
        text="SomeUploader - Katja",
    )

    services.store_parse_result(walk_task(site01), listing_result(dropped))

    assert not CrawlTask.objects.filter(url=page.url).exists()
    assert Media.objects.filter(page=page).count() == 1


def test_enqueue_listing_walks_starts_a_walk_then_leaves_it_alone(site01: Site) -> None:
    specs = [TaskSpec(CATALOGUE, LISTING, 30)]

    started = services.enqueue_listing_walks(site01, specs)

    root = CrawlTask.objects.get(url=CATALOGUE)
    assert (started.created, started.started) == (1, (CATALOGUE,))
    assert walk_of(root.context) == CATALOGUE

    again = services.enqueue_listing_walks(site01, specs)

    assert (again.created, again.walking, again.resumed) == (0, (CATALOGUE,), ())


def test_a_dead_frontier_is_resumed_where_it_stopped_not_at_page_one(site01: Site) -> None:
    """`deactivate_untargeted --stop-broad-listings` skips the pending frontier; the next
    deliberate `crawl_seed --listings` puts that page back, deep in the catalogue."""
    walk_task(site01, status=CrawlTask.Status.DONE, fetched_at=timezone.now())
    frontier = walk_task(
        site01,
        f"{CATALOGUE}?page=57",
        status=CrawlTask.Status.SKIPPED,
        decision_reason="broad listing stopped",
        attempts=3,
    )

    status = services.enqueue_listing_walks(site01, [TaskSpec(CATALOGUE, LISTING, 30)])

    frontier.refresh_from_db()
    assert status.resumed == (frontier.url,)
    assert frontier.status == CrawlTask.Status.PENDING
    assert (frontier.attempts, frontier.decision_reason) == (0, "")


def test_a_walk_that_reached_the_end_is_reported_not_refetched(site01: Site) -> None:
    walk_task(site01, status=CrawlTask.Status.DONE, fetched_at=timezone.now())
    last = walk_task(site01, f"{CATALOGUE}?page=3240", status=CrawlTask.Status.DONE)

    status = services.enqueue_listing_walks(site01, [TaskSpec(CATALOGUE, LISTING, 30)])

    last.refresh_from_db()
    assert status.complete == (last.url,)
    assert last.status == CrawlTask.Status.DONE


def test_an_older_ungated_chain_root_is_adopted_and_walked_again(site01: Site) -> None:
    """A `--listings` chain queued before the walk existed: its start URL becomes the walk's root
    and is re-queued, so the catalogue is walked again — this time behind the gate."""
    root = CrawlTask.objects.create(
        site=site01, url=CATALOGUE, kind=LISTING, status=CrawlTask.Status.DONE
    )

    status = services.enqueue_listing_walks(site01, [TaskSpec(CATALOGUE, LISTING, 30)])

    root.refresh_from_db()
    assert status.started == (CATALOGUE,)
    assert (root.status, walk_of(root.context)) == (CrawlTask.Status.PENDING, CATALOGUE)


def test_a_walk_takes_over_the_next_page_an_older_chain_already_fetched(site01: Site) -> None:
    """Adoption has to reach past the root: page 2 already has a task, so nothing would be
    created for it and the walk would end after one page."""
    Creator.objects.create(name="Sasha Meyer")
    stale = CrawlTask.objects.create(
        site=site01, url=f"{CATALOGUE}?page=2", kind=LISTING, status=CrawlTask.Status.DONE
    )

    services.store_parse_result(walk_task(site01), listing_result(page_ahead(2)))

    stale.refresh_from_db()
    assert walk_of(stale.context) == CATALOGUE
    assert stale.status == CrawlTask.Status.PENDING


def test_a_detail_page_found_by_a_walk_is_not_itself_gated(site01: Site) -> None:
    """The gate reads catalogue cards. A detail task keeps the walk only as provenance; what its
    own page links (related videos) is judged as it always was."""
    Creator.objects.create(name="Sasha Meyer")
    detail = CrawlTask.objects.create(
        site=site01,
        url="https://site01.example/video/2/b",
        kind=DETAIL,
        context=walk_context(CATALOGUE),
    )

    services.store_parse_result(
        detail, listing_result(card("/video/9/related", "OtherUploader - egal"))
    )

    assert CrawlTask.objects.filter(url="https://site01.example/video/9/related").exists()
