"""Local reference import and database matching workflows; no media persistence."""

import math
from collections import Counter, defaultdict
from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import NamedTuple

import numpy as np
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from numpy.typing import NDArray

from catalog.models import Creator
from index.models import FrameHash, Page
from matching import crop_index, embedding, incremental
from matching.calibration import current_thresholds
from matching.common_frames import active_common_groups, common_frame_ids
from matching.confidence import (
    HIGH,
    LOW,
    REVIEW,
    ConfidenceInputs,
    band_for,
    score_confidence,
)
from matching.confidence import collection_images as image_evidence
from matching.copy_extent import CLASSIFIER_VERSION, ExtentInputs, classify, matched_span
from matching.creators import creator_ids, set_creators
from matching.crop_index import GridHit
from matching.duration import page_duration
from matching.fingerprint import (
    EMBEDDING_ORIENTATION,
    PIPELINE,
    Fingerprint,
    file_sha256,
    fingerprint_video,
    pipeline_label,
)
from matching.hamming import HashIndex, to_words
from matching.leaks import active_cluster_pages, propagate_cluster_leaks, related_pages
from matching.models import (
    ClusterMember,
    Reference,
    ReferenceCandidate,
    ReferenceFrameHash,
    ReferenceLeak,
    ReferenceMatch,
)
from matching.pdq import MIN_QUALITY
from matching.reference_hashes import ReferenceHashes, load_reference_hashes
from matching.services import mirror_groups
from matching.timeline import (
    COLLECTION_REASON,
    FramePosition,
    TimelineFrame,
    TimelineResult,
    frame_position,
    no_timeline,
    verify_timeline,
)

# A frame matching pages from this many distinct page groups is common footage, not evidence.
COMMON_PAGE_GROUPS = 5
# Distinct page frames matching non-shared reference frames needed for a strong match.
STRONG_MIN_FRAMES = 2
# The strict match distance: below it a page frame is evidence on its own (ADR 0015). One
# number for the whole reference matcher — the candidate scan, shared-frame marking,
# evidence aggregation, the `match_references` CLI and the M2 measurement all read this, so
# they can never drift apart. Page-pair duplicate detection and cluster building have their
# own knob (`matching.duplicates.DEFAULT_MAX_DISTANCE`) and are deliberately not this one.
STRICT_MAX_DISTANCE = 36
# Looser distance for frames that only count with a consistent timeline or corroboration.
LOOSE_MAX_DISTANCE = 40
# A timeline admitting loose frames needs this many consistent frames, at least one strict.
LOOSE_MIN_CONSISTENT_FRAMES = 3
# Reference frame hash rows per INSERT when importing a fingerprint.
IMPORT_BATCH_SIZE = 512
# Advisory lock key for shared-flag recomputation (scheduler 73428200, build_clusters 73428109).
SHARED_FRAMES_LOCK = 73428110
# (reference_id, frame_index) packed into one int64: frame indexes stay far below 2**32.
_INDEX_SPAN = 1 << 32


def resolve_creator(name: str) -> Creator:
    candidates = [
        creator
        for creator in Creator.objects.all()
        if name.casefold()
        in {creator.name.casefold(), *(str(a).casefold() for a in creator.aliases)}
    ]
    if len(candidates) != 1:
        raise ValueError(f"Unknown or ambiguous creator: {name}")
    return candidates[0]


def resolve_creators(names: Sequence[str]) -> tuple[Creator, list[Creator]]:
    """(primary creator, co-performers) from repeatable `--creator` values, first name primary.

    Unknown or ambiguous names raise ValueError (`resolve_creator`), so an import fails with a
    clear message instead of silently dropping a performer. Repeated names collapse.
    """
    if not names:
        raise ValueError("at least one creator is required")
    resolved = [resolve_creator(name) for name in names]
    primary = resolved[0]
    seen = {primary.pk}
    others: list[Creator] = []
    for creator in resolved[1:]:
        if creator.pk not in seen:
            seen.add(creator.pk)
            others.append(creator)
    return primary, others


def select_references(
    creator: str | None = None, reference_id: int | None = None
) -> QuerySet[Reference]:
    references = (
        Reference.objects.select_related("creator").prefetch_related("creators").order_by("pk")
    )
    if creator:
        # Any performer finds the original, not only the primary creator (ADR 0010, 2026-09-16).
        found = resolve_creator(creator)
        references = references.filter(Q(creator=found) | Q(creators=found)).distinct()
    if reference_id is not None:
        references = references.filter(pk=reference_id)
        if not references.exists():
            raise ValueError(f"Reference {reference_id} not found in selection")
    return references


def add_reference(
    *,
    creator: Creator,
    path: Path,
    creators: Sequence[Creator] = (),
    title: str | None = None,
    fps: float = 1.0,
    source_note: str = "",
    dihedral: bool = True,
    embed: bool = False,
) -> Reference:
    """Fingerprint a local original on the server and import it (`import_fingerprint`).

    `creators` are the co-performers; `creator` stays the primary creator and is always linked.
    With `embed`, every sampled moment also gets the second signal's sketch (ADR 0037); the
    pinned model must be present, or this raises before any hashing starts.
    """
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be finite and positive")
    before = path.stat()
    digest = file_sha256(path)
    # Per pipeline and sampling rate: re-importing this file under the *current* recipe at the same
    # fps would only store the same hashes twice, but a row from an older recipe or at another
    # rate is a different grid and may stay (ADR 0022).
    existing = (
        Reference.objects.filter(sha256=digest, pipeline=PIPELINE, frame_fps=fps)
        .values_list("pk", flat=True)
        .first()
    )
    if existing is not None:
        raise ValueError(f"This file has already been imported (reference {existing})")
    model = settings.MATCHING_EMBEDDING_MODEL_PATH or None
    sketch = embedding.encoder(model).sketch if embed else None
    fingerprint = fingerprint_video(
        path,
        fps=fps,
        dihedral=dihedral,
        title=title,
        sha256=digest,
        file_stat=before,
        embed=sketch,
    )
    return import_fingerprint(
        creator=creator,
        creators=creators,
        fingerprint=fingerprint,
        source_note=source_note,
        source="server",
    )


