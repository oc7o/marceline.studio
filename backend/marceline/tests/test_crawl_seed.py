"""`crawl_seed` seeds target creators by default (issue #71), others only on request."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog.models import Creator, Site, WatchlistEntry
from crawler.listing_walk import walk_of
from crawler.models import CrawlTask
from crawler.sites.base import PROSPECT_SEED_PRIORITY, SEED_PRIORITY, WATCHLIST_SEED_PRIORITY
from marceline.targets import co_performer_priority
from marceline.tests.app_helpers import add_member, make_organisation, own
from matching.models import Reference

pytestmark = pytest.mark.django_db


def owned_original(creator: Creator, *co_performers: Creator) -> Reference:
    organisation = make_organisation(f"Org of {creator.name}")
    owner = add_member(organisation, f"owner-{creator.pk}")
    reference = Reference.objects.create(
        creator=creator, title="o", sha256=f"{creator.pk:064d}", frame_fps=1, uploaded_by=owner
    )
    own(reference, organisation)
    reference.creators.add(creator, *co_performers)
    return reference


def seeds() -> set[tuple[str, int]]:
    return set(CrawlTask.objects.values_list("url", "priority"))


def test_crawl_seed_seeds_rights_holders_then_co_performers_never_noted_watchlist() -> None:
    Site.objects.create(domain="site01.example")
    Site.objects.create(domain="site15.example")
    Site.objects.create(domain="site07.example", enabled=False)
    owned_original(
        Creator.objects.create(name="Lena Hartwell"), Creator.objects.create(name="Jonas Stein")
    )
    WatchlistEntry.objects.create(creator=Creator.objects.create(name="Mira Kovacs"))
    WatchlistEntry.objects.create(creator=Creator.objects.create(name="Iris Holland"), crawl=True)
    Creator.objects.create(name="Nina Ashton")  # neither: never seeded
    out = StringIO()

    call_command("crawl_seed", stdout=out)

    assert seeds() == {
        ("https://site01.example/search/lena-hartwell/", SEED_PRIORITY),
        ("https://site01.example/search/jonas-stein/", co_performer_priority(1, 1)),
        ("https://site01.example/search/iris-holland/", WATCHLIST_SEED_PRIORITY),
    }
    assert (
        "site01.example: 3 new task(s) for 3 creator(s) "
        "(1 rights holder(s), 1 co-performer(s), 1 crawled watchlist, 0 prospect(s))"
    ) in out.getvalue()
    assert "site15.example: no adapter yet" in out.getvalue()


def test_crawl_seed_without_targets_seeds_nothing_and_no_broad_listing() -> None:
    Site.objects.create(domain="site09.example")
    Creator.objects.create(name="Lena Hartwell")
    out = StringIO()

    call_command("crawl_seed", refresh=True, stdout=out)

    assert not CrawlTask.objects.exists()
    assert "site09.example: 0 new task(s) for 0 creator(s) (0 rights holder(s)" in out.getvalue()

    call_command("crawl_seed", listings=True, stdout=out)
    assert seeds() == {("https://site09.example/videos", SEED_PRIORITY)}
    assert "+ broad listings" in out.getvalue()


def test_crawl_seed_explicit_creator_overrides_targets() -> None:
    Site.objects.create(domain="site01.example")
    Creator.objects.create(name="Nina Ashton")
    WatchlistEntry.objects.create(creator=Creator.objects.create(name="Mira Kovacs"), crawl=True)

    call_command("crawl_seed", creators=["Nina Ashton"], stdout=StringIO())

    assert seeds() == {("https://site01.example/search/nina-ashton/", SEED_PRIORITY)}


def test_crawl_seed_max_creators_keeps_the_highest_priority_first() -> None:
    Site.objects.create(domain="site01.example")
    WatchlistEntry.objects.create(creator=Creator.objects.create(name="Alpha Watched"), crawl=True)
    owned_original(Creator.objects.create(name="Zulu Owned"))

    call_command("crawl_seed", max_creators=1, stdout=StringIO())

    assert seeds() == {("https://site01.example/search/zulu-owned/", SEED_PRIORITY)}


def test_crawl_seed_query_enqueues_search_seeds_instead_of_creators() -> None:
    Site.objects.create(domain="site01.example")
    Site.objects.create(domain="site03.example")
    Site.objects.create(domain="site02.example", enabled=False)
    owned_original(Creator.objects.create(name="Lena Hartwell"))
    out = StringIO()

    call_command("crawl_seed", query=["Kira Rowan Summer Road Trip", "Tom & Jerry's"], stdout=out)

    assert set(CrawlTask.objects.values_list("url", "kind", "priority")) == {
        (url, CrawlTask.Kind.SEARCH, SEED_PRIORITY)
        for url in (
            "https://site01.example/search/kira-rowan-summer-road-trip/",
            "https://site01.example/search/tom-jerry-s/",
            "https://site03.example/Kira-Rowan-Summer-Road-Trip.html",
            "https://site03.example/Tom-Jerry-s.html",
        )
    }
    assert "site01.example: 2 new task(s) for 2 query(ies)" in out.getvalue()


def test_crawl_seed_query_respects_site() -> None:
    Site.objects.create(domain="site01.example")
    Site.objects.create(domain="site02.example")

    call_command("crawl_seed", query=["Kira Rowan Summer Road Trip"], site="site02.example")

    assert list(CrawlTask.objects.values_list("url", flat=True)) == [
        "https://site02.example/?q=kira+rowan+summer+road+trip"
    ]


def test_crawl_seed_refresh_preserves_dead_detail_tombstone() -> None:
    site = Site.objects.create(domain="site01.example")
    owned_original(Creator.objects.create(name="Lena Hartwell"))
    task = CrawlTask.objects.create(
        site=site,
        url="https://site01.example/search/lena-hartwell/",
        kind=CrawlTask.Kind.DETAIL,
        status=CrawlTask.Status.SKIPPED,
        decision_reason="HTTP 404",
        fetched_at=timezone.now(),
        attempts=1,
    )
    # Refresh must respect the recorded DETAIL outcome even if rediscovered as a seed.
    call_command("crawl_seed", refresh=True, stdout=StringIO())
    task.refresh_from_db()
    assert task.status == CrawlTask.Status.SKIPPED
    assert task.attempts == 1
    assert CrawlTask.objects.count() == 1


def prospect(name: str, *aliases: str, crawl: bool = True) -> Creator:
    creator = Creator.objects.create(name=name, aliases=list(aliases))
    WatchlistEntry.objects.create(
        creator=creator, crawl=crawl, purpose=WatchlistEntry.Purpose.PROSPECT
    )
    return creator


def test_prospects_seed_last_below_co_performers() -> None:
    Site.objects.create(domain="site01.example")
    owned_original(
        Creator.objects.create(name="Lena Hartwell"), Creator.objects.create(name="Jonas Stein")
    )
    WatchlistEntry.objects.create(creator=Creator.objects.create(name="Iris Holland"), crawl=True)
    prospect("Anna Studio")
    prospect("Berta Noted", crawl=False)  # a prospect not crawled yet: never seeded
    out = StringIO()

    call_command("crawl_seed", stdout=out)

    assert seeds() == {
        ("https://site01.example/search/lena-hartwell/", SEED_PRIORITY),
        ("https://site01.example/search/jonas-stein/", co_performer_priority(1, 1)),
        ("https://site01.example/search/iris-holland/", WATCHLIST_SEED_PRIORITY),
        ("https://site01.example/search/anna-studio/", PROSPECT_SEED_PRIORITY),
    }
    assert PROSPECT_SEED_PRIORITY <= WATCHLIST_SEED_PRIORITY < co_performer_priority(1, 1)
    assert "1 crawled watchlist, 1 prospect(s))" in out.getvalue()


def test_prospect_seeds_are_capped_per_day_across_sites(settings: Settings) -> None:
    settings.PROSPECT_DAILY_SEED_CAP = 3
    Site.objects.create(domain="site01.example")
    Site.objects.create(domain="site02.example")
    # Two names -> site01: 2 searches; site02: 2 creator pages + 2 searches = 6 seeds.
    prospect("Anna Studio", "Anna Films")
    owned_original(Creator.objects.create(name="Lena Hartwell"))

    # The scheduler calls crawl_seed one site at a time; the cap holds across the calls.
    for domain in ("site02.example", "site01.example"):
        call_command("crawl_seed", site=domain, refresh=True, stdout=StringIO())

    anna = CrawlTask.objects.filter(priority=PROSPECT_SEED_PRIORITY)
    assert anna.count() == 3
    first = set(anna.values_list("url", flat=True))
    # Paying targets are never capped.
    assert CrawlTask.objects.filter(priority=SEED_PRIORITY).count() == 3

    # The same day: the cap is used up by pending seeds, nothing more is queued.
    for domain in ("site02.example", "site01.example"):
        call_command("crawl_seed", site=domain, refresh=True, stdout=StringIO())
    assert anna.count() == 3

    # A day later the fetched seeds no longer count, and never-fetched seeds go first.
    anna.update(status=CrawlTask.Status.DONE, fetched_at=timezone.now() - timedelta(hours=25))
    for domain in ("site02.example", "site01.example"):
        call_command("crawl_seed", site=domain, refresh=True, stdout=StringIO())
    pending = set(
        CrawlTask.objects.filter(
            priority=PROSPECT_SEED_PRIORITY, status=CrawlTask.Status.PENDING
        ).values_list("url", flat=True)
    )
    assert len(pending) == 3
    assert len(pending - first) == 3  # the three seeds that never ran


def test_crawl_seed_listings_walks_a_catalogue_whose_cards_carry_titles() -> None:
    """site13.example declares `card_text`, so its catalogue becomes a gated breadth-first
    walk (ADR 0029 amendment) instead of a plain pagination chain."""
    Site.objects.create(domain="site13.example")
    out = StringIO()

    call_command("crawl_seed", listings=True, stdout=out)

    root = CrawlTask.objects.get(url="https://site13.example/videos")
    assert walk_of(root.context) == root.url
    assert "listing walk(s): started https://site13.example/videos" in out.getvalue()

    call_command("crawl_seed", listings=True, stdout=out)

    assert "listing walk(s): walking at https://site13.example/videos" in out.getvalue()
