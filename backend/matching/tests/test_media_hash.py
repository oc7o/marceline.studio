import subprocess
import sys

import pytest
from PIL import Image

from index.models import Media
from matching import media_hash
from matching.frames import hash_poster, hash_sprite
from matching.media_hash import UnsupportedMediaError, hash_media_bytes
from matching.media_worker import hash_media
from matching.pdq import MATCH_DISTANCE, hamming_distance
from matching.tests.images import sprite_sheet, synthetic_image, to_bytes


def test_kind_strings_are_media_kinds() -> None:
    kinds = set(Media.Kind.values)
    assert kinds >= media_hash.STILL_IMAGE_KINDS
    assert media_hash.PREVIEW_KIND in kinds
    assert {Media.Kind.POSTER, Media.Kind.FRAME, Media.Kind.PHOTO} == media_hash.STILL_IMAGE_KINDS


def test_the_worker_and_the_kind_string_hash_alike() -> None:
    data = to_bytes(synthetic_image(2))
    assert hash_media(Media.Kind.POSTER, data) == hash_media_bytes("poster", data)
    with pytest.raises(UnsupportedMediaError):
        hash_media_bytes("sprite", data)


def test_sprite_hashes_with_a_declared_grid() -> None:
    """An adapter that read the grid off the site itself (issue #14) is believed outright, no
    geometry detection needed — `sprite_grid` is passed straight to `hash_sprite`."""
    data = to_bytes(sprite_sheet(4, 3, (120, 90)))

    hashed = hash_media_bytes("sprite", data, sprite_grid=(4, 3))

    assert (hashed.width, hashed.height) == (480, 270)
    assert [f.frame_index for f in hashed.frames] == list(range(12))
    assert hashed.frames == hash_sprite(data, grid=(4, 3))


def test_sprite_hashes_with_a_confidently_detected_grid() -> None:
    """No declared grid: the sheet's own geometry is unambiguous (matching.sprite_grid), so the
    worker still gets tiles instead of refusing."""
    data = to_bytes(sprite_sheet(5, 4, (100, 100)))  # matches test_sprite_grid's confident case

    hashed = hash_media_bytes("sprite", data)

    assert [f.frame_index for f in hashed.frames] == list(range(20))
    assert hashed.frames == hash_sprite(data, grid=(5, 4))


def test_sprite_refuses_when_the_grid_is_neither_declared_nor_confident() -> None:
    """A plain, untiled image (or an ambiguous sheet): refuse rather than hash a guessed grid."""
    with pytest.raises(UnsupportedMediaError, match="sprite grid unknown"):
        hash_media_bytes("sprite", to_bytes(synthetic_image(2)))


def test_crop_bars_is_off_by_default_and_a_no_op_without_bars() -> None:
    """The flag reaches the hash, and a frame without bars is unaffected by it."""
    data = to_bytes(synthetic_image(3))
    assert hash_media_bytes("poster", data) == hash_media_bytes("poster", data, crop_bars=True)


def test_crop_bars_changes_a_letterboxed_poster() -> None:
    source = synthetic_image(4)
    padded = Image.new("RGB", (source.width, source.height + 48), (0, 0, 0))
    padded.paste(source, (0, 24))
    data = to_bytes(padded)

    plain = hash_media_bytes("poster", data)
    cropped = hash_media_bytes("poster", data, crop_bars=True)

    original = hash_poster(to_bytes(source))[0].hash
    # The stored dimensions stay the media's own, cropped or not.
    assert (cropped.width, cropped.height) == (padded.width, padded.height)
    assert hamming_distance(original, plain.frames[0].hash) > MATCH_DISTANCE
    assert hamming_distance(original, cropped.frames[0].hash) <= MATCH_DISTANCE


def test_hashing_by_kind_needs_no_django() -> None:
    # The crawl runtime's hash children import this module without Django (ADR 0030).
    code = (
        "import sys, marceline.runtime.hash_pool, matching.media_hash; "
        "sys.exit(1 if any(name.startswith('django') for name in sys.modules) else 0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