@transaction.atomic
def import_fingerprint(
    *,
    creator: Creator,
    fingerprint: Fingerprint,
    creators: Sequence[Creator] = (),
    source_note: str = "",
    mark_shared: bool = True,
    source: str = "local-cli",
) -> Reference:
    """Store a fingerprint as a Reference atomically; `source` says where it was computed.

    Videos and photo collections take the same path: a collection is one reference whose
    `sha256` is its collection digest, whose images are `ReferenceFrameHash` rows ordered by
    `frame_index`, and which has no fps and no duration (ADR 0020).
    `creators` are the co-performers. The primary `creator` is always linked too, in the same
    transaction (`matching.creators.set_creators`), so the invariant never breaks halfway.
    Refuses a sha256 that already exists **on this pipeline at this sampling rate**; the same file
    under an older recipe or at another fps is a different sampling grid and is imported beside
    it, so the operator can confirm the new hashes before retiring the old row (issue #70,
    ADR 0022 and its 2026-09-17 amendment).
    With `mark_shared=False` the caller must run `mark_shared_frames()` afterwards (once for a
    batch of imports).
    """
    existing = Reference.objects.filter(
        sha256=fingerprint.sha256, pipeline=fingerprint.pipeline, frame_fps=fingerprint.fps
    ).values_list("pk", flat=True)
    if (pk := existing.first()) is not None:
        raise ValueError(
            f"File already imported as reference {pk} on {fingerprint.pipeline}"
            + ("" if fingerprint.fps is None else f" at {fingerprint.fps:g} fps")
        )
    reference = Reference.objects.create(
        creator=creator,
        kind=fingerprint.kind,
        title=fingerprint.title,
        duration_s=fingerprint.duration_s,
        sha256=fingerprint.sha256,
        source_note=source_note,
        frame_fps=fingerprint.fps,
        pipeline=fingerprint.pipeline,
        embedding_pipeline=fingerprint.embedding_pipeline,
        client={**fingerprint.client, "source": source},
    )
    set_creators(reference, creators)
    rows = (
        ReferenceFrameHash(
            reference=reference,
            frame_index=frame.frame_index,
            timestamp_s=frame.timestamp_s,
            pdq=frame.pdq,
            quality=frame.quality,
            dihedral=frame.dihedral,
            embedding=frame.embedding,
            crop_grid=frame.crop_grid,
        )
        for frame in fingerprint.frames
    )
    while batch := list(islice(rows, IMPORT_BATCH_SIZE)):
        ReferenceFrameHash.objects.bulk_create(batch)
    if mark_shared:
        mark_shared_frames()
    return reference


class EmbeddingAttachment(NamedTuple):
    """What `attach_embeddings` did to one existing reference."""

    reference_id: int
    positions: int


@transaction.atomic
def attach_embeddings(fingerprint: Fingerprint) -> EmbeddingAttachment:
    """Add the second signal to a reference that already holds exactly these PDQ hashes (0037).

    The reference side of the embedding is free and retroactive, and that is the whole reason
    this function exists: the operator re-runs `client/fingerprint.py --embeddings` over the same
    originals, and the sketches land **on the existing rows** instead of importing 600,000
    duplicate PDQ hashes beside them. The frame grid did not change, so the reference's identity
    (sha256, pipeline, frame fps — ADR 0022) did not either.

    Safety comes from parity, not from trust: every unflipped position's PDQ hash must match the
    stored row bit for bit, and the position sets must be equal. Anything else raises, because a
    sketch attached to a *different* grid would silently claim to describe a moment it never saw.
    Idempotent; refuses a reference that already carries another recipe's sketches.
    """
    if not fingerprint.embedding_pipeline:
        raise ValueError("this fingerprint file carries no embeddings")
    reference = (
        Reference.objects.select_for_update()
        .filter(sha256=fingerprint.sha256, pipeline=fingerprint.pipeline, frame_fps=fingerprint.fps)
        .first()
    )
    if reference is None:
        raise ValueError(
            f"no reference of {fingerprint.sha256[:12]} on {fingerprint.pipeline}"
            + ("" if fingerprint.fps is None else f" at {fingerprint.fps:g} fps")
        )
    if reference.embedding_pipeline not in ("", fingerprint.embedding_pipeline):
        raise ValueError(
            f"reference {reference.pk} carries {reference.embedding_pipeline} sketches already; "
            "retire it instead of mixing recipes"
        )
    stored = {
        index: bytes(pdq)
        for index, pdq in ReferenceFrameHash.objects.filter(
            reference=reference, dihedral=EMBEDDING_ORIENTATION
        ).values_list("frame_index", "pdq")
    }
    incoming = {
        frame.frame_index: frame
        for frame in fingerprint.frames
        if frame.dihedral == EMBEDDING_ORIENTATION
    }
    if set(stored) != set(incoming):
        raise ValueError(
            f"reference {reference.pk} has {len(stored)} positions, the file has "
            f"{len(incoming)}: not the same sampling grid"
        )
    differing = [index for index, pdq in stored.items() if incoming[index].pdq != pdq]
    if differing:
        raise ValueError(
            f"reference {reference.pk}: {len(differing)} position(s) hash differently "
            f"(first: {differing[0]}); the file is not a re-run of this original"
        )
    updated = 0
    for index, frame in incoming.items():
        updated += ReferenceFrameHash.objects.filter(
            reference=reference, frame_index=index, dihedral=EMBEDDING_ORIENTATION
        ).update(embedding=frame.embedding)
    reference.embedding_pipeline = fingerprint.embedding_pipeline
    reference.save(update_fields=("embedding_pipeline",))
    return EmbeddingAttachment(reference.pk, updated)


class SharedCandidate(NamedTuple):
    """One reference hash as seen by shared-frame detection.

    `creator_ids` holds every performer of the original, not only the primary creator: a page
    linked to any of them may be a leak of this very original (ADR 0010, 2026-09-16).
    `sha256` identifies the *file*, not the row: since issue #70 one original may be stored twice,
    once per pipeline, and those two rows are the same material rather than two originals that
    happen to share an intro.
    """

    reference_id: int
    sha256: str
    creator_ids: frozenset[int]
    frame_index: int
    dihedral: int
    pdq: bytes


