"""A re-crawl never re-creates a suppressed association (docs/suppression.md).

The hard requirement of the feature: once a name is suppressed, crawling the same page again —
for another performer it credits, for a recheck, for anything — stores no creator link and no
breadcrumb for the suppressed person, while everything about the other performers is stored as
usual. Invented names only.
"""

from datetime import date

import pytest
from django.utils import timezone

from catalog.models import Creator, CreatorSuppression, Site
from crawler import services
from crawler.models import CrawlTask
from crawler.sites.base import PageData, ParseResult
from index.models import Page, SiteCreatorBreadcrumb

pytestmark = pytest.mark.django_db

URL = "https://example.test/video/two-performers"


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(domain="example.test", crawl_delay_s=0)


def crawl(site: Site) -> Page:
    """Store the same detail page again, crediting both performers, as a re-crawl would."""
    task, _ = CrawlTask.objects.get_or_create(site=site, url=URL, kind=CrawlTask.Kind.DETAIL)
    data = PageData(
        url=URL,
        title="Nora Vale and Dana Quill scene",
        tags=("nora vale", "hd"),
        performers=("Nora Vale", "Dana Quill"),
        extra={"profile_slug": "nora-vale"},
    )
    page = services.store_parse_result(task, ParseResult(page=data))
    assert page is not None
    return page


def test_a_recrawl_stores_no_link_and_no_breadcrumb_for_a_suppressed_name(site: Site) -> None:
    nora = Creator.objects.create(name="Nora Vale")
    dana = Creator.objects.create(name="Dana Quill")
    first = crawl(site)
    assert set(first.creators.all()) == {nora, dana}

    CreatorSuppression.objects.create(
        creator=nora, requested_on=date(2026, 9, 17), answer_due_on=date(2026, 10, 17)
    )
    Page.creators.through.objects.filter(creator=nora).delete()
    SiteCreatorBreadcrumb.objects.filter(creator=nora).delete()
    services.clear_reference_caches()

    page = crawl(site)

    assert list(page.creators.all()) == [dana]
    assert list(SiteCreatorBreadcrumb.objects.values_list("creator_id", flat=True)) == [dana.pk]
    # The page itself is stored as always — it is not this person's page alone.
    assert page.url == URL and page.title == "Nora Vale and Dana Quill scene"


def test_the_link_is_dropped_even_when_the_cached_creator_list_is_stale(site: Site) -> None:
    """The per-process creator cache can be up to two minutes old; the database still refuses."""
    nora = Creator.objects.create(name="Nora Vale")
    stale = list(Creator.objects.all())  # what a crawler that started earlier holds

    CreatorSuppression.objects.create(
        creator=nora, requested_on=date(2026, 9, 17), answer_due_on=date(2026, 10, 17)
    )
    task, _ = CrawlTask.objects.get_or_create(site=site, url=URL, kind=CrawlTask.Kind.DETAIL)
    page = services.store_parse_result(
        task,
        ParseResult(page=PageData(url=URL, title="Nora Vale scene", performers=("Nora Vale",))),
        creators=stale,
    )

    assert page is not None
    assert page.creators.count() == 0
    assert not SiteCreatorBreadcrumb.objects.exists()


def test_a_lifted_suppression_lets_the_next_crawl_link_again(site: Site) -> None:
    nora = Creator.objects.create(name="Nora Vale")
    suppression = CreatorSuppression.objects.create(
        creator=nora, requested_on=date(2026, 9, 17), answer_due_on=date(2026, 10, 17)
    )
    assert crawl(site).creators.count() == 0

    suppression.lifted_at = timezone.now()
    suppression.lift_reason = "withdrawn by the person"
    suppression.save()
    services.clear_reference_caches()

    assert list(crawl(site).creators.all()) == [nora]
    assert list(SiteCreatorBreadcrumb.objects.values_list("creator_id", flat=True)) == [nora.pk]
