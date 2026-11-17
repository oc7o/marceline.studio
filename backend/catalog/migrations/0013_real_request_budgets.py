"""Budgets in real requests and reviewed CDN subdomains (issue #74, ADR 0007 amendment).

`Site.daily_request_budget` counted page tasks until now; from here on it counts every request
for pages and robots.txt to the site's own hosts, retries and redirect hops included, and media
from those hosts has its own `daily_media_request_budget`. Left at the model defaults, media on
the origin would be capped at 1,000 a day. This applies the new `SitePolicy` values right away
instead of waiting for `seed_sources` — but only where a site still has the old policy value, so
an operator's own setting stays.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

# domain: (old page-task budget, page budget, origin media budget, reviewed CDN subdomains)
BUDGETS: dict[str, tuple[int, int, int, tuple[str, ...]]] = {
    "site01.example": (2500, 3000, 4500, ()),
    "site02.example": (5000, 5500, 1000, ("fastdelivery.site02.example",)),
    "site03.example": (4000, 4500, 1000, ()),
    "site04.example": (5000, 5500, 1000, ()),
    "site07.example": (2500, 3000, 3000, ()),
    "site19.example": (2500, 3000, 1000, ("imgcdn.site19.example",)),
    "site18.example": (2500, 3000, 3000, ()),
    "site22.example": (2500, 3000, 9500, ()),
    "site05.example": (2500, 3000, 1000, ("static-eu-cdn.site05.example",)),
    "site06.example": (2500, 3000, 1000, ()),
    "site11.example": (2500, 3000, 3000, ("cdn.site11.example",)),
    "site10.example": (2500, 3000, 1000, ()),
    "site09.example": (2500, 3000, 1000, ("image-cdn.site09.example",)),
    "site08.example": (2500, 3000, 6000, ()),
    "site20.example": (2500, 3000, 2500, ()),
    "site17.example": (2500, 3000, 5500, ()),
    "site21.example": (2500, 3000, 5000, ()),
}


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Site = apps.get_model("catalog", "Site")
    SiteHost = apps.get_model("catalog", "SiteHost")
    for domain, (old, page, media, cdn_hosts) in BUDGETS.items():
        site = Site.objects.filter(domain=domain).first()
        if site is None:
            continue
        if site.daily_request_budget == old:
            site.daily_request_budget = page
            site.daily_media_request_budget = media
            site.save(update_fields=("daily_request_budget", "daily_media_request_budget"))
        for host in cdn_hosts:
            SiteHost.objects.get_or_create(
                site=site, host=host, defaults={"role": "cdn", "source": "policy"}
            )


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Site = apps.get_model("catalog", "Site")
    for domain, (old, page, _, _) in BUDGETS.items():
        Site.objects.filter(domain=domain, daily_request_budget=page).update(
            daily_request_budget=old
        )


class Migration(migrations.Migration):
    dependencies = [("catalog", "0012_request_accounting")]

    operations = [migrations.RunPython(forwards, backwards)]
