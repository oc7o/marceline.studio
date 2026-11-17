from datetime import timedelta

from django.utils import timezone

from catalog.models import Creator, Site
from index.breadcrumbs import rebuild_breadcrumbs, record_page_links, recount_breadcrumbs
from index.models import Page, SiteCreatorBreadcrumb


def _page(site: Site, slug: str, *creators: Creator) -> Page:
    page = Page.objects.create(site=site, url=f"https://{site.domain}/{slug}", title=slug)
    page.creators.add(*creators)
    return page


def test_record_page_links_counts_pages_and_moves_last_seen(db: None) -> None:
    site = Site.objects.create(domain="a.test")
    lena = Creator.objects.create(name="Lena")
    first = _page(site, "1", lena)
    earlier = timezone.now() - timedelta(days=3)

    assert record_page_links(first, [lena.pk], now=earlier) == 1
    second = _page(site, "2", lena)
    record_page_links(second, [lena.pk, lena.pk])

    crumb = SiteCreatorBreadcrumb.objects.get()
    assert crumb.page_count == 2
    assert crumb.first_seen == earlier
    assert crumb.last_seen > earlier
    assert record_page_links(second, []) == 0


def test_recount_breadcrumbs_after_unlinking_keeps_dates(db: None) -> None:
    site = Site.objects.create(domain="a.test")
    lena, mia = Creator.objects.create(name="Lena"), Creator.objects.create(name="Mia")
    page = _page(site, "1", lena, mia)
    earlier = timezone.now() - timedelta(days=3)
    record_page_links(page, [lena.pk, mia.pk], now=earlier)

    page.creators.remove(lena)
    assert recount_breadcrumbs(site.pk, [lena.pk, mia.pk, lena.pk]) == 2

    crumbs = {row.creator_id: row for row in SiteCreatorBreadcrumb.objects.all()}
    assert (crumbs[lena.pk].page_count, crumbs[mia.pk].page_count) == (0, 1)
    assert crumbs[lena.pk].last_seen == earlier
    assert recount_breadcrumbs(site.pk, []) == 0


def test_rebuild_breadcrumbs_from_the_index(db: None) -> None:
    site = Site.objects.create(domain="a.test")
    other = Site.objects.create(domain="b.test")
    lena, mia, gone = (Creator.objects.create(name=name) for name in ("Lena", "Mia", "Gone"))
    _page(site, "1", lena, mia)
    _page(site, "2", lena)
    _page(other, "3", mia)
    now = timezone.now()
    stale = SiteCreatorBreadcrumb.objects.create(
        site=other, creator=gone, page_count=7, first_seen=now, last_seen=now
    )

    assert rebuild_breadcrumbs() == 3
    assert rebuild_breadcrumbs() == 3

    counts = {
        (row.site.domain, row.creator.name): row.page_count
        for row in SiteCreatorBreadcrumb.objects.select_related("site", "creator")
    }
    assert counts == {
        ("a.test", "Lena"): 2,
        ("a.test", "Mia"): 1,
        ("b.test", "Mia"): 1,
        ("b.test", "Gone"): 0,
    }
    stale.refresh_from_db()
    assert stale.page_count == 0


def test_an_inactive_page_never_counts_towards_a_breadcrumb(db: None) -> None:
    """ADR 0029 amendment 2026-09-17: a deactivated page's credit link stays on the page (a
    reactivation may still need it), but stops contributing to the breadcrumb a human browses."""
    site = Site.objects.create(domain="a.test")
    lena = Creator.objects.create(name="Lena")
    active = _page(site, "1", lena)
    inactive = _page(site, "2", lena)
    Page.objects.filter(pk=inactive.pk).update(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED
    )

    assert record_page_links(active, [lena.pk]) == 1
    assert SiteCreatorBreadcrumb.objects.get().page_count == 1
    assert rebuild_breadcrumbs() == 1
    assert SiteCreatorBreadcrumb.objects.get().page_count == 1
    # The link itself is untouched -- only the aggregate stops counting it.
    assert lena in inactive.creators.all()
