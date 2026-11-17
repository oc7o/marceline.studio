"""Capturing a site's catalogue total from a page already fetched (issue #107, ADR 0049)."""

import pytest

from catalog.models import CatalogueSizeMethod, Site, SiteCatalogueSize
from crawler import catalogue_totals
from crawler.models import CrawlTask
from crawler.sites.site02 import Site02Adapter
from crawler.sites.site01 import Site01Adapter
from crawler.tests.helpers import load_fixture

pytestmark = pytest.mark.django_db


def test_a_stated_total_is_recorded_from_a_page_we_fetched_anyway() -> None:
    site = Site.objects.create(domain="site02.example")
    written = catalogue_totals.record(
        site,
        Site02Adapter(),
        CrawlTask.Kind.LISTING,
        "https://site02.example/",
        load_fixture("site02.example", "listing.html"),
    )
    record = SiteCatalogueSize.objects.get()
    assert written
    assert record.total == 66_758
    assert record.method == CatalogueSizeMethod.STATED_ON_SITE
    assert record.recorded_by == "crawler"
    # A path shape, never the URL it was read from.
    assert "https://" not in record.source_path


def test_an_adapter_that_states_nothing_writes_nothing() -> None:
    site = Site.objects.create(domain="site01.example")
    assert not catalogue_totals.record(
        site, Site01Adapter(), CrawlTask.Kind.LISTING, "https://site01.example/", "<html></html>"
    )
    assert not SiteCatalogueSize.objects.exists()


def test_an_implausibly_small_number_is_not_a_catalogue() -> None:
    """A per-tag or per-creator count must never become the denominator."""

    class Chatty:
        domain = "chatty.test"

        def catalogue_total(self, kind: CrawlTask.Kind, url: str, html: str) -> object:
            from crawler.sites.base import CatalogueTotal

            return CatalogueTotal(total=42, source_path="/tag/*")

    site = Site.objects.create(domain="chatty.test")
    assert not catalogue_totals.record(
        site,
        Chatty(),  # type: ignore[arg-type]
        CrawlTask.Kind.LISTING,
        "https://chatty.test/",
        "<html></html>",
    )
    assert not SiteCatalogueSize.objects.exists()
