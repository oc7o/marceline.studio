"""The catalogue-size record and the monitoring threshold (issue #107, ADR 0049)."""

from datetime import timedelta

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from catalog import catalogue_alerts
from catalog.catalogue_size import RESTAMP_AFTER, over_threshold, record_total, size_lines
from catalog.models import CatalogueSizeMethod, Site, SiteCatalogueSize

pytestmark = pytest.mark.django_db

ALERTING = dict(
    SAFETY_ALERT_TO="ops@example.invalid",
    SAFETY_ALERT_FROM="bot@example.invalid",
    CATALOGUE_SHARE_ALERT_PERCENT=25.0,
)


def site(domain: str = "example.test") -> Site:
    return Site.objects.create(domain=domain)


def counted(pages_indexed: int, pages_active: int = 0) -> SiteCatalogueSize:
    """Set the numerator the way `index.catalogue_share` does, and hand back a fresh row."""
    SiteCatalogueSize.objects.update(
        pages_indexed=pages_indexed, pages_active=pages_active, counted_at=timezone.now()
    )
    return SiteCatalogueSize.objects.select_related("site").get()


def test_a_first_figure_is_recorded_with_its_provenance() -> None:
    written = record_total(
        site(),
        66_758,
        CatalogueSizeMethod.STATED_ON_SITE,
        source_path="/* (header meta)",
        note="the site's own counter",
        recorded_by="crawler",
    )
    record = SiteCatalogueSize.objects.get()
    assert written
    assert (record.total, record.method) == (66_758, CatalogueSizeMethod.STATED_ON_SITE)
    assert (record.source_path, record.recorded_by) == ("/* (header meta)", "crawler")


def test_an_unchanged_figure_is_not_rewritten_on_every_page() -> None:
    """A site states its total on every page; without this the crawler would UPDATE per page."""
    target = site()
    record_total(target, 66_758, CatalogueSizeMethod.STATED_ON_SITE)
    assert not record_total(target, 66_758, CatalogueSizeMethod.STATED_ON_SITE)
    stale = timezone.now() - RESTAMP_AFTER - timedelta(minutes=1)
    SiteCatalogueSize.objects.update(observed_at=stale)
    assert record_total(target, 66_758, CatalogueSizeMethod.STATED_ON_SITE)


def test_a_weaker_method_never_overwrites_a_better_figure() -> None:
    """A number scraped off a listing page must not replace what a person entered."""
    target = site()
    record_total(target, 26_347, CatalogueSizeMethod.MANUAL, source_path="docs/sources/*.md")
    assert not record_total(target, 999_999, CatalogueSizeMethod.STATED_ON_SITE)
    assert not record_total(target, 999_999, CatalogueSizeMethod.LISTING_PAGES)
    assert SiteCatalogueSize.objects.get().total == 26_347
    # …and a person may always correct it.
    assert record_total(target, 27_000, CatalogueSizeMethod.MANUAL)
    assert SiteCatalogueSize.objects.get().total == 27_000


def test_a_changed_figure_of_the_same_standing_is_taken_at_once() -> None:
    target = site()
    record_total(target, 66_758, CatalogueSizeMethod.STATED_ON_SITE)
    assert record_total(target, 66_800, CatalogueSizeMethod.STATED_ON_SITE)
    assert SiteCatalogueSize.objects.get().total == 66_800


def test_a_nonsense_total_is_refused() -> None:
    assert not record_total(site(), 0, CatalogueSizeMethod.STATED_ON_SITE)
    assert not SiteCatalogueSize.objects.exists()


def test_the_share_is_both_numbers_and_unknown_stays_unknown() -> None:
    record_total(site(), 10_000, CatalogueSizeMethod.MANUAL)
    record = counted(900, 60)
    assert record.share_indexed == pytest.approx(9.0)
    assert record.share_active == pytest.approx(0.6)
    # A site with no record at all reports honestly, rather than reporting zero.
    assert "total unknown" in " ".join(size_lines(site("other.test")))


def test_size_lines_show_both_shares_and_the_provenance() -> None:
    target = site()
    record_total(target, 10_000, CatalogueSizeMethod.SITEMAP_COUNT, source_path="/sitemap.xml")
    counted(900, 60)
    text = " ".join(size_lines(target))
    assert "10,000" in text and "sitemap_count" in text and "/sitemap.xml" in text
    assert "ever indexed 9.0 %" in text and "active 0.6 %" in text


def test_the_threshold_reads_the_ever_indexed_share() -> None:
    """The share counts what was taken, and a deactivated page was taken once."""
    record_total(site(), 1_000, CatalogueSizeMethod.MANUAL)
    record = counted(300, 10)
    assert over_threshold(record, percent=25.0)
    assert not over_threshold(record, percent=50.0)
    # Zero switches the alert off, whatever the share is.
    assert not over_threshold(record, percent=0.0)


def test_the_operator_is_mailed_once_per_crossing() -> None:
    record_total(site(), 1_000, CatalogueSizeMethod.MANUAL)
    with override_settings(**ALERTING):
        assert catalogue_alerts.notify_over_threshold([counted(300, 10)]) == 1
        assert len(mail.outbox) == 1
        assert "example.test" in mail.outbox[0].body
        # Still over: no second mail.
        assert catalogue_alerts.notify_over_threshold([counted(300, 10)]) == 0
        assert len(mail.outbox) == 1
        # Back under: the stamp is cleared, so a later crossing mails again.
        assert catalogue_alerts.notify_over_threshold([counted(10, 10)]) == 0
        assert SiteCatalogueSize.objects.get().notified_at is None
        assert catalogue_alerts.notify_over_threshold([counted(400, 10)]) == 1
        assert len(mail.outbox) == 2


def test_nothing_is_sent_without_an_address_pair() -> None:
    """The alert is off until the operator supplies a mailbox; the log line still happens."""
    record_total(site(), 1_000, CatalogueSizeMethod.MANUAL)
    with override_settings(CATALOGUE_SHARE_ALERT_PERCENT=25.0):
        assert catalogue_alerts.notify_over_threshold([counted(900, 10)]) == 1
    assert mail.outbox == []
