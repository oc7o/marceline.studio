from collections.abc import Callable

import pytest
from django.db import IntegrityError, transaction

from index.models import FrameHash, Media, Page


def test_media_url_is_unique_per_page(make_page: Callable[[str], Page]) -> None:
    first, second = make_page("one"), make_page("two")
    Media.objects.create(page=first, kind=Media.Kind.POSTER, url="https://cdn.test/a.jpg")
    Media.objects.create(page=second, kind=Media.Kind.POSTER, url="https://cdn.test/a.jpg")

    with pytest.raises(IntegrityError), transaction.atomic():
        Media.objects.create(page=first, kind=Media.Kind.SPRITE, url="https://cdn.test/a.jpg")


def test_frame_hash_round_trips_32_bytes(make_page: Callable[[str], Page]) -> None:
    media = Media.objects.create(
        page=make_page("one"), kind=Media.Kind.POSTER, url="https://cdn.test/a.jpg"
    )
    value = bytes(range(32))

    frame = FrameHash.objects.create(media=media, pdq=value, quality=90)
    frame.refresh_from_db()

    assert bytes(frame.pdq) == value


def test_frame_hash_rejects_wrong_length(make_page: Callable[[str], Page]) -> None:
    media = Media.objects.create(
        page=make_page("one"), kind=Media.Kind.POSTER, url="https://cdn.test/a.jpg"
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        FrameHash.objects.create(media=media, pdq=b"\x00" * 31, quality=90)
