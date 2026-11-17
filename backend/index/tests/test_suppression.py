"""Index suppression as the index sees it (docs/suppression.md): links and breadcrumbs.

`catalog` may not import `index`, so the marker's own tests live in
`catalog/tests/test_suppression.py` and these two — the database trigger on the page-creator link
table, and the breadcrumb writers — live here. Invented names.
"""

from datetime import date

import pytest
from django.utils import timezone

from catalog.models import Creator, CreatorSuppression, Site
from index.breadcrumbs import breadcrumb_rows, rebuild_breadcrumbs, record_page_links
from index.models import Page, SiteCreatorBreadcrumb

pytestmark = pytest.mark.django_db


def suppress(creator: Creator) -> CreatorSuppression:
    return CreatorSuppression.objects.create(
        creator=creator, requested_on=date(2026, 9, 17), answer_due_on=date(2026, 10, 17)
    )


@pytest.fixture
def creator(db: None) -> Creator:
    return Creator.objects.create(name="Nora Vale", aliases=["Nora V"])


def test_the_database_refuses_to_store_a_link_to_a_suppressed_name(creator: Creator) -> None:
    """The last line of defence: whatever writer tries, the association is not stored."""
    site = Site.objects.create(domain="example.test")
    page = Page.objects.create(site=site, url="https://example.test/a")
    other = Creator.objects.create(name="Dana Quill")
    suppression = suppress(creator)

    page.creators.add(creator, other)

    assert list(page.creators.all()) == [other]

    suppression.lifted_at = timezone.now()
    suppression.lift_reason = "withdrawn"
    suppression.save()
    page.creators.add(creator)

    assert set(page.creators.all()) == {creator, other}


def test_breadcrumbs_are_never_written_or_rebuilt_for_a_suppressed_name(creator: Creator) -> None:
    site = Site.objects.create(domain="example.test")
    page = Page.objects.create(site=site, url="https://example.test/a")
    other = Creator.objects.create(name="Dana Quill")
    page.creators.add(other)
    record_page_links(page, [creator.pk, other.pk])
    assert SiteCreatorBreadcrumb.objects.count() == 2

    suppress(creator)
    SiteCreatorBreadcrumb.objects.filter(creator=creator).delete()
    record_page_links(page, [creator.pk, other.pk])

    assert [row.creator_id for row in breadcrumb_rows()] == [other.pk]
    rebuild_breadcrumbs()
    assert list(SiteCreatorBreadcrumb.objects.values_list("creator_id", flat=True)) == [other.pk]
