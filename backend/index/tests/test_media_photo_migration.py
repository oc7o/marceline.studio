"""The site11 POSTER -> PHOTO reclassification of index migration 0008 (issue #69).

The migration's selection rule is imported and run against the current models: it is plain ORM
over fields the historical models share, so this pins exactly which rows `make migrate` moves.
"""

from importlib import import_module

from catalog.models import Site
from index.models import Media, Page

migration = import_module("index.migrations.0008_site11_photo_media")

PHOTO_URL = "https://n1.site11.example/content/l/e/lenah/1000/lenah_0040.jpg"
SECOND_PHOTO_URL = "https://n1.site11.example/content/l/e/lenah/1000/lenah_0039.jpg"
VIDEO_POSTER_URL = "https://site11.example/content/k/i/kirar/1000/kirar_0047.jpg"
VIDEO_CLIP_URL = "https://cdn.site11.example/content/k/i/kirar/1000/kirar_0047.mp4"


def selected() -> set[int]:
    return set(migration.photo_media(Media, Page).values_list("pk", flat=True))


def test_only_site11_photo_posts_are_reclassified(db: None) -> None:
    site11 = Site.objects.create(domain="site11.example", name="Site11")
    other = Site.objects.create(domain="example.test", name="Other")

    photo_post = Page.objects.create(
        site=site11,
        url="https://site11.example/lenah/40/",
        raw_extraction={"post_type": "photo", "post_number": 40, "profile_slug": "lenah"},
        safety_score=1.0,
    )
    photo = Media.objects.create(page=photo_post, kind=Media.Kind.POSTER, url=PHOTO_URL)

    # A blocked page keeps no `extra`, so only the full-photo host identifies it.
    blocked_post = Page.objects.create(
        site=site11,
        url="https://site11.example/lenah/39/",
        raw_extraction={},
        safety_status=Page.SafetyStatus.BLOCKED,
    )
    blocked_photo = Media.objects.create(
        page=blocked_post, kind=Media.Kind.POSTER, url=SECOND_PHOTO_URL
    )

    # A video post: its poster depicts the clip and stays POSTER, even the (hypothetical) one
    # served from the photo host. The mp4 is a PREVIEW and is never in scope.
    video_post = Page.objects.create(
        site=site11,
        url="https://site11.example/video/new/1234567/",
        raw_extraction={"post_type": "video", "video_id": "1234567"},
        safety_score=1.0,
    )
    video_poster = Media.objects.create(
        page=video_post, kind=Media.Kind.POSTER, url=VIDEO_POSTER_URL
    )
    poster_on_photo_host = Media.objects.create(
        page=video_post, kind=Media.Kind.POSTER, url=PHOTO_URL
    )
    clip = Media.objects.create(page=video_post, kind=Media.Kind.PREVIEW, url=VIDEO_CLIP_URL)

    # Another site is never touched, whatever its pages happen to record.
    foreign_page = Page.objects.create(
        site=other,
        url="https://example.test/v/1",
        raw_extraction={"post_type": "photo"},
        safety_score=1.0,
    )
    foreign = Media.objects.create(
        page=foreign_page, kind=Media.Kind.POSTER, url="https://cdn.example.test/p.jpg"
    )

    assert selected() == {photo.pk, blocked_photo.pk}

    migration.photo_media(Media, Page).update(kind=Media.Kind.PHOTO)

    for media, kind in (
        (photo, Media.Kind.PHOTO),
        (blocked_photo, Media.Kind.PHOTO),
        (video_poster, Media.Kind.POSTER),
        (poster_on_photo_host, Media.Kind.POSTER),
        (clip, Media.Kind.PREVIEW),
        (foreign, Media.Kind.POSTER),
    ):
        media.refresh_from_db()
        assert media.kind == kind
    # Idempotent: the rows it already moved are no longer posters, so a re-run selects nothing.
    assert selected() == set()
