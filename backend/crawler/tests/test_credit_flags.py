"""Credit flags (issue #3): compilation posts and noise credits in creator linking and backfill."""

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from catalog.models import Creator, Site
from crawler import services
from crawler.models import CrawlTask
from crawler.relink import relink_flagged_pages
from crawler.sites.base import PageData, ParseResult
from index.breadcrumbs import record_page_links
from index.models import Page, SiteCreatorBreadcrumb

COMPILATION = {"compilation": {"reasons": ["many_performers"], "performer_count": 6}}


@pytest.fixture
def site03(db: None) -> Site:
    return Site.objects.create(domain="site03.example", crawl_delay_s=0)


def _store(site: Site, title: str, performers: tuple[str, ...], extra: dict[str, Any]) -> Page:
    task, _ = CrawlTask.objects.get_or_create(
        site=site, url=f"https://{site.domain}/post/abc", kind=CrawlTask.Kind.DETAIL
    )
    data = PageData(url=task.url, title=title, performers=performers, extra=extra)
    page = services.store_parse_result(task, ParseResult(page=data))
    assert page is not None
    return page


def test_creator_links_rule_out_compilations_and_dropped_names() -> None:
    lena = Creator(pk=1, name="Lena Hartwell", aliases=["Lena"])
    mia = Creator(pk=2, name="Mira Kovacs")
    creators = [lena, mia]
    title = "Lena Rowe and Mira Kovacs"
    dropped = {"dropped_performers": [{"name": "Lena", "reason": "title_fragment"}]}

    plain = services.creator_links(title, (), (), {}, creators)
    noise = services.creator_links(title, (), (), dropped, creators)
    compilation = services.creator_links(title, (), (), COMPILATION, creators)

    assert (plain.linked, plain.ruled_out) == ([lena, mia], [])
    assert (noise.linked, noise.ruled_out) == ([mia], [lena])
    assert (compilation.linked, compilation.ruled_out) == ([], [lena, mia])
    # A dropped fragment does not hide a full name elsewhere in the text.
    assert services.creator_links("Lena Hartwell", (), (), dropped, creators).linked == [lena]


def test_compilation_page_is_indexed_without_creator_links(site03: Site) -> None:
    Creator.objects.create(name="Lena Hartwell")

    page = _store(site03, "Best of Lena Hartwell and friends", ("Lena Hartwell",), COMPILATION)

    assert page.creators.count() == 0
    assert page.raw_extraction["compilation"] == COMPILATION["compilation"]
    assert not SiteCreatorBreadcrumb.objects.exists()


def test_recrawl_that_flags_a_compilation_removes_its_links(site03: Site) -> None:
    lena = Creator.objects.create(name="Lena Hartwell")
    page = _store(site03, "Lena Hartwell scene", ("Lena Hartwell",), {})
    other = Page.objects.create(site=site03, url="https://site03.example/post/def")
    other.creators.add(lena)
    record_page_links(other, [lena.pk])
    assert SiteCreatorBreadcrumb.objects.get().page_count == 2

    _store(site03, "Lena Hartwell scene", ("Lena Hartwell",), COMPILATION)

    assert page.creators.count() == 0
    crumb = SiteCreatorBreadcrumb.objects.get()
    assert crumb.page_count == 1


def _stored_post(site: Site, slug: str, raw: dict[str, Any], *creators: Creator) -> Page:
    page = Page.objects.create(
        site=site, url=f"https://{site.domain}/post/{slug}", title=str(raw.get("raw_title", ""))
    )
    page.raw_extraction = raw
    page.save(update_fields=("raw_extraction",))
    page.creators.add(*creators)
    record_page_links(page, [creator.pk for creator in creators])
    return page


def test_relink_flagged_pages_dry_run_then_apply_is_idempotent(site03: Site) -> None:
    lena = Creator.objects.create(name="Lena Hartwell")
    names = ["Lena Hartwell", "Ann Aa", "Bea Bb", "Cy Cc", "Di Dd", "Ed Ee"]
    compilation = _stored_post(
        site03,
        "a",
        {"raw_title": " - ".join(names), "performers": names, "duration_s": 3600},
        lena,
    )
    scene = _stored_post(
        site03,
        "b",
        {"raw_title": "Lena Hartwell Kit Rowan", "performers": ["Lena Hartwell", "Kit"]},
        lena,
    )
    blocked = _stored_post(site03, "c", {"performers": ["Lena Hartwell"]})

    report = relink_flagged_pages("site03.example")

    assert (report.pages, report.without_extraction) == (3, 1)
    assert (report.compilations, report.credits_changed, report.flags_written) == (1, 1, 2)
    assert (report.links_removed, report.pages_unlinked, len(report.creators)) == (1, 1, 1)
    assert (report.pages_left_without_links, report.active_pages_left_without_links) == (1, 1)
    assert report.dropped == {"title_fragment": 1}
    compilation.refresh_from_db()
    assert "compilation" not in compilation.raw_extraction
    assert compilation.creators.count() == 1

    applied = relink_flagged_pages("site03.example", dry_run=False)

    assert applied.links_removed == 1
    compilation.refresh_from_db()
    scene.refresh_from_db()
    blocked.refresh_from_db()
    assert compilation.creators.count() == 0
    assert compilation.raw_extraction["compilation"]["reasons"] == ["many_performers"]
    assert scene.creators.count() == 1
    assert scene.raw_extraction["performers"] == ["Lena Hartwell"]
    assert scene.raw_extraction["dropped_performers"] == [
        {"name": "Kit", "reason": "title_fragment"}
    ]
    assert blocked.raw_extraction == {"performers": ["Lena Hartwell"]}
    assert SiteCreatorBreadcrumb.objects.get().page_count == 1

    again = relink_flagged_pages("site03.example", dry_run=False)
    assert (again.flags_written, again.links_removed, again.credits_changed) == (0, 0, 0)


def test_relink_flagged_pages_command(site03: Site) -> None:
    out = StringIO()

    call_command("relink_flagged_pages", "--site", "site03.example", stdout=out)

    assert "Dry run, nothing written." in out.getvalue()
    assert "creator links would be removed: 0" in out.getvalue()
    with pytest.raises(ValueError, match="no stored credit classifier"):
        relink_flagged_pages("site01.example")
