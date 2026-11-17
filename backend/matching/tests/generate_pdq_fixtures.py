"""Regenerate the PDQ parity fixtures shared by the backend and the browser client.

Run manually (not part of the test run), from `backend/`:

    uv run python -m matching.tests.generate_pdq_fixtures

It writes `matching/tests/fixtures/pdq/*.json`: a small synthetic RGB image (base64) plus the
hashes and quality today's `pdqhash.compute_dihedral` produces for it. Those files are the
contract between the server's hasher and the WebAssembly build in `frontend/wasm/pdq`
(`frontend/src/lib/pdq.test.ts` reads the very same files).

Everything here is synthetic and deterministic - gradients, seeded noise, checkerboards,
near-uniform frames, odd sizes. No media, ever.
"""

import base64
import json
from pathlib import Path

import numpy as np
import pdqhash

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pdq"
# Keep fixtures a few kB each: the RGB buffer is stored inline as base64.
MAX_PIXELS = 64 * 64


def _gradient(width: int, height: int) -> np.ndarray:
    x = np.linspace(0, 255, width)[None, :]
    y = np.linspace(0, 255, height)[:, None]
    red = np.broadcast_to(x, (height, width))
    green = np.broadcast_to(y, (height, width))
    blue = (x + y) / 2
    return np.stack([red, green, blue], axis=2)


def _noise(width: int, height: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (height, width, 3)).astype(np.float64)


def _checkerboard(width: int, height: int, square: int) -> np.ndarray:
    rows = (np.arange(height) // square)[:, None]
    cols = (np.arange(width) // square)[None, :]
    mask = (rows + cols) % 2
    image = np.where(mask == 0, 20.0, 235.0)
    return np.stack([image, image * 0.6, 255.0 - image], axis=2)


def _near_uniform(width: int, height: int, seed: int, level: int) -> np.ndarray:
    """A flat frame with a whisper of noise: PDQ quality lands below MIN_QUALITY."""
    rng = np.random.default_rng(seed)
    base = np.full((height, width, 3), float(level))
    return base + rng.integers(0, 2, (height, width, 3))


def _stripes(width: int, height: int, period: int) -> np.ndarray:
    cols = np.arange(width)[None, :]
    value = np.where((cols // period) % 2 == 0, 30.0, 220.0)
    image = np.broadcast_to(value, (height, width))
    return np.stack([image, image, image], axis=2)


def _diagonal_shapes(width: int, height: int, seed: int) -> np.ndarray:
    """Gradient plus a few blocks: structured, high quality, not symmetric."""
    rng = np.random.default_rng(seed)
    image = _gradient(width, height)
    for _ in range(4):
        x0 = int(rng.integers(0, max(1, width - 2)))
        y0 = int(rng.integers(0, max(1, height - 2)))
        x1 = min(width, x0 + int(rng.integers(2, max(3, width // 2))))
        y1 = min(height, y0 + int(rng.integers(2, max(3, height // 2))))
        image[y0:y1, x0:x1] = rng.integers(0, 256, 3).astype(np.float64)
    return image


def build_images() -> dict[str, np.ndarray]:
    """Name -> HxWx3 float array (clipped to uint8 later). Odd sizes on purpose."""
    return {
        # Smallest hashable size (PDQ's MIN_HASHABLE_DIM is 5).
        "tiny_5x5_gradient": _gradient(5, 5),
        "small_9x7_noise": _noise(9, 7, seed=11),
        "odd_17x9_stripes": _stripes(17, 9, period=3),
        "odd_23x37_gradient": _gradient(23, 37),
        "square_16x16_checker": _checkerboard(16, 16, square=2),
        "square_32x32_noise": _noise(32, 32, seed=7),
        "square_33x31_shapes": _diagonal_shapes(33, 31, seed=3),
        # Exactly the 64x64 the hasher downsamples to.
        "square_64x64_gradient": _gradient(64, 64),
        "square_64x64_noise": _noise(64, 64, seed=42),
        "wide_64x36_shapes": _diagonal_shapes(64, 36, seed=5),
        "tall_27x48_checker": _checkerboard(27, 48, square=5),
        # Featureless frames: quality below MIN_QUALITY (50), still hashed.
        "flat_24x24_dark": _near_uniform(24, 24, seed=1, level=8),
        "flat_40x30_mid": _near_uniform(40, 30, seed=2, level=127),
        # One column of content in an otherwise flat frame: low but nonzero quality.
        "sparse_48x27_edge": _stripes(48, 27, period=24),
    }


def to_uint8(image: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(np.clip(np.rint(image), 0, 255).astype(np.uint8))


def fixture(name: str, image: np.ndarray) -> dict[str, object]:
    rgb = to_uint8(image)
    height, width = rgb.shape[0], rgb.shape[1]
    if height * width > MAX_PIXELS:
        raise ValueError(f"{name}: {width}x{height} is larger than the fixture budget")
    variants, quality = pdqhash.compute_dihedral(rgb)
    hashes = [np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes().hex() for bits in variants]
    return {
        "name": name,
        "width": width,
        "height": height,
        # Row-major packed RGB24, exactly what the WASM wrapper takes.
        "rgb_base64": base64.b64encode(rgb.tobytes()).decode("ascii"),
        "quality": int(quality),
        # pdqhash.compute_dihedral order: original, rot90, rot180, rot270,
        # flipX, flipY, flipPlus1, flipMinus1.
        "hashes": hashes,
    }


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in FIXTURE_DIR.glob("*.json"):
        stale.unlink()
    names = []
    for name, image in build_images().items():
        data = fixture(name, image)
        path = FIXTURE_DIR / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        names.append(name)
        print(f"{path.name}: {data['width']}x{data['height']} quality={data['quality']}")
    print(f"\n{len(names)} fixtures in {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
