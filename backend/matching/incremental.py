"""Incremental reference matching: the candidate scan cache and the settings fingerprint (ADR 0015).

The expensive part of `match_references` is the Hamming scan of every page frame against every
reference hash. Its result is sparse: only page frames within the matching radius of some
reference frame can ever become evidence, shared-frame page groups or common-frame discounts.
`ReferenceCandidate` stores those (frame, reference, smallest distance) pairs. A normal run scans
only
- page frames created since the previous scan started (minus `FRAME_OVERLAP`, for transactions
  that committed late) against all references — frames of pages reactivated since then count as
  new, and frames of inactive pages are never scanned (issue #71) — and
- references that are new or whose eligible frames changed (`reference_signatures`) against all
  page frames.
Everything downstream (shared flags, evidence, timeline, corroboration, confidence) is recomputed
from the candidate frames on every run, so results equal a full run on the same data.

A full rescan (`match_references --full`) rebuilds the cache. It also happens automatically when
the fingerprint of what the *scan* depends on changes (`scan_settings`): `SCAN_VERSION`, the scan
radius (the larger of the strict and loose distance), the minimum quality and the hash identity
(hash size, exact or approximate search) and whether the reference-side crop grid is consulted
(ADR 0039 — it changes which triples lie within the radius for every gridded reference, so
switching it on or off costs exactly one full rescan).
Nothing else changes which (frame, reference, distance)
triples lie within the radius. Shared reference frames, common-frame groups, confidence thresholds,
retired or imported references and human decisions only change the aggregation, which every run
recomputes from the candidates anyway, so they do not force a full rescan (until 2026-09-17 they
did, at ~110 s per run on the live index).
"""

import hashlib
import json
import time
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from django.db.models import Q
from django.utils import timezone
from numpy.typing import NDArray

from index.models import FrameHash
from matching import ann, crop_index
from matching.hamming import HASH_BYTES, HashIndex, to_words
from matching.models import MatchRunState, Reference, ReferenceCandidate
from matching.reference_hashes import ReferenceHashes, load_reference_hashes, signatures

# Bump when the scan itself changes meaning: forces one full rescan everywhere.
SCAN_VERSION = 1
# Page frames committed up to this long after their created_at are still scanned.
FRAME_OVERLAP = timedelta(hours=1)
FULL = "full"
INCREMENTAL = "incremental"
_INSERT_BATCH = 5000


def scan_candidates(
    reference_hashes: Sequence[tuple[int, bytes]],
    page_hashes: Sequence[tuple[int, bytes]],
    radius: int,
) -> dict[tuple[int, int], int]:
    """Pure: (frame_id, reference_id) -> smallest distance <= `radius`.

    `reference_hashes` are (reference_id, hash) with repeated ids; `page_hashes` (frame_id, hash).
    """
    if not reference_hashes or not page_hashes:
        return {}
    return scan_candidate_words(
        np.asarray([rid for rid, _ in reference_hashes], dtype=np.int64),
        to_words([value for _, value in reference_hashes]),
        np.asarray([fid for fid, _ in page_hashes], dtype=np.int64),
        to_words([value for _, value in page_hashes]),
        radius,
    )


def scan_candidate_words(
    reference_ids: NDArray[np.int64],
    reference_words: NDArray[np.uint64],
    frame_ids: NDArray[np.int64],
    frame_words: NDArray[np.uint64],
    radius: int,
) -> dict[tuple[int, int], int]:
    """`scan_candidates` over packed (N, 4) uint64 hash arrays (no bytes round trip, RE3)."""
    if not len(reference_ids) or not len(frame_ids):
        return {}
    index = HashIndex.from_words(reference_ids, reference_words)
    positions, ids, distances = index.pairs_within(frame_words, radius)
    found: dict[tuple[int, int], int] = {}
    for position, reference_id, distance in zip(
        frame_ids[positions].tolist(), ids.tolist(), distances.tolist(), strict=True
    ):
        key = (position, reference_id)
        if distance < found.get(key, radius + 1):
            found[key] = distance
    return found


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def scan_settings(*, radius: int, min_quality: int) -> dict[str, Any]:
    """Everything the candidate cache depends on besides the hashes themselves.

    The hashes are covered separately: new page frames by `frames_scanned_at`, reference edits by
    `reference_signatures`. An approximate ANN backend (ADR 0024) can miss neighbours, so switching
    between exact and approximate search invalidates the cache too — and so does the crop grid
    (ADR 0039), which changes *which* (frame, reference, distance) triples lie within the radius
    for every reference that carries one. Switching it on or off therefore costs one full rescan.
    That is the "second full rescan the first time grids appear" the ADR lists as a cost.
    """
    approximate = ann.resolve_backend(ann.MIN_FAISS_SIZE) == ann.IVF
    return {
        "version": SCAN_VERSION,
        "radius": radius,
        "min_quality": min_quality,
        "hash_bytes": HASH_BYTES,
        "search": "approximate" if approximate else "exact",
        "crop_grid": crop_index.enabled(),
    }


