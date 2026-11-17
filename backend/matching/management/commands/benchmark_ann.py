"""Measure recall, speed and memory of the Hamming search backends (ADR 0024).

Reads nothing from the database and writes nothing: the corpus is synthetic hashes, planted with
near-duplicates at the distances real PDQ transforms produce (jpeg 0-8, resize 4-30, 1% crop
10-42; see ADR 0024). Never uses real or pirated media.

Recall is measured against the exact answer from the numpy scan, at the strict threshold (31) and
the loose one (40). `query recall` is the share of queries with a true neighbour that found at
least one — the metric that decides whether a leak is missed, because the candidate scan needs
only one hit per (page frame, reference) pair.

    manage.py benchmark_ann --index 100000 --queries 20000 --backends numpy,flat,ivf:1024:32
"""

import resource
import time
from argparse import ArgumentParser
from typing import Any

import numpy as np
from django.core.management.base import BaseCommand

from matching import ann
from matching.hamming import HashIndex
from matching.pdq import MATCH_DISTANCE
from matching.references import LOOSE_MAX_DISTANCE

# How far a real re-encode moves a PDQ hash, from the ADR 0024 measurements.
LEAK_DISTANCES = (0, 2, 4, 8, 14, 20, 26, 31, 36, 40)


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def random_codes(count: int, rng: np.random.Generator) -> np.ndarray:
    return rng.integers(0, 256, (count, 32), dtype=np.uint8)


def flip(rows: np.ndarray, counts: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Copy of `rows` with `counts[i]` random bits flipped in row i."""
    out = rows.copy()
    for i, total in enumerate(counts.tolist()):
        for bit in rng.choice(256, size=int(total), replace=False):
            out[i, int(bit) >> 3] ^= np.uint8(1 << (int(bit) & 7))
    return out


def build_corpus(
    n_index: int, n_queries: int, n_leaks: int, seed: int
) -> tuple[list[bytes], list[bytes]]:
    """(index hashes, query hashes) with `n_leaks` planted near-duplicate pairs."""
    rng = np.random.default_rng(seed)
    n_leaks = min(n_leaks, n_index, n_queries)
    index = random_codes(n_index, rng)
    queries = random_codes(n_queries, rng)
    # Plant leaks: query i is a near-duplicate of index i, at a realistic distance.
    targets = index[:n_leaks]
    distances = np.asarray(
        [LEAK_DISTANCES[i % len(LEAK_DISTANCES)] for i in range(n_leaks)], dtype=np.int64
    )
    queries[:n_leaks] = flip(targets, distances, rng)
    return [row.tobytes() for row in index], [row.tobytes() for row in queries]


def as_sets(positions: np.ndarray, ids: np.ndarray, n_queries: int) -> list[set[int]]:
    found: list[set[int]] = [set() for _ in range(n_queries)]
    for position, found_id in zip(positions.tolist(), ids.tolist(), strict=True):
        found[position].add(int(found_id))
    return found


def recall(found: list[set[int]], truth: list[set[int]]) -> tuple[float, int]:
    hits = sum(len(f & t) for f, t in zip(found, truth, strict=True))
    total = sum(len(t) for t in truth)
    return (hits / total if total else 1.0), total - hits


def query_recall(found: list[set[int]], truth: list[set[int]]) -> tuple[float, int]:
    pairs = [(f, t) for f, t in zip(found, truth, strict=True) if t]
    ok = sum(1 for f, t in pairs if f & t)
    return (ok / len(pairs) if pairs else 1.0), len(pairs) - ok


class Command(BaseCommand):
    help = "Measure recall, speed and memory of the Hamming search backends (ADR 0024)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--index", type=int, default=100_000, help="index hashes")
        parser.add_argument("--queries", type=int, default=20_000, help="query hashes")
        parser.add_argument("--leaks", type=int, default=3_000, help="planted near-duplicates")
        parser.add_argument("--seed", type=int, default=7)
        parser.add_argument(
            "--backends",
            default="numpy,flat,ivf:1024:32",
            help="comma-separated: numpy, flat, ivf[:NLIST:NPROBE]",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        n_index: int = options["index"]
        n_queries: int = options["queries"]
        radius = LOOSE_MAX_DISTANCE

        self.stdout.write(
            f"Corpus: {n_index} index hashes, {n_queries} queries, "
            f"{options['leaks']} planted leaks (synthetic)"
        )
        index_hashes, query_hashes = build_corpus(
            n_index, n_queries, options["leaks"], options["seed"]
        )

        exact = HashIndex(list(range(n_index)), index_hashes, backend=ann.NUMPY)
        started = time.perf_counter()
        positions, ids, distances = exact.pairs_within(query_hashes, radius)
        exact_seconds = time.perf_counter() - started
        truth_loose = as_sets(positions, ids, n_queries)
        strict = distances <= MATCH_DISTANCE
        truth_strict = as_sets(positions[strict], ids[strict], n_queries)
        self.stdout.write(
            f"Exact answer: {len(ids)} pairs within {radius}, "
            f"{int(strict.sum())} within {MATCH_DISTANCE} ({exact_seconds:.1f} s)"
        )

        header = (
            f"{'backend':<18}{'build s':>9}{'search s':>10}{'qps':>10}"
            f"{'recall@31':>11}{'recall@40':>11}{'qrecall@31':>12}"
            f"{'missed@31':>11}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for name in str(options["backends"]).split(","):
            parts = name.split(":")
            kwargs: dict[str, int] = {}
            if parts[0] == ann.IVF and len(parts) == 3:
                kwargs = {"nlist": int(parts[1]), "nprobe": int(parts[2])}
            started = time.perf_counter()
            index = HashIndex(list(range(n_index)), index_hashes, backend=parts[0], **kwargs)
            # Building is lazy, so force it before timing the search.
            index.query(query_hashes[0], radius)
            build_seconds = time.perf_counter() - started

            started = time.perf_counter()
            positions, ids, distances = index.pairs_within(query_hashes, radius)
            search_seconds = time.perf_counter() - started

            found_loose = as_sets(positions, ids, n_queries)
            keep = distances <= MATCH_DISTANCE
            found_strict = as_sets(positions[keep], ids[keep], n_queries)
            r_strict, missed_strict = recall(found_strict, truth_strict)
            r_loose, _ = recall(found_loose, truth_loose)
            q_strict, _ = query_recall(found_strict, truth_strict)
            self.stdout.write(
                f"{name:<18}{build_seconds:>9.2f}{search_seconds:>10.2f}"
                f"{n_queries / max(search_seconds, 1e-9):>10.0f}"
                f"{r_strict:>11.6f}{r_loose:>11.6f}{q_strict:>12.6f}"
                f"{missed_strict:>11}"
            )

        # ru_maxrss is the high-water mark of the whole process, so it cannot be attributed to
        # one backend: per-backend memory needs one process per backend (ADR 0024).
        self.stdout.write(f"\nPeak RSS for this process: {peak_rss_mb():.0f} MB")
        self.stdout.write(
            "Exact backends (numpy, flat) must show recall 1.000000. Any miss in an "
            "approximate backend is a leak the matcher would never see: re-ranking removes "
            "false positives, it cannot recover a candidate the index did not return."
        )
