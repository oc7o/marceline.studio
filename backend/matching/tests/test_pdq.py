from collections.abc import Callable

import pytest
from PIL import Image, ImageOps

from matching.pdq import (
    MATCH_DISTANCE,
    MIN_QUALITY,
    MediaDecodeError,
    from_hex,
    hamming_distance,
    hash_image,
    hash_image_bytes,
    to_hex,
)
from matching.tests.images import (
    brightness,
    crop_fraction,
    jpeg,
    resize_width,
    synthetic_image,
    to_bytes,
)

SEEDS = range(1, 6)

TRANSFORMS: dict[str, Callable[[Image.Image], Image.Image]] = {
    "jpeg q40": lambda image: jpeg(image, 40),
    "resize to 160px": lambda image: resize_width(image, 160),
    "crop 0.5% per side": lambda image: crop_fraction(image, 0.005),
    "brighter x1.3": lambda image: brightness(image, 1.3),
    "darker x0.7": lambda image: brightness(image, 0.7),
    "jpeg q60 of 160px": lambda image: jpeg(resize_width(image, 160), 60),
}


def test_hash_shape_and_quality() -> None:
    result = hash_image(synthetic_image(1))
    assert len(result.hash) == 32
    assert MIN_QUALITY <= result.quality <= 100
    assert hash_image(synthetic_image(1)) == result


def test_bytes_and_image_inputs_agree() -> None:
    image = synthetic_image(2)
    assert hash_image_bytes(to_bytes(image)) == hash_image(image)


@pytest.mark.parametrize("name", TRANSFORMS)
@pytest.mark.parametrize("seed", SEEDS)
def test_robust_to_common_reupload_transformations(name: str, seed: int) -> None:
    original = synthetic_image(seed)
    transformed = TRANSFORMS[name](original)
    distance = hamming_distance(hash_image(original).hash, hash_image(transformed).hash)
    assert distance <= MATCH_DISTANCE


def test_unrelated_images_are_far_apart() -> None:
    hashes = [hash_image(synthetic_image(seed)).hash for seed in SEEDS]
    for i, first in enumerate(hashes):
        for second in hashes[i + 1 :]:
            assert hamming_distance(first, second) >= 64


@pytest.mark.parametrize("seed", SEEDS)
def test_horizontal_flip_is_not_matched(seed: int) -> None:
    """Documents a known PDQ limitation: mirrored copies need dihedral hashing."""
    original = synthetic_image(seed)
    flipped = ImageOps.mirror(original)
    assert hamming_distance(hash_image(original).hash, hash_image(flipped).hash) > MATCH_DISTANCE


@pytest.mark.parametrize("seed", SEEDS)
def test_larger_crops_are_not_matched(seed: int) -> None:
    """Documents a known PDQ limitation: ~1% crop per side already reaches the threshold,
    3% lands far beyond it. Cropped reuploads need frame-level or learned matching."""
    original = synthetic_image(seed)
    cropped = crop_fraction(original, 0.03)
    assert hamming_distance(hash_image(original).hash, hash_image(cropped).hash) > MATCH_DISTANCE


def test_featureless_images_have_low_quality() -> None:
    assert hash_image(Image.new("RGB", (320, 180), "black")).quality < MIN_QUALITY


def test_hex_round_trip_and_validation() -> None:
    hash_ = hash_image(synthetic_image(3)).hash
    assert len(to_hex(hash_)) == 64
    assert from_hex(to_hex(hash_)) == hash_
    with pytest.raises(ValueError, match="32 bytes"):
        from_hex("abcd")
    with pytest.raises(ValueError, match="32 bytes"):
        hamming_distance(hash_, b"x")


def test_undecodable_bytes() -> None:
    with pytest.raises(MediaDecodeError):
        hash_image_bytes(b"not an image")