def fingerprint(settings: dict[str, Any]) -> str:
    return _digest(settings)


def lock_state() -> MatchRunState:
    """The singleton state row, locked for the rest of the transaction (serializes runs)."""
    MatchRunState.objects.get_or_create(pk=1)
    return MatchRunState.objects.select_for_update().get(pk=1)


def reference_signatures(
    min_quality: int, hashes: ReferenceHashes | None = None
) -> dict[str, list[int]]:
    """Reference id -> [eligible frame count, last eligible frame id]; changes on any edit.

    Computed from `hashes`, the run's snapshot, so the scan and the recorded coverage always
    describe the same rows."""
    snapshot = load_reference_hashes() if hashes is None else hashes
    return signatures(snapshot, Reference.objects.values_list("pk", flat=True), min_quality)


def _page_hashes(
    min_quality: int, since: datetime | None = None
) -> tuple[NDArray[np.int64], NDArray[np.uint64]]:
    """(frame ids, packed hashes) of quality-eligible frames of active pages."""
    # Only active pages are matched (issue #71). A page reactivated since the last scan counts as
    # new: its frames are old, but the scan has never seen them while it was inactive.
    rows = FrameHash.objects.filter(quality__gte=min_quality, media__page__active=True)
    if since is not None:
        rows = rows.filter(Q(created_at__gte=since) | Q(media__page__active_changed_at__gte=since))
    ids: list[int] = []
    values: list[bytes] = []
    for pk, pdq in rows.order_by("pk").values_list("pk", "pdq").iterator(chunk_size=_INSERT_BATCH):
        ids.append(pk)
        values.append(bytes(pdq))
    return np.asarray(ids, dtype=np.int64), to_words(values)


def _store(found: Iterable[tuple[tuple[int, int], int]]) -> None:
    rows = [
        ReferenceCandidate(frame_id=frame_id, reference_id=reference_id, distance=distance)
        for (frame_id, reference_id), distance in found
    ]
    ReferenceCandidate.objects.bulk_create(rows, batch_size=_INSERT_BATCH, ignore_conflicts=True)


def grid_reference_ids(min_quality: int) -> set[int]:
    """References carrying at least one crop grid (ADR 0039); empty set when the signal is off."""
    if not crop_index.enabled():
        return set()
    return set(
        crop_index.grid_rows(min_quality=min_quality)
        .values_list("reference_id", flat=True)
        .distinct()
    )


def scan_grid(
    reference_ids: Collection[int],
    frames: tuple[NDArray[np.int64], NDArray[np.uint64]],
    radius: int,
    min_quality: int,
) -> dict[tuple[int, int], int]:
    """`scan_candidate_words` through the crop grid: (frame_id, reference_id) -> grid minimum.

    Stage 1 searches the moments' corner crops wide (`crop_grid.STAGE1_RADIUS`) and stage 2 takes
    the exact minimum over the whole lattice; only minima within `radius` come back, so the
    candidate cache holds the same kind of number it always did — a PDQ Hamming distance the
    aggregation may treat as evidence.
    """
    frame_ids, words = frames
    if not reference_ids or not len(frame_ids):
        return {}
    stage1 = crop_index.load_stage1(reference_ids, min_quality)
    found: dict[tuple[int, int], int] = {}
    for hit in crop_index.search(stage1, words, radius=radius):
        key = (int(frame_ids[hit.position]), hit.reference_id)
        if hit.distance < found.get(key, radius + 1):
            found[key] = hit.distance
    return found