def find_shared_frames(
    reference_frames: Sequence[SharedCandidate],
    page_frames: Iterable[tuple[int, bytes]],
    page_groups: Mapping[int, Hashable],
    page_creators: Mapping[int, Collection[int]],
    *,
    max_distance: int = STRICT_MAX_DISTANCE,
    common_page_groups: int = COMMON_PAGE_GROUPS,
) -> set[tuple[int, int]]:
    """(reference_id, frame_index) keys whose frames are not unique to one reference.

    Pure. A frame is shared when its dihedral-0 hash lies within `max_distance` of another
    reference's dihedral-0 hash (same creator included: a common intro), or when any of its
    orientations matches page frames from at least `common_page_groups` distinct groups
    (`page_groups[page_id]`, default: the page). Pages linked to *any* creator of the reference
    (`page_creators[page_id]` against `SharedCandidate.creator_ids`) are skipped there: many of
    them are likely leak copies, and a co-performer's name links them just as well.

    Two references of the same *file* (equal `sha256`) are exempt from the first rule: since issue
    #70 one original may be stored once per pipeline, and on slow-moving material the two grids sit
    close enough that they would otherwise mark each other's frames as common footage — deleting
    the evidence of both rows during the very window the operator needs them (ADR 0022).
    """
    page_rows = list(page_frames)
    digests: dict[int, str] = {}
    creators: dict[int, frozenset[int]] = {}
    for frame in reference_frames:
        digests.setdefault(frame.reference_id, frame.sha256)
        creators.setdefault(frame.reference_id, frame.creator_ids)
    return shared_frame_keys(
        np.asarray([frame.reference_id for frame in reference_frames], dtype=np.int64),
        np.asarray([frame.frame_index for frame in reference_frames], dtype=np.int64),
        np.asarray([frame.dihedral for frame in reference_frames], dtype=np.int64),
        to_words([frame.pdq for frame in reference_frames]),
        digests,
        creators,
        np.asarray([page_id for page_id, _ in page_rows], dtype=np.int64),
        to_words([value for _, value in page_rows]),
        page_groups,
        page_creators,
        max_distance=max_distance,
        common_page_groups=common_page_groups,
    )


def shared_frame_keys(
    reference_ids: NDArray[np.int64],
    frame_indexes: NDArray[np.int64],
    dihedrals: NDArray[np.int64],
    words: NDArray[np.uint64],
    digests: Mapping[int, str],
    reference_creators: Mapping[int, Collection[int]],
    page_ids: NDArray[np.int64],
    page_words: NDArray[np.uint64],
    page_groups: Mapping[int, Hashable],
    page_creators: Mapping[int, Collection[int]],
    *,
    max_distance: int = STRICT_MAX_DISTANCE,
    common_page_groups: int = COMMON_PAGE_GROUPS,
) -> set[tuple[int, int]]:
    """`find_shared_frames` over parallel arrays (one row per reference hash), fully batched.

    Both searches are single batched range searches (M3): the dihedral-0 self-join, and all page
    frames against every reference hash at once. The per-page-frame query loop this replaces cost
    ~22 ms per frame in OpenMP start-up alone (20 min for 55k page frames on an import).
    """
    shared: set[tuple[int, int]] = set()
    if not len(reference_ids):
        return shared
    originals = np.flatnonzero(dihedrals == 0)
    if len(originals):
        codes = {digest: code for code, digest in enumerate(sorted(set(digests.values())))}
        file_code = np.asarray(
            [codes[digests[rid]] for rid in reference_ids[originals].tolist()], dtype=np.int64
        )
        index = HashIndex.from_words(np.arange(len(originals), dtype=np.int64), words[originals])
        positions, ids, _ = index.pairs_within(words[originals], max_distance)
        own = originals[positions]
        other = originals[ids]
        keep = (reference_ids[own] != reference_ids[other]) & (
            file_code[positions] != file_code[ids]
        )
        shared.update(
            zip(reference_ids[own[keep]].tolist(), frame_indexes[own[keep]].tolist(), strict=True)
        )
    if not len(page_ids):
        return shared
    everything = HashIndex.from_words(np.arange(len(reference_ids), dtype=np.int64), words)
    positions, rows, _ = everything.pairs_within(page_words, max_distance)
    groups: dict[tuple[int, int], set[Hashable]] = defaultdict(set)
    creator_sets = {rid: frozenset(ids) for rid, ids in reference_creators.items()}
    for page_id, row in zip(page_ids[positions].tolist(), rows.tolist(), strict=True):
        reference_id = int(reference_ids[row])
        if creator_sets.get(reference_id, frozenset()).isdisjoint(page_creators.get(page_id, ())):
            group = page_groups.get(page_id, ("page", page_id))
            groups[(reference_id, int(frame_indexes[row]))].add(group)
    shared.update(key for key, seen in groups.items() if len(seen) >= common_page_groups)
    return shared


def active_page_groups() -> dict[int, Hashable]:
    """Pages in an active (non-rejected) cluster share the cluster's group."""
    return {
        page_id: ("cluster", cluster_id)
        for page_id, cluster_id in ClusterMember.objects.exclude(status="rejected").values_list(
            "page_id", "cluster_id"
        )
    }


