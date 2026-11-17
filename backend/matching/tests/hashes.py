"""Deterministic random PDQ-shaped hashes for tests."""

import numpy as np


def random_hashes(count: int, seed: int) -> list[bytes]:
    rng = np.random.default_rng(seed)
    return [row.tobytes() for row in rng.integers(0, 256, (count, 32), dtype=np.uint8)]


def flip_bits(hash_: bytes, count: int, seed: int = 0) -> bytes:
    """Copy of `hash_` with exactly `count` distinct bits flipped."""
    rng = np.random.default_rng(seed)
    value = int.from_bytes(hash_)
    for bit in rng.choice(256, size=count, replace=False):
        value ^= 1 << int(bit)
    return value.to_bytes(32)