def _merge(
    found: dict[tuple[int, int], int], extra: dict[tuple[int, int], int], radius: int
) -> None:
    """Keep the smaller distance per (frame, reference); the grid can only ever improve one."""
    for key, distance in extra.items():
        if distance < found.get(key, radius + 1):
            found[key] = distance


@dataclass(frozen=True)
class ScanReport:
    mode: str
    page_frames: int
    references: int
    candidates: int
    seconds: float
    grid_references: int = 0
    grid_candidates: int = 0

    def line(self) -> str:
        if self.mode == FULL:
            scope = f"{self.page_frames} page frames x {self.references} references"
        else:
            scope = (
                f"{self.page_frames} new page frames, {self.references} new or changed references"
            )
        grid = (
            ""
            if not self.grid_references
            else (
                f"; crop grid: {self.grid_candidates} pairs from "
                f"{self.grid_references} gridded references"
            )
        )
        return (
            f"Scan: {self.mode} ({scope}); {self.candidates} candidate frame/reference pairs"
            f"{grid}; {self.seconds:.1f} s"
        )


def refresh_candidates(
    state: MatchRunState,
    *,
    radius: int,
    min_quality: int,
    full: bool,
    now: datetime | None = None,
    hashes: ReferenceHashes | None = None,
) -> ScanReport:
    """Bring `ReferenceCandidate` up to date and record the coverage on `state` (not saved).

    `hashes` is the run's reference hash snapshot (`load_reference_hashes`); loaded here if absent.
    """
    started = now or timezone.now()
    clock = time.perf_counter()
    snapshot = load_reference_hashes() if hashes is None else hashes
    signatures = reference_signatures(min_quality, snapshot)
    eligible = snapshot.eligible(min_quality)

    def scan(
        mask: NDArray[np.bool_], frames: tuple[NDArray[np.int64], NDArray[np.uint64]]
    ) -> dict[tuple[int, int], int]:
        return scan_candidate_words(
            snapshot.reference_id[mask], snapshot.words[mask], frames[0], frames[1], radius
        )

    # References carrying a crop grid (ADR 0039), or nothing at all while the signal is off. A
    # grid only ever *adds* pairs or lowers a distance the plain scan already found, so it goes
    # through exactly the same code path and the same radius as everything else.
    gridded = grid_reference_ids(min_quality)
    grid_found = 0
    if full or state.frames_scanned_at is None:
        mode = FULL
        ReferenceCandidate.objects.all().delete()
        pages = _page_hashes(min_quality)
        found = scan(eligible, pages)
        grid = scan_grid(gridded, pages, radius, min_quality)
        grid_found = len(grid)
        _merge(found, grid, radius)
        _store(found.items())
        references = len(signatures)
    else:
        mode = INCREMENTAL
        changed = [
            int(pk)
            for pk, value in signatures.items()
            if state.reference_signatures.get(pk) != value
        ]
        is_changed = snapshot.of_references(changed)
        pages = _page_hashes(min_quality, since=state.frames_scanned_at - FRAME_OVERLAP)
        found = scan(eligible & ~is_changed, pages)
        grid = scan_grid(gridded - set(changed), pages, radius, min_quality)
        if changed:
            ReferenceCandidate.objects.filter(reference_id__in=changed).delete()
            everything = _page_hashes(min_quality)
            found.update(scan(eligible & is_changed, everything))
            _merge(grid, scan_grid(gridded & set(changed), everything, radius, min_quality), radius)
        grid_found = len(grid)
        _merge(found, grid, radius)
        _store(found.items())
        references = len(changed)
    report = ScanReport(
        mode=mode,
        page_frames=len(pages[0]),
        references=references,
        candidates=ReferenceCandidate.objects.count(),
        seconds=time.perf_counter() - clock,
        grid_references=len(gridded),
        grid_candidates=grid_found,
    )
    state.reference_signatures = signatures
    state.frames_scanned_at = started
    state.last_mode = mode
    state.last_run_at = started
    if mode == FULL:
        state.last_full_at = started
    state.last_scanned_frames = report.page_frames
    state.last_candidates = report.candidates
    state.last_scan_seconds = report.seconds
    return report
