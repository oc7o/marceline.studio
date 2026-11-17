"""Reclassify site11.example photo posts from POSTER to the new PHOTO kind (issue #69).

site11 is the first photo-first source: a profile grid is a wall of single full-resolution
images and each post page is one of them. The adapter had to store those as POSTER — the closest
kind that existed — which left a POSTER row unable to say whether it *depicts* a video or *is* the
work. `index.Media.Kind.PHOTO` makes that explicit, so the rows already indexed move with it.

Two independent signals identify such a row, and one is enough:

- its page's own `raw_extraction["post_type"] == "photo"`, which the adapter writes for every
  `/{slug}/{num}/` post it parses, and
- the media URL's host `n1.site11.example`, which serves nothing but full-size post photos
  (`site11.example` serves video posters and the `_300px` cards, `cdn.site11.example` the mp4s).

The host check also catches pages whose `raw_extraction` was cleared because they were blocked for
safety. Pages the adapter recorded as video posts are excluded by id, so a video's poster stays a
POSTER even if its thumbnail were ever served from the photo host. Only positive JSON lookups are
used: `exclude()` on a JSON key silently drops rows where the key is missing, which is exactly the
blocked-page case this migration must keep.

Nothing else changes: same decoder, same frame hashes, same eligibility and safety state. This is
a relabelling, not a re-fetch — no media is requested and no hash is touched.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

DOMAIN = "site11.example"
# The full-size photo CDN. Video posters and listing cards live on the apex host instead.
PHOTO_HOST = "https://n1.site11.example/"


def photo_media(Media, Page):
    """site11 POSTER rows that are photo posts rather than video posters.

    Kept importable so `index/tests/test_media_photo_migration.py` can pin the selection rule.
    """
    video_pages = list(
        Page.objects.filter(
            site__domain__iexact=DOMAIN, raw_extraction__post_type="video"
        ).values_list("pk", flat=True)
    )
    declared_photo = Q(page__raw_extraction__post_type="photo")
    on_photo_host = Q(url__startswith=PHOTO_HOST)
    return (
        Media.objects.filter(kind="poster", page__site__domain__iexact=DOMAIN)
        .filter(declared_photo | on_photo_host)
        .exclude(page_id__in=video_pages)
    )


def to_photo(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Media = apps.get_model("index", "Media")
    Page = apps.get_model("index", "Page")
    changed = photo_media(Media, Page).update(kind="photo")
    print(f"index.Media: reclassified {changed} site11.example poster row(s) as photo")


def to_poster(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Reverse: site11 is the only source emitting PHOTO, so every such row was a POSTER."""
    Media = apps.get_model("index", "Media")
    Media.objects.filter(kind="photo", page__site__domain__iexact=DOMAIN).update(kind="poster")


class Migration(migrations.Migration):
    dependencies = [
        ("index", "0007_media_kind_photo"),
    ]

    operations = [
        migrations.RunPython(to_photo, to_poster),
    ]
