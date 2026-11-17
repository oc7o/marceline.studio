"""Request metrics in `request_report` and the admin (issue #74)."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from catalog import media_shapes
from catalog import request_accounting as accounting
from catalog.models import Site, SiteHost


@pytest.fixture
def traffic(db: None) -> Site:
    site = Site.objects.create(
        domain="site.test", daily_request_budget=1000, daily_media_request_budget=200
    )
    other = Site.objects.create(domain="other.test")
    hour = accounting.hour_floor(timezone.now())
    old = hour - timedelta(days=3)
    accounting.upsert_counts(
        {
            (hour, site.pk, "site.test", "origin", "page", "page", "2xx"): [40, 4_000_000],
            (hour, site.pk, "site.test", "origin", "robots", "robots", "2xx"): [1, 100],
            (hour, site.pk, "site.test", "origin", "page", "retry", "5xx"): [2, 0],
            (hour, site.pk, "site.test", "origin", "page", "page", "refused"): [7, 0],
            (old, site.pk, "site.test", "origin", "page", "page", "2xx"): [100, 0],
            (hour, site.pk, "www.site.test", "origin", "media", "media", "2xx"): [50, 0],
            (hour, site.pk, "img.cdn.test", "cdn", "media", "media", "2xx"): [300, 0],
            (hour, other.pk, "img.cdn.test", "cdn", "media", "media", "2xx"): [200, 0],
            (hour, None, "rdap.test", "third_party", "rdap", "rdap", "2xx"): [3, 0],
        }
    )
    SiteHost.objects.create(site=site, host="img.cdn.test", role="cdn", daily_request_budget=900)
    return site


def test_report_shows_origin_budgets_cdn_hosts_types_and_outcomes(traffic: Site) -> None:
    out = StringIO()
    call_command("request_report", stdout=out)
    text = out.getvalue()

    assert "site.test\n  origin page 24h: 43/1,000 (4.0 MB); 7d: 143" in text
    assert "24h by type: page 40, retry 2, robots 1; by outcome: 2xx 41, 5xx 2, refused 7" in text
    assert "  origin media 24h: 50/200 (0.0 MB); 7d: 50" in text
    assert "img.cdn.test: 24h 300 (all sites 500/900); 7d 300" in text
    assert "rdap.test (third_party): 24h 3; 7d 3" in text


def test_report_for_one_site_and_bad_arguments(traffic: Site) -> None:
    out = StringIO()
    call_command("request_report", "--site", "other.test", "--days", "1", stdout=out)
    text = out.getvalue()
    assert "\nsite.test" not in text
    assert "rdap.test" not in text
    assert "img.cdn.test: 24h 200 (all sites 500/900); 1d 200" in text
    with pytest.raises(CommandError):
        call_command("request_report", "--site", "missing.test")
    with pytest.raises(CommandError):
        call_command("request_report", "--days", "0")


def test_site_admin_shows_origin_use_and_saves_host_roles_as_operator(
    traffic: Site, admin_client: Client
) -> None:
    listing = admin_client.get(reverse("admin:catalog_site_changelist")).content.decode()
    assert "43 (4%)" in listing
    assert "50 (25%)" in listing

    page = admin_client.get(reverse("admin:catalog_site_change", args=(traffic.pk,)))
    assert "origin page 24h: 43/1,000" in page.content.decode()

    row = SiteHost.objects.get(site=traffic)
    response = admin_client.post(
        reverse("admin:catalog_sitehost_change", args=(row.pk,)),
        {"site": traffic.pk, "host": "img.cdn.test", "role": "origin", "note": "same servers"},
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert (row.role, row.source) == ("origin", "operator")

    counts = admin_client.get(reverse("admin:catalog_requestcount_changelist"))
    assert counts.status_code == 200


def test_the_request_report_names_dead_media_url_shapes(traffic: Site) -> None:
    """ADR 0041: the waste comes out of this site's media budget, so it is reported here."""
    dead = "https://cdn.test/contents/videos_screenshots/29000/29060/preview.jpg"
    for _ in range(20):
        media_shapes.record(traffic.pk, dead, media_shapes.GONE)
    out = StringIO()

    call_command("request_report", "--site", "site.test", stdout=out)

    text = out.getvalue()
    assert "cdn.test/contents/videos_screenshots/*/*/preview.jpg" in text
    assert "29060" not in text  # a shape, never a URL


def test_the_site_admin_shows_them_too(traffic: Site, admin_client: Client) -> None:
    dead = "https://cdn.test/contents/videos_screenshots/29000/29060/preview.jpg"
    for _ in range(20):
        media_shapes.record(traffic.pk, dead, media_shapes.GONE)

    page = admin_client.get(reverse("admin:catalog_site_change", args=(traffic.pk,)))

    assert "would be suppressed" in page.content.decode()
    shapes = admin_client.get(reverse("admin:catalog_mediaurlshape_changelist"))
    assert shapes.status_code == 200
