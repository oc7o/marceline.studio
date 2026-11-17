"""Synthetic, content-free test images: gradients, random shapes and text."""

import io

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance

SIZE = (640, 360)


def synthetic_image(seed: int, size: tuple[int, int] = SIZE) -> Image.Image:
    rng = np.random.default_rng(seed)
    width, height = size
    ramp = np.linspace(0, 1, width)[None, :, None] * np.linspace(0.3, 1, height)[:, None, None]
    base = rng.integers(0, 256, 3) * ramp + rng.integers(0, 256, 3) * (1 - ramp)
    noise = rng.normal(0, 6, (height, width, 3))
    image = Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x0, y0 = int(rng.integers(0, width)), int(rng.integers(0, height))
        x1 = x0 + int(rng.integers(20, width // 2))
        y1 = y0 + int(rng.integers(20, height // 2))
        colour = tuple(int(c) for c in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            draw.ellipse((x0, y0, x1, y1), fill=colour)
        else:
            draw.rectangle((x0, y0, x1, y1), fill=colour)
    draw.text((int(rng.integers(0, width // 2)), int(rng.integers(0, height - 20))), f"seed {seed}")
    return image


def jpeg(image: Image.Image, quality: int) -> Image.Image:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def resize_width(image: Image.Image, width: int) -> Image.Image:
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.Resampling.BILINEAR)


def crop_fraction(image: Image.Image, fraction: float) -> Image.Image:
    dx, dy = round(image.width * fraction), round(image.height * fraction)
    return image.crop((dx, dy, image.width - dx, image.height - dy))


def brightness(image: Image.Image, factor: float) -> Image.Image:
    return ImageEnhance.Brightness(image).enhance(factor)


def sprite_sheet(columns: int, rows: int, tile_size: tuple[int, int], seed: int = 0) -> Image.Image:
    """A synthetic sprite sheet: `columns` x `rows` distinct tiles, exactly `tile_size` each."""
    sheet = Image.new("RGB", (columns * tile_size[0], rows * tile_size[1]))
    for index in range(columns * rows):
        tile = synthetic_image(seed + index, tile_size)
        sheet.paste(tile, ((index % columns) * tile_size[0], (index // columns) * tile_size[1]))
    return sheet


def to_bytes(image: Image.Image, format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=format)
    return buffer.getvalue()
