"""Black-bar detection and cropping before hashing (docs/matching/black-bars.md).

Synthetic images only (numpy/Pillow), as every matching test does.
"""

import io

import numpy as np
import pytest
from PIL import Image

from matching.black_bars import (
    DARK_LEVEL,
    NO_BARS,
    Bars,
    Rule,
    crop_bars,
    detect_bars,
)
from matching.pdq import hamming_distance, hash_image

RULES = (Rule.MAX, Rule.PERCENTILE)


def picture(width: int = 400, height: int = 226, *, seed: int = 7) -> Image.Image:
    """A 1/f-ish noise field: natural image statistics, well above the PDQ quality floor."""
    rng = np.random.default_rng(seed)
    frequencies = np.sqrt(
        np.fft.fftfreq(height)[:, None] ** 2 + np.fft.fftfreq(width)[None, :] ** 2
    )
    frequencies[0, 0] = 1e-6
    spectrum = frequencies**-1.0 * np.exp(2j * np.pi * rng.random((height, width)))
    field = np.real(np.fft.ifft2(spectrum))
    field -= field.min()
    field /= max(field.max(), 1e-9)
    plane = (40 + field * 215).astype(np.uint8)
    return Image.merge("RGB", [Image.fromarray(plane) for _ in range(3)])


def letterboxed(image: Image.Image, bar: int) -> Image.Image:
    canvas = Image.new("RGB", (image.width, image.height + 2 * bar), (0, 0, 0))
    canvas.paste(image, (0, bar))
    return canvas


def pillarboxed(image: Image.Image, bar: int) -> Image.Image:
    canvas = Image.new("RGB", (image.width + 2 * bar, image.height), (0, 0, 0))
    canvas.paste(image, (bar, 0))
    return canvas


def jpeg(image: Image.Image, quality: int = 72) -> Image.Image:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buffer.getvalue()))


@pytest.mark.parametrize("rule", RULES)
def test_no_bars_is_a_no_op(rule: Rule) -> None:
    """A frame without bars comes back as the same object and hashes identically."""
    image = jpeg(picture())
    cropped, bars = crop_bars(image, rule=rule)
    assert bars == NO_BARS
    assert not bars.found
    assert cropped is image
    assert hash_image(cropped).hash == hash_image(image).hash


@pytest.mark.parametrize("rule", RULES)
def test_letterbox_is_trimmed_and_the_hash_comes_back(rule: Rule) -> None:
    source = picture()
    padded = jpeg(letterboxed(source, 24))
    bars = detect_bars(padded, rule=rule)
    assert bars.letterbox and not bars.pillarbox
    assert 20 <= bars.top <= 27 and 20 <= bars.bottom <= 27

    original = hash_image(source).hash
    as_today = hamming_distance(original, hash_image(padded).hash)
    cropped, _ = crop_bars(padded, rule=rule)
    as_cropped = hamming_distance(original, hash_image(cropped).hash)
    # Padding costs far more than the match threshold; trimming it brings the picture back.
    assert as_today > 31
    assert as_cropped <= 31
    assert as_cropped < as_today


@pytest.mark.parametrize("rule", RULES)
def test_pillarbox_is_trimmed(rule: Rule) -> None:
    source = picture()
    padded = jpeg(pillarboxed(source, 40))
    bars = detect_bars(padded, rule=rule)
    assert bars.pillarbox and not bars.letterbox
    original = hash_image(source).hash
    assert hamming_distance(original, hash_image(padded).hash) > 31
    cropped, _ = crop_bars(padded, rule=rule)
    assert hamming_distance(original, hash_image(cropped).hash) <= 31


@pytest.mark.parametrize("rule", RULES)
def test_a_picture_fading_to_black_must_not_fire(rule: Rule) -> None:
    """The crop that must not happen: a dark picture edge is not a bar (step guard).

    Without the guard the detector trims 12-32 rows here and moves the frame 44-88 bits away
    from its own original — it would break matches that work today.
    """
    source = picture()
    faded = (
        np.asarray(source, dtype=np.float64) * np.linspace(0.0, 1.0, source.height)[:, None, None]
    )
    image = jpeg(Image.fromarray(faded.astype(np.uint8)))
    assert detect_bars(image, rule=rule) == NO_BARS
    cropped, bars = crop_bars(image, rule=rule)
    assert cropped is image and not bars.found


@pytest.mark.parametrize("rule", RULES)
def test_a_mostly_dark_frame_keeps_its_geometry(rule: Rule) -> None:
    """The minimum kept area stops a near-black frame from being cropped to a sliver."""
    image = Image.new("RGB", (400, 226), (0, 0, 0))
    image.paste(picture(400, 20), (0, 100))
    assert detect_bars(image, rule=rule) == NO_BARS


@pytest.mark.parametrize("rule", RULES)
def test_a_single_dark_edge_row_is_noise_not_a_bar(rule: Rule) -> None:
    source = picture()
    image = Image.new("RGB", (source.width, source.height + 1), (0, 0, 0))
    image.paste(source, (0, 1))
    assert detect_bars(image, rule=rule) == NO_BARS


def test_percentile_rule_survives_a_mark_inside_the_bar() -> None:
    """A logo burnt into the bar defeats the plain max rule; the percentile rule trims anyway."""
    source = picture()
    padded = letterboxed(source, 24)
    padded.paste(Image.new("RGB", (40, 20), (240, 240, 240)), (padded.width - 44, 2))
    image = jpeg(padded)
    plain = detect_bars(image, rule=Rule.MAX)
    assert plain.top == 0 and plain.bottom > 0  # the marked bar survives, the clean one does not
    percentile = detect_bars(image, rule=Rule.PERCENTILE)
    assert percentile.top > 0 and percentile.bottom > 0
    original = hash_image(source).hash
    assert hamming_distance(original, hash_image(image).hash) > 31
    cropped, _ = crop_bars(image, rule=Rule.PERCENTILE)
    assert hamming_distance(original, hash_image(cropped).hash) <= 31


def test_dark_level_is_a_luma_level_not_a_colour() -> None:
    """A saturated dark blue bar (luma under the level) is still a bar."""
    source = picture()
    canvas = Image.new("RGB", (source.width, source.height + 40), (0, 0, 40))
    canvas.paste(source, (0, 20))
    bars = detect_bars(canvas, rule=Rule.MAX)
    assert bars.top == 20 and bars.bottom == 20


def test_bars_box_and_flags() -> None:
    bars = Bars(top=2, bottom=3, left=4, right=5)
    assert bars.box(100, 50) == (4, 2, 95, 47)
    assert bars.found and bars.letterbox and bars.pillarbox
    assert not NO_BARS.found


@pytest.mark.parametrize("rule", RULES)
def test_tiny_and_odd_images_are_left_alone(rule: Rule) -> None:
    for image in (Image.new("RGB", (1, 1)), Image.new("L", (1, 8)), Image.new("RGB", (8, 1))):
        assert detect_bars(image, rule=rule) == NO_BARS


def test_dark_level_constant_is_a_byte() -> None:
    assert 0 < DARK_LEVEL < 255
