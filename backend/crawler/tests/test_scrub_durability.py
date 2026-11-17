"""A re-crawl never undoes the scrub a deactivation performed (issue #148, ADR 0029 amendment).

`store_parse_result` wrote `title`, `tags` and `raw_extraction["performers"]` unconditionally, so
every deactivated page that a listing, a search or the media-link recovery re-crawled got the names
the deactivation had just deleted written straight back. What must hold instead: while the page is
deactivated as untargeted the three fields stay empty, everything else about the page keeps being
stored (so it can still be reactivated), and the moment it is active again the next parse fills it
normally. Invented titles and names only.
"""

import pytest
from django.utils import timezone

from catalog.models import Creator, Site
from crawler import services
from crawler.models import CrawlTask
from crawler.sites.base import PageData, ParseResult
from index.models import Page
from index.retention import scrub_deactivated_pages

pytestmark = pytest.mark.django_db

URL = "https://example.test/video/untargeted"
UNTARGETED = Page.InactiveReason.UNTARGETED


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(domain="example.test", crawl_delay_s=0)


def crawl(site: Site, *, duration_s: int = 42) -> Page:
    """Store the same detail page as a re-crawl would, with its title, tags and credits."""
    task, _ = CrawlTask.objects.get_or_create(site=site, url=URL, kind=CrawlTask.Kind.DETAIL)
    data = PageData(
        url=URL,
        title="Nora Vale scene",
        tags=("nora vale", "hd"),
        performers=("Nora Vale",),
        duration_s=duration_s,
        extra={"external_links": ["https://mirror.test/x"]},
    )
    page = services.store_parse_result(task, ParseResult(page=data))
    assert page is not None
    return page


def deactivate(page: Page) -> None:
    """What `marceline.targets.apply_activity` does to an untargeted page."""
    now = timezone.now()
    Page.objects.filter(pk=page.pk).update(
        active=False, inactive_reason=UNTARGETED, active_changed_at=now, deactivated_at=now
    )
    scrub_deactivated_pages([page.pk])


def test_a_recrawl_of_a_deactivated_page_leaves_it_scrubbed(site: Site) -> None:
    Creator.objects.create(name="Nora Vale")
    deactivate(crawl(site))

    page = crawl(site, duration_s=99)

    assert (page.title, page.tags) == ("", [])
    assert "performers" not in page.raw_extraction
    assert "dropped_performers" not in page.raw_extraction
    # What is not about people is kept current, exactly as the scrub leaves it in place.
    assert page.raw_extraction == {
        "duration_s": 99,
        "external_links": ["https://mirror.test/x"],
    }
    # And the page stays deactivated with its clock running: this writes no activity fields.
    assert (page.active, page.inactive_reason) == (False, UNTARGETED)
    assert page.deactivated_at is not None


def test_a_recrawl_still_records_everything_a_reactivation_needs(site: Site) -> None:
    creator = Creator.objects.create(name="Nora Vale")
    deactivate(crawl(site))
    Page.objects.filter(url=URL).update(availability=Page.Availability.UNKNOWN)

    page = crawl(site)

    # The `linked` rule reads this link to notice the page again once the creator is a target.
    assert list(page.creators.all()) == [creator]
    assert page.availability == Page.Availability.PLAYABLE
    assert page.availability_checked_at is not None
    assert page.safety_scored_at is not None


def test_a_reactivated_page_is_filled_normally_again(site: Site) -> None:
    Creator.objects.create(name="Nora Vale")
    deactivate(crawl(site))
    # Retargeted: `apply_activity` reactivates it and clears the clock.
    Page.objects.filter(url=URL).update(active=True, inactive_reason="", deactivated_at=None)

    page = crawl(site)

    assert page.title == "Nora Vale scene"
    assert page.tags == ["nora vale", "hd"]
    assert page.raw_extraction["performers"] == ["Nora Vale"]


def test_a_page_deactivated_for_another_reason_is_not_treated_as_scrubbed(site: Site) -> None:
    """Only the targeted-indexing reason means "scrubbed". A future `inactive_reason` has to say
    what it wants here deliberately, instead of silently inheriting this rule."""
    deactivate(crawl(site))
    # A hypothetical other reason, written the way a new rule would.
    Page.objects.filter(url=URL).update(inactive_reason="some_other_reason")

    page = crawl(site)

    assert page.title == "Nora Vale scene"


def test_a_new_page_is_stored_with_its_text(site: Site) -> None:
    page = crawl(site)

    assert page.title == "Nora Vale scene"
    assert page.raw_extraction["performers"] == ["Nora Vale"]
