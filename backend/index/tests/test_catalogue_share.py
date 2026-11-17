"""Our share of each site's catalogue: the numerator, and what the report says (issue #107)."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from catalog.catalogue_size import record_total
from catalog.models import CatalogueSizeMethod, Site, SiteCatalogueSize
from index import catalogue_share
from index.models import Page

pytestmark = pytest.mark.django_db


def pages(site: Site, *, active: int, inactive: int) -> None:
    for index in range(active + inactive):
        Page.objects.create(
            site=site,
            url=f"https://{site.domain}/{index}",
            title=f"page {index}",
            active=index < active,
            inactive_reason="" if index < active else Page.InactiveReason.UNTARGETED,
        )


def test_both_shares_are_measured_and_stamped_into_the_record() -> None:
    site = Site.objects.create(domain="known.test")
    pages(site, active=2, inactive=8)
    record_total(site, 1_000, CatalogueSizeMethod.SITEMAP_COUNT, source_path="/sitemap.xml")

    records = catalogue_share.refresh_counts()
    record = SiteCatalogueSize.objects.get()
    assert [r.pk for r in records] == [record.pk]
    assert (record.pages_indexed, record.pages_active) == (10, 2)
    assert record.counted_at is not None
    assert record.share_indexed == pytest.approx(1.0)
    assert record.share_active == pytest.approx(0.2)


def test_a_site_without_a_total_is_counted_but_never_given_a_share() -> None:
    """Unknown is reported as unknown: 16 of 18 sites were in this state on 2026-09-18."""
    site = Site.objects.create(domain="unknown.test")
    pages(site, active=3, inactive=1)

    share = next(row for row in catalogue_share.measure() if row.site_id == site.pk)
    assert (share.pages_indexed, share.pages_active) == (4, 3)
    assert share.total is None
    assert share.share_indexed is None and share.share_active is None
    assert "total unknown" in catalogue_share.render_text([share])


def test_measure_reports_a_disabled_site_too() -> None:
    site = Site.objects.create(domain="off.test", enabled=False)
    pages(site, active=0, inactive=2)
    share = next(row for row in catalogue_share.measure(domain="off.test") if row.domain)
    assert share.enabled is False
    assert "(disabled)" in catalogue_share.render_text([share])


def test_evaluate_alerts_but_changes_no_crawl() -> None:
    site = Site.objects.create(domain="big.test")
    pages(site, active=10, inactive=90)
    record_total(site, 200, CatalogueSizeMethod.MANUAL)

    with override_settings(CATALOGUE_SHARE_ALERT_PERCENT=25.0):
        records, alerted = catalogue_share.evaluate()
    site.refresh_from_db()
    assert alerted == 1
    assert records[0].share_indexed == pytest.approx(50.0)
    # Alerting only: nothing about the site changed.
    assert (site.enabled, site.paused_until) == (True, None)
    assert site.daily_request_budget == Site._meta.get_field("daily_request_budget").default


def test_the_command_records_a_total_by_hand_and_reports_the_share() -> None:
    site = Site.objects.create(domain="byhand.test")
    pages(site, active=1, inactive=9)

    out = StringIO()
    call_command(
        "catalogue_size",
        "--site",
        "byhand.test",
        "--record",
        "1000",
        "--method",
        "sitemap_count",
        "--source",
        "/sitemap.xml",
        "--refresh",
        stdout=out,
    )
    text = out.getvalue()
    assert "recorded 1,000 for byhand.test" in text
    assert "ever indexed 10 = 1.0 %" in text
    assert "active 1 = 0.1 %" in text
    assert SiteCatalogueSize.objects.get().pages_indexed == 10
