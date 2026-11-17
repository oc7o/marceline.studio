"""Deterministic sketch-shaped bytes and a fake encoder, so tests need no model file.

Nothing in the test suite may download the 45 MB ONNX encoder or open a session (no network in
tests, and a model file is not a fixture). Everything that needs "an embedding" uses these.
"""

import numpy as np
from PIL import Image

from matching.embedding import SKETCH_BITS, SKETCH_BYTES


def random_sketches(count: int, seed: int) -> list[bytes]:
    rng = np.random.default_rng(seed)
    return [row.tobytes() for row in rng.integers(0, 256, (count, SKETCH_BYTES), dtype=np.uint8)]


def flip_bits(sketch: bytes, count: int, seed: int = 0) -> bytes:
    """Copy of `sketch` with exactly `count` distinct bits flipped."""
    rng = np.random.default_rng(seed)
    value = int.from_bytes(sketch)
    for bit in rng.choice(SKETCH_BITS, size=count, replace=False):
        value ^= 1 << int(bit)
    return value.to_bytes(SKETCH_BYTES)


class FakeEncoder:
    """A stand-in for `matching.embedding.Encoder`: same shape, no model, no onnxruntime.

    The sketch is derived from the picture's own bytes, so the same picture always gives the same
    sketch and two different pictures almost never do — the only two properties the pipeline
    relies on.
    """

    def __init__(self) -> None:
        self.calls = 0

    def sketch(self, image: Image.Image) -> bytes:
        self.calls += 1
        pixels = np.asarray(image.convert("RGB").resize((16, 16)), dtype=np.uint8)
        digest = np.frombuffer(pixels.tobytes(), dtype=np.uint8)
        return bytes(int(digest[index::SKETCH_BYTES].sum() % 256) for index in range(SKETCH_BYTES))

    def sketches(self, images: list[Image.Image]) -> list[bytes]:
        return [self.sketch(image) for image in images]