def lock_shared_frames() -> None:
    """Serialize shared-flag recomputation (imports, matching runs) until the transaction ends.

    A transaction-level advisory lock, not `select_for_update` on every Reference row: those row
    locks also blocked every foreign-key insert against any reference (leak proposals, review
    decisions) for as long as the computation ran — minutes on an import before the searches were
    batched (M3). Re-entrant within one transaction.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SHARED_FRAMES_LOCK])


@transaction.atomic
def mark_shared_frames(
    *,
    max_distance: int = STRICT_MAX_DISTANCE,
    min_quality: int = MIN_QUALITY,
    common_page_groups: int = COMMON_PAGE_GROUPS,
    page_frames: QuerySet[FrameHash] | None = None,
    hashes: ReferenceHashes | None = None,
) -> int:
    """Recompute `ReferenceFrameHash.shared` for all references; returns shared frame positions.

    Idempotent. Frames below `min_quality` are never considered and end up not shared.
    `page_frames` narrows the page side to a superset of the frames within `max_distance` of any
    eligible reference frame (the candidate scan cache); default: all page frames.
    `hashes` is the matching run's snapshot of every reference hash (loaded here if absent); its
    `shared` array is updated in place to what was written.
    """
    lock_shared_frames()
    snapshot = load_reference_hashes() if hashes is None else hashes
    # Every performer of an original counts, not only the primary creator: pages found under a
    # co-performer's name are just as likely to be leaks of it (ADR 0010, 2026-09-16).
    linked_creators: dict[int, set[int]] = defaultdict(set)
    # The file behind each row: two pipelines of one original must not call each other shared.
    digests = dict(Reference.objects.values_list("pk", "sha256"))
    for reference_id, creator_id in Reference.objects.values_list("pk", "creator_id"):
        linked_creators[reference_id].add(creator_id)
    for reference_id, creator_id in Reference.creators.through.objects.values_list(
        "reference_id", "creator_id"
    ):
        linked_creators[reference_id].add(creator_id)
    eligible = snapshot.eligible(min_quality) & np.isin(
        snapshot.reference_id, np.fromiter(digests, dtype=np.int64)
    )
    page_creators: dict[int, set[int]] = defaultdict(set)
    for page_id, creator_id in Page.creators.through.objects.values_list("page_id", "creator_id"):
        page_creators[page_id].add(creator_id)
    frames = FrameHash.objects.all() if page_frames is None else page_frames
    page_ids: list[int] = []
    page_hashes: list[bytes] = []
    # Inactive pages (issue #71) are kept but never matched, so they mark nothing shared.
    for page_id, pdq in (
        frames.filter(quality__gte=min_quality, media__page__active=True)
        .order_by("pk")
        .values_list("media__page_id", "pdq")
        .iterator(chunk_size=5000)
    ):
        page_ids.append(page_id)
        page_hashes.append(bytes(pdq))
    keys = shared_frame_keys(
        snapshot.reference_id[eligible],
        snapshot.frame_index[eligible],
        snapshot.dihedral[eligible],
        snapshot.words[eligible],
        digests,
        linked_creators,
        np.asarray(page_ids, dtype=np.int64),
        to_words(page_hashes),
        active_page_groups(),
        page_creators,
        max_distance=max_distance,
        common_page_groups=common_page_groups,
    )
    key_codes = np.asarray(
        [reference_id * _INDEX_SPAN + frame_index for reference_id, frame_index in keys],
        dtype=np.int64,
    )
    wanted = eligible & np.isin(
        snapshot.reference_id * _INDEX_SPAN + snapshot.frame_index, key_codes
    )
    for value in (True, False):
        pks = snapshot.pk[(snapshot.shared != value) & (wanted == value)].tolist()
        for start in range(0, len(pks), 5000):
            ReferenceFrameHash.objects.filter(pk__in=pks[start : start + 5000]).update(shared=value)
    snapshot.shared[:] = wanted
    return len(keys)


class ReferenceFrame(NamedTuple):
    reference_id: int
    timestamp_s: float
    shared: bool
    pdq: bytes


class PageFrame(NamedTuple):
    """One quality-eligible page frame as seen by matching."""

    page_id: int
    pdq: bytes
    frame_id: int = 0
    position: FramePosition | None = None
    common: bool = False


@dataclass
class FrameMatch:
    """Hits of one page frame against one reference, as (distance, reference timestamp)."""

    position: FramePosition | None
    common: bool
    clean: list[tuple[int, float]] = field(default_factory=list)
    shared: list[tuple[int, float]] = field(default_factory=list)

    def best_clean(self) -> int | None:
        return min((d for d, _ in self.clean), default=None)

    def seconds(self, radius: int) -> tuple[float, ...]:
        return tuple(sorted({t for d, t in self.clean if d <= radius}))

    def nearest_seconds(self, radius: int) -> tuple[float, ...]:
        """Distinct clean hit seconds within `radius`, nearest first (ties: earlier first)."""
        return tuple(dict.fromkeys(t for d, t in sorted(self.clean) if d <= radius))


@dataclass
class PageEvidence:
    """Match evidence of one page against one reference.

    Page frames in active common-frame groups and hits on shared reference frames are never
    evidence. A frame whose best non-shared hit lies above `max_distance` but within
    `loose_max_distance` is evidence only once `resolve` admits it: as part of a consistent
    timeline (at least `LOOSE_MIN_CONSISTENT_FRAMES` frames, one of them strict), or when the page
    is corroborated by an active cluster / mirror group with a current strong match.

    `ordered=False` for a photo collection: its images have no order, so there is no timeline to
    verify and none to penalise, and a loose frame can only be admitted by corroboration.
    """

    max_distance: int = STRICT_MAX_DISTANCE
    loose_max_distance: int = LOOSE_MAX_DISTANCE
    ordered: bool = True
    frames: dict[int, FrameMatch] = field(default_factory=dict)
    admitted: set[int] = field(default_factory=set)
    corroborated: bool = False
    timeline: TimelineResult | None = None

    def strict_keys(self) -> set[int]:
        return {
            key
            for key, frame in self.frames.items()
            if not frame.common and (frame.best_clean() or 0) <= self.max_distance and frame.clean
        }

    def loose_keys(self) -> set[int]:
        return {
            key
            for key, frame in self.frames.items()
            if not frame.common
            and frame.clean
            and self.max_distance < (frame.best_clean() or 0) <= self.loose_max_distance
        }

    def _radius(self, key: int) -> int:
        return self.loose_max_distance if key in self.loose_keys() else self.max_distance

    def _timeline_frames(self, keys: Collection[int]) -> list[TimelineFrame]:
        return [
            TimelineFrame(
                key,
                self.frames[key].position,
                self.frames[key].nearest_seconds(self._radius(key)),
            )
            for key in sorted(keys)
        ]

    def resolve(self, *, corroborated: bool = False) -> None:
        """Admit loose frames and compute the timeline over evidence frames. Idempotent."""
        strict, loose = self.strict_keys(), self.loose_keys()
        self.corroborated = corroborated
        admitted: set[int] = set()
        if loose and corroborated:
            admitted = set(loose)
        elif loose and self.ordered:
            combined = verify_timeline(self._timeline_frames(strict | loose))
            if (
                combined.consistent
                and combined.consistent_frames >= LOOSE_MIN_CONSISTENT_FRAMES
                and combined.chain & strict
            ):
                admitted = set(combined.chain & loose)
        self.admitted = admitted
        self.timeline = (
            verify_timeline(self._timeline_frames(strict | admitted))
            if self.ordered
            else no_timeline(COLLECTION_REASON)
        )

    @property
    def evidence_keys(self) -> set[int]:
        return self.strict_keys() | self.admitted

    @property
    def evidence_frames(self) -> int:
        return len(self.evidence_keys)

    @property
    def loose_frames(self) -> int:
        return len(self.admitted)

    def _strict_hit(self, frame: FrameMatch) -> bool:
        return any(d <= self.max_distance for d, _ in (*frame.clean, *frame.shared))

    @property
    def matched_frames(self) -> int:
        return sum(
            key in self.admitted or self._strict_hit(frame) for key, frame in self.frames.items()
        )

    @property
    def common_frames(self) -> int:
        return sum(frame.common and self._strict_hit(frame) for frame in self.frames.values())

    @property
    def collection_images(self) -> int:
        """Independent (page frame, collection image) evidence pairs; collections only."""
        return image_evidence(len(self.matched_seconds), self.evidence_frames)

    @property
    def strength(self) -> str:
        """`strong` needs STRONG_MIN_FRAMES pieces of evidence.

        For a video that is distinct page frames; for a photo collection it is independent image
        pairs, because two page frames showing the same photo are one picture, not two moments
        of one video (ADR 0020).
        """
        found = self.evidence_frames if self.ordered else self.collection_images
        return "strong" if found >= STRONG_MIN_FRAMES else "weak"

    @property
    def best_evidence(self) -> tuple[int, float]:
        """Lowest (distance, timestamp) among evidence hits, else among other stored hits."""
        clean = [
            hit
            for key in self.evidence_keys
            for hit in self.frames[key].clean
            if hit[0] <= self._radius(key)
        ]
        if clean:
            return min(clean)
        fallback = [
            hit
            for frame in self.frames.values()
            for hit in (*frame.clean, *frame.shared)
            if hit[0] <= self.max_distance
        ]
        if not fallback:
            raise ValueError("page has no stored evidence")
        return min(fallback)

    @property
    def matched_seconds(self) -> list[int]:
        return sorted(
            {
                int(t)
                for key in self.evidence_keys
                for t in self.frames[key].seconds(self._radius(key))
            }
        )

    @property
    def shared_seconds(self) -> list[int]:
        seconds = {
            int(t)
            for frame in self.frames.values()
            if not frame.common
            for d, t in frame.shared
            if d <= self.max_distance
        }
        return sorted(seconds - set(self.matched_seconds))

    @property
    def common_seconds(self) -> list[int]:
        seconds = {
            int(t)
            for frame in self.frames.values()
            if frame.common
            for d, t in (*frame.clean, *frame.shared)
            if d <= self.max_distance
        }
        return sorted(seconds - set(self.matched_seconds) - set(self.shared_seconds))


def aggregate_matches(
    reference_frames: Mapping[int, ReferenceFrame],
    page_frames: Iterable[PageFrame],
    *,
    max_distance: int = STRICT_MAX_DISTANCE,
    loose_max_distance: int = LOOSE_MAX_DISTANCE,
    unordered_references: Collection[int] = (),
) -> tuple[dict[tuple[int, int], PageEvidence], Counter[int]]:
    """Pure: evidence per (reference_id, page_id) and considered frames per page.

    Entries are resolved without corroboration; call `resolve(corroborated=True)` for pages a
    cluster or mirror group ties to a strong match. Entries with `matched_frames == 0` (only
    unadmitted loose hits) are not matches. Each page frame counts at most once per reference.
    `unordered_references` are photo collections: their hits get no timeline (ADR 0020).
    """
    rows = [row for _, row in sorted(reference_frames.items())]
    return aggregate_hash_matches(
        np.asarray([row.reference_id for row in rows], dtype=np.int64),
        np.asarray([row.timestamp_s for row in rows], dtype=np.float64),
        np.asarray([row.shared for row in rows], dtype=np.bool_),
        to_words([row.pdq for row in rows]),
        page_frames,
        max_distance=max_distance,
        loose_max_distance=loose_max_distance,
        unordered_references=unordered_references,
    )


def aggregate_hash_matches(
    reference_ids: NDArray[np.int64],
    timestamps: NDArray[np.float64],
    shared: NDArray[np.bool_],
    words: NDArray[np.uint64],
    page_frames: Iterable[PageFrame],
    *,
    max_distance: int = STRICT_MAX_DISTANCE,
    loose_max_distance: int = LOOSE_MAX_DISTANCE,
    unordered_references: Collection[int] = (),
    grid_hits: Sequence["GridHit"] = (),
) -> tuple[dict[tuple[int, int], PageEvidence], Counter[int]]:
    """`aggregate_matches` over parallel reference hash arrays (RE3), with one batched search.

    All page frames are searched in a single `pairs_within` call (M3) instead of one FAISS query
    per frame; hits are then replayed per frame in (distance, hash position) order, which is the
    order `HashIndex.query` returns, so the evidence is identical.

    `grid_hits` are crop-grid hits (ADR 0039) whose `position` indexes `page_frames`. They are
    replayed into exactly the same `FrameMatch` lists as the plain hits, because a crop of a moment
    is a PDQ hash of the original at that moment: same units, same threshold, same shared-frame
    rule, same timeline second. An empty sequence — the default, and what the matcher passes while
    `MATCHING_REFERENCE_CROP_GRID` is off — leaves every number here bit-for-bit as it was.
    """
    unordered = frozenset(unordered_references)
    radius = max(max_distance, loose_max_distance)
    frames = list(page_frames)
    totals: Counter[int] = Counter(frame.page_id for frame in frames)
    evidence: dict[tuple[int, int], PageEvidence] = {}
    if not frames or not len(reference_ids):
        return evidence, totals

    def record(key: int, reference_id: int, is_shared: bool, distance: int, second: float) -> None:
        if is_shared and distance > max_distance:
            return
        frame = frames[key]
        entry = evidence.setdefault(
            (reference_id, frame.page_id),
            PageEvidence(
                max_distance=max_distance,
                loose_max_distance=radius,
                ordered=reference_id not in unordered,
            ),
        )
        hits = entry.frames.setdefault(key, FrameMatch(frame.position, frame.common))
        (hits.shared if is_shared else hits.clean).append((distance, second))

    index = HashIndex.from_words(np.arange(len(reference_ids), dtype=np.int64), words)
    positions, hits_at, distances = index.pairs_within(
        to_words([frame.pdq for frame in frames]), radius
    )
    order = np.lexsort((hits_at, distances, positions))
    for key, row, distance in zip(
        positions[order].tolist(), hits_at[order].tolist(), distances[order].tolist(), strict=True
    ):
        record(key, int(reference_ids[row]), bool(shared[row]), distance, float(timestamps[row]))
    # The grid's minimum for a moment is never larger than that moment's uncropped hit, so this
    # can only lower a best distance or add a frame the plain scan never reached.
    for hit in grid_hits:
        record(hit.position, hit.reference_id, hit.shared, hit.distance, hit.timestamp_s)
    for entry in evidence.values():
        entry.resolve()
    return evidence, totals


def grid_evidence(
    reference_ids: Collection[int],
    page_frames: Sequence[PageFrame],
    radius: int,
    min_quality: int,
) -> list[GridHit]:
    """Crop-grid hits for these references against these page frames (ADR 0039), or nothing.

    Empty while `MATCHING_REFERENCE_CROP_GRID` is off, and empty when no selected reference
    carries a grid — in both cases `aggregate_hash_matches` produces exactly today's numbers.

    This repeats the stage-1 search `incremental.scan_grid` already did, on purpose and cheaply:
    the scan searches every active page frame, this searches only the frames that became
    candidates, and keeping the two apart is what lets the pure aggregation stay pure.
    """
    if not crop_index.enabled() or not page_frames:
        return []
    stage1 = crop_index.load_stage1(reference_ids, min_quality)
    if not len(stage1):
        return []
    return crop_index.search(stage1, to_words([frame.pdq for frame in page_frames]), radius=radius)


def auto_confirm_high() -> bool:
    """Django setting AUTO_CONFIRM_HIGH (default off): confirm high-band proposals as auto."""
    return bool(getattr(settings, "AUTO_CONFIRM_HIGH", False))


def load_page_frames(
    min_quality: int, frames: QuerySet[FrameHash] | None = None
) -> list[PageFrame]:
    """Quality-eligible frames of active pages (optionally only `frames`) with position and common
    flag. Inactive pages (issue #71) are never evidence."""
    source = FrameHash.objects.all() if frames is None else frames
    rows = list(
        source.filter(quality__gte=min_quality, media__page__active=True)
        .order_by("pk")
        .values_list(
            "pk", "media__page_id", "pdq", "media_id", "media__kind", "media__url", "frame_index"
        )
        .iterator(chunk_size=5000)
    )
    representatives, members = active_common_groups()
    common = common_frame_ids(((row[0], bytes(row[2])) for row in rows), representatives, members)
    return [
        PageFrame(
            page_id, bytes(pdq), pk, frame_position(kind, url, frame_index, media_id), pk in common
        )
        for pk, page_id, pdq, media_id, kind, url, frame_index in rows
    ]


def related_to_strong(
    evidence: Mapping[tuple[int, int], PageEvidence], reference_ids: Collection[int]
) -> dict[int, set[int]]:
    """Pages per reference that share an active cluster or mirror group with another page holding
    a current strong match; seeds rejected by a human (either table) do not count."""
    memberships = [*active_cluster_pages(), *mirror_groups()]
    rejected: dict[int, set[int]] = defaultdict(set)
    for model in (ReferenceMatch, ReferenceLeak):
        for reference_id, page_id in model.objects.filter(
            reference_id__in=reference_ids, status="rejected"
        ).values_list("reference_id", "page_id"):
            rejected[reference_id].add(page_id)
    strong: dict[int, set[int]] = defaultdict(set)
    for (reference_id, page_id), entry in evidence.items():
        if entry.strength == "strong" and page_id not in rejected[reference_id]:
            strong[reference_id].add(page_id)
    return {
        reference_id: set(related_pages([("group", memberships)], strong[reference_id]))
        for reference_id in reference_ids
    }


def page_frame_counts(page_ids: Collection[int], min_quality: int) -> Counter[int]:
    """Quality-eligible frames per page (`ReferenceMatch.page_frames`)."""
    ids = sorted(page_ids)
    totals: Counter[int] = Counter()
    for start in range(0, len(ids), 5000):
        for row in (
            FrameHash.objects.filter(
                quality__gte=min_quality, media__page_id__in=ids[start : start + 5000]
            )
            .values("media__page_id")
            .annotate(frames=Count("pk"))
        ):
            totals[row["media__page_id"]] = row["frames"]
    return totals


@transaction.atomic
def match_references(
    references: Iterable[Reference],
    *,
    max_distance: int = STRICT_MAX_DISTANCE,
    min_quality: int = MIN_QUALITY,
    loose_max_distance: int = LOOSE_MAX_DISTANCE,
    full: bool = False,
    list_unmatched: bool = False,
) -> list[str]:
    """Match references against page frames; the Hamming scan is incremental (ADR 0015).

    Only page frames new since the last scan and new or changed references are scanned; all
    evidence is recomputed from the candidate frames, so results equal a full run. `full`
    rebuilds the scan cache; a changed settings fingerprint does so automatically.
    Each reference reports how many creator-linked pages it did not match; `list_unmatched` adds
    one line per such page.
    """
    if not 0 <= max_distance <= 256 or not 0 <= min_quality <= 100:
        raise ValueError("distance must be 0-256 and quality 0-100")
    if not max_distance <= loose_max_distance <= 256:
        raise ValueError("loose-max-distance must be between max-distance and 256")
    ids = [reference.pk for reference in references]
    state = incremental.lock_state()
    radius = max(max_distance, loose_max_distance)
    scan_settings = incremental.scan_settings(radius=radius, min_quality=min_quality)
    full = full or state.fingerprint != incremental.fingerprint(scan_settings)
    # Take the shared-frames lock before the snapshot, so no import can flip flags in between.
    lock_shared_frames()
    # Every reference hash, read once and shared by the scan, shared flags and aggregation (RE3).
    hashes = load_reference_hashes()
    scan = incremental.refresh_candidates(
        state, radius=radius, min_quality=min_quality, full=full, hashes=hashes
    )
    candidates = ReferenceCandidate.objects.all()
    mark_shared_frames(
        max_distance=max_distance,
        min_quality=min_quality,
        page_frames=FrameHash.objects.filter(
            pk__in=candidates.filter(distance__lte=max_distance).values("frame_id")
        ),
        hashes=hashes,
    )
    # Serialize reruns for overlapping references; status is never part of an update.
    selected = list(Reference.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    chosen = hashes.eligible(min_quality) & hashes.of_references(ids)
    collections = set(
        Reference.objects.filter(pk__in=ids, kind=Reference.Kind.PHOTOS).values_list(
            "pk", flat=True
        )
    )
    page_frames = load_page_frames(
        min_quality,
        FrameHash.objects.filter(pk__in=candidates.filter(reference_id__in=ids).values("frame_id")),
    )
    evidence, _ = aggregate_hash_matches(
        hashes.reference_id[chosen],
        hashes.timestamp_s[chosen],
        hashes.shared[chosen],
        hashes.words[chosen],
        page_frames,
        max_distance=max_distance,
        loose_max_distance=loose_max_distance,
        unordered_references=collections,
        grid_hits=grid_evidence(ids, page_frames, radius, min_quality),
    )
    totals = page_frame_counts({page for _, page in evidence}, min_quality)
    related = related_to_strong(evidence, ids)
    for (reference_id, page_id), entry in evidence.items():
        if page_id in related.get(reference_id, set()):
            entry.resolve(corroborated=True)
    evidence = {key: entry for key, entry in evidence.items() if entry.matched_frames}
    clusters = dict(
        ClusterMember.objects.exclude(status="rejected").values_list("page_id", "cluster_id")
    )
    raw = dict(
        Page.objects.filter(pk__in={page for _, page in evidence}).values_list(
            "pk", "raw_extraction"
        )
    )
    thresholds = current_thresholds()
    output = []
    # Pages whose copy extent may have changed: every page this run matched, and every page that
    # lost a proposal (a compilation there may now be one original fewer).
    extent_pages = {page for _, page in evidence}
    for reference in selected:
        pages = set()
        strengths: Counter[str] = Counter()
        bands: Counter[str] = Counter()
        rows = ReferenceMatch.objects.filter(reference=reference)
        # An auto decision a human changed or reset is a human decision from now on.
        rows.filter(decided_by="auto").exclude(status="confirmed").update(decided_by="human")
        rows.filter(decided_by="").exclude(status="proposed").update(decided_by="human")
        for (reference_id, page_id), entry in sorted(evidence.items()):
            if reference_id != reference.pk:
                continue
            pages.add(page_id)
            strengths[entry.strength] += 1
            distance, timestamp = entry.best_evidence
            timeline = entry.timeline or verify_timeline([])
            # A photo collection is judged on independent images, never on a timeline it cannot
            # have; `images` stays None for a video so the video rule is untouched (ADR 0020).
            images = entry.collection_images if reference.pk in collections else None
            confidence = score_confidence(
                ConfidenceInputs(
                    evidence_frames=entry.evidence_frames,
                    best_distance=distance,
                    loose_max_distance=loose_max_distance,
                    timeline_score=timeline.score,
                    common_frames=entry.common_frames,
                    page_duration_s=page_duration(raw.get(page_id)),
                    reference_duration_s=reference.duration_s,
                    corroborated=entry.corroborated,
                    collection_images=images,
                )
            )
            band = band_for(
                confidence.value, entry.evidence_frames, thresholds, collection_images=images
            )
            bands[band] += 1
            row, _ = ReferenceMatch.objects.update_or_create(
                reference=reference,
                page_id=page_id,
                defaults={
                    "best_distance": distance,
                    "best_reference_timestamp_s": timestamp,
                    "matched_frames": entry.matched_frames,
                    "evidence_frames": entry.evidence_frames,
                    "loose_frames": entry.loose_frames,
                    "common_frames": entry.common_frames,
                    "matched_seconds": entry.matched_seconds,
                    "shared_seconds": entry.shared_seconds,
                    "common_seconds": entry.common_seconds,
                    "timeline_score": timeline.score,
                    "consistent_frames": timeline.consistent_frames,
                    "timeline_reason": timeline.reason,
                    "confidence": confidence.value,
                    "band": band,
                    "confidence_signals": {
                        **confidence.components,
                        "corroborated": entry.corroborated,
                        **({} if images is None else {"collection_images": images}),
                    },
                    "strength": entry.strength,
                    "copy_span_s": matched_span(timeline.chain_seconds, reference.frame_fps),
                    "page_frames": totals[page_id],
                    "cluster_id": clusters.get(page_id),
                    # A PDQ match takes over a row another signal proposed: this is how an
                    # embedding (ADR 0037) or metadata (ADR 0040) candidate is promoted to a real
                    # match, and the only way either one ever becomes evidence without a human.
                    "candidate_source": ReferenceMatch.CandidateSource.PDQ,
                },
            )
            if band == HIGH and auto_confirm_high():
                ReferenceMatch.objects.filter(pk=row.pk, status="proposed", decided_by="").update(
                    status="confirmed", decided_by="auto", decided_at=timezone.now()
                )
        # Candidate rows are owned by the signal that proposed them - today only the embedding's
        # (`matching.embedding_candidates`, ADR 0037), which refreshes and removes its own; this
        # run knows nothing about them. The third signal writes no rows at all since the ADR 0040
        # amendment: it aims browser captures, and this is where their evidence arrives.
        stale = (
            ReferenceMatch.objects.filter(reference=reference, status="proposed")
            .exclude(page_id__in=pages)
            .exclude(candidate_source__in=ReferenceMatch.CANDIDATE_ONLY_SOURCES)
        )
        extent_pages.update(stale.values_list("page_id", flat=True))
        stale.delete()
        propagate_cluster_leaks(
            reference,
            {
                pid
                for (rid, pid), entry in evidence.items()
                if rid == reference.pk and entry.strength == "strong"
            },
        )
        # Recall helper: pages linked to any performer of the original, not only the primary one.
        missing = (
            Page.objects.filter(creators__in=creator_ids(reference), active=True)
            .exclude(pk__in=pages)
            .distinct()
            .order_by("pk")
        )
        cluster_count = len({clusters[p] for p in pages if p in clusters})
        output.append(
            f"Reference {reference.pk} {reference.title}: {len(pages)} pages matched "
            f"({strengths['strong']} strong, {strengths['weak']} weak), "
            f"{cluster_count} clusters matched; {missing.count()} creator-linked pages NOT matched"
            f"; bands: {bands[HIGH]} high, {bands[REVIEW]} review, {bands[LOW]} low"
        )
        # One line per unmatched page is ~15k lines per scheduled run on the live index; only
        # the count by default, the list on request (`match_references -v 2`).
        if list_unmatched:
            output.extend(f"  NOT matched: {page.pk} {page.title} {page.url}" for page in missing)
    refresh_copy_extents(extent_pages)
    # The candidate cache now covers exactly these scan settings.
    state.settings = scan_settings
    state.fingerprint = incremental.fingerprint(scan_settings)
    state.save()
    output.append(scan.line())
    return output


def refresh_copy_extents(page_ids: Collection[int]) -> int:
    """Re-derive the copy extent (issue #62) of every match on these pages, and of every match an
    older classifier version wrote; returns how many rows changed.

    Reads only stored rows: the span comes from the matching run, the rest from the page's
    duration and the other originals strongly matched on the same page, so a subset run
    (`--reference`) still updates the other originals' rows on the pages it touched. Never changes
    a status, a band or which rows exist — an excerpt is a match (ADR 0015 amendment 2026-09-17).
    `updated_at` is left alone: it is not a decision.
    """
    stale = ReferenceMatch.objects.exclude(copy_extent_version=CLASSIFIER_VERSION).values_list(
        "page_id", flat=True
    )
    pages = set(page_ids) | set(stale)
    if not pages:
        return 0
    rows = list(
        ReferenceMatch.objects.filter(page_id__in=pages).select_related("reference").order_by("pk")
    )
    # Distinct video files strongly matched per page; a pipeline twin is the same original.
    files: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        if (
            row.status != ReferenceMatch.Status.REJECTED
            and row.strength == ReferenceMatch.Strength.STRONG
            and row.reference.kind == Reference.Kind.VIDEO
        ):
            files[row.page_id].add(row.reference.sha256)
    durations = {
        pk: page_duration(raw)
        for pk, raw in Page.objects.filter(pk__in=pages).values_list("pk", "raw_extraction")
    }
    changed = []
    for row in rows:
        other_originals = len(files[row.page_id] - {row.reference.sha256})
        result = classify(
            ExtentInputs(
                collection=row.reference.kind == Reference.Kind.PHOTOS,
                reference_duration_s=row.reference.duration_s,
                page_duration_s=durations.get(row.page_id),
                span_s=row.copy_span_s,
                timeline_score=row.timeline_score,
                consistent_frames=row.consistent_frames,
                other_originals=other_originals,
            )
        )
        values = (
            result.extent.value,
            result.reason.value,
            result.coverage,
            min(other_originals, 32767),
            CLASSIFIER_VERSION,
        )
        if values != (
            row.copy_extent,
            row.copy_extent_reason,
            row.copy_coverage,
            row.copy_other_originals,
            row.copy_extent_version,
        ):
            (
                row.copy_extent,
                row.copy_extent_reason,
                row.copy_coverage,
                row.copy_other_originals,
                row.copy_extent_version,
            ) = values
            changed.append(row)
    ReferenceMatch.objects.bulk_update(
        changed,
        (
            "copy_extent",
            "copy_extent_reason",
            "copy_coverage",
            "copy_other_originals",
            "copy_extent_version",
        ),
        batch_size=1000,
    )
    return len(changed)


def reference_report(references: Iterable[Reference]) -> list[str]:
    output: list[str] = []
    total: Counter[str] = Counter()
    all_pages: set[int] = set()
    all_touched_pages: set[int] = set()
    memberships = list(
        ClusterMember.objects.exclude(status="rejected").values_list("cluster_id", "page_id")
    )

    def cluster_lines(pages: set[int], touched_pages: set[int]) -> list[str]:
        touched = {cluster for cluster, page in memberships if page in touched_pages}
        return [
            f"  Cluster {cluster}: {sum(c == cluster and p in pages for c, p in memberships)}/"
            f"{sum(c == cluster for c, _ in memberships)} member pages matched"
            for cluster in sorted(touched)
        ]

    def summary(counts: Counter[str]) -> str:
        return (
            f"{counts['confirmed']} confirmed, {counts['rejected']} rejected (false matches), "
            f"{counts['proposed']} proposed (unreviewed)"
        )

    def strength_lines(counts: Counter[tuple[str, str]]) -> list[str]:
        lines = []
        for strength in ("strong", "weak"):
            split = Counter({status: n for (s, status), n in counts.items() if s == strength})
            lines.append(f"  {strength}: {summary(split)}")
        return lines

    by_strength: Counter[tuple[str, str]] = Counter()
    # Which recipe produced each original's hashes. Two rows on different pipelines are two
    # sampling grids, never one original counted twice (issue #70, ADR 0022).
    pipelines: Counter[str] = Counter()
    # The rows of one file, so the report can say which of its grids finds pages the other misses
    # (issue #73): sha256 -> [(reference id, pipeline tag, matched page ids)].
    grids: dict[str, list[tuple[int, str, set[int]]]] = defaultdict(list)
    for reference in references:
        matches = list(reference.matches.all())
        counts = Counter(match.status for match in matches)
        strengths = Counter((match.strength, match.status) for match in matches)
        total.update(counts)
        by_strength.update(strengths)
        pages = {match.page_id for match in matches if match.status != "rejected"}
        touched_pages = {match.page_id for match in matches}
        all_touched_pages.update(touched_pages)
        all_pages.update(pages)
        pipelines[reference.pipeline] += 1
        grids[reference.sha256].append((reference.pk, reference.pipeline, pages))
        output.append(f"Reference {reference.pk} {reference.title}: {summary(counts)}")
        output.append(f"  pipeline: {pipeline_label(reference.pipeline)}")
        output.extend(strength_lines(strengths))
        output.extend(cluster_lines(pages, touched_pages))
    output.append(f"Total: {summary(total)}")
    output.extend(strength_lines(by_strength))
    output.extend(cluster_lines(all_pages, all_touched_pages))
    for tag, count in sorted(pipelines.items()):
        output.append(f"  pipeline {pipeline_label(tag)}: {count} original(s)")
    output.extend(grid_lines(grids))
    return output


def grid_lines(grids: Mapping[str, Sequence[tuple[int, str, set[int]]]]) -> list[str]:
    """The retirement question for a file stored on more than one sampling grid (issue #73).

    Two rows of one original are two sampling *phases* half a second apart, and which of them a
    page's single poster lands on is decided per page. So a row is retirable exactly when the pages
    it matches are also matched by a sibling row, and this says so per file rather than leaving it
    to a database probe. `docs/matching/issue-73-v1-reference-recall.md` measures why a denser grid
    does not make the question go away: halving the spacing only doubles the chance that an
    arbitrary picture lands within the match threshold of some sample, so a row's phase is worth
    keeping until its file has a denser row that has been matched.
    """
    multi = {digest: rows for digest, rows in grids.items() if len(rows) > 1}
    if not multi:
        return []
    lines = [
        "Files on several sampling grids (issue #73, ADR 0022): a row with pages of its own is why "
        "its file keeps both grids; a row with none of its own can be retired."
    ]
    for digest, rows in sorted(multi.items()):
        union: set[int] = set().union(*(pages for _, _, pages in rows))
        lines.append(f"  file {digest[:12]}: {len(rows)} grids, {len(union)} page(s) in the union")
        for reference_id, tag, pages in sorted(rows):
            own = sorted(
                pages - set().union(*(other for pk, _, other in rows if pk != reference_id))
            )
            unique = (
                f"{len(own)} of its own: {', '.join(str(page) for page in own)}"
                if own
                else "none of its own"
            )
            lines.append(f"    reference {reference_id} on {pipeline_label(tag)}")
            lines.append(f"      {len(pages)} page(s) matched, {unique}")
    return lines
