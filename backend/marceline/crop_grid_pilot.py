"""The reference-side crop grid pilot, exactly as ADR 0039 pre-registered it.

Three stages, and an exit criterion frozen in code so nobody can move it after seeing the numbers:

1. **select** — which originals to re-fingerprint, and what that costs. Writes nothing.
2. **plan** — which originals are ready to be judged, and on what. Writes nothing.
3. **measure** — the three pre-registered numbers and the verdict.

**Eight originals, not two and not all 44** (operator, 2026-09-18: *"We start low key. Let's say 8
originals."*). ADR 0039 pre-registered the criteria on two files; running them over eight is
strictly more evidence for the same frozen bars, so nothing about the criteria changes — only how
many files each one is computed over. `select` sizes the storage, the index and the re-fingerprint
for those eight and states the extrapolation to the whole library separately, because the full
rollout is a later decision.

**There is no fetch stage, and that is the whole point of this signal.** ADR 0037's pilot had to
re-fetch about 3,000 media before it could measure anything, because page pixels are discarded
after hashing. The crop grid is retroactive: every one of the 203,315 page hashes already stored is
compared against the new lattice with **no request, no re-hash and no budget**. This module makes
no network call of any kind.

**How the comparison works, with nothing snapshotted.** A gridded original imports *alongside* the
plain one under its own pipeline tag (ADR 0022), so for a pilot file both live in the database at
once: the plain reference is "today" and the gridded reference is "with the grid", measured on the
same page frames in the same run. Nothing has to be remembered from an earlier run and nothing has
to be rolled back.

The criterion, from ADR 0039's "Pilot, pre-registered" as its amendment of 2026-09-18 states it:

- **1, recall.** Of the near-miss pages of the pilot files — the ones *above* the matcher's strict
  threshold and up to 72 bits from the plain reference today — **at least half** come inside that
  threshold of the gridded one.
- **2, no harm.** No page the plain reference matches today moves outward by more than 4 bits, and
  no page that is confirmed **and matched today** becomes unmatched.
- **3, precision.** At most **one in five** of the *new* proposed matches is rejected by a human.

**The bars did not move**: half, four bits, one in five are exactly what was pre-registered. What
the amendment changed is the yardstick both halves are measured with — `STRICT_MAX_DISTANCE`,
read from `matching.references` rather than restated here — and the exclusion of pages no signal
can reach. Both changes make criterion 1 *stricter* (a page the matcher already matches is no
longer counted as recovered) and remove a false `WITHDRAW` path from criterion 2.

Fail 1 and the reference-side case is unproven from our own data — **say so and stop**; §6.1's
cross-site result stands on its own for clustering. Fail 2 or 3 and the grid is withdrawn. Anything
the criteria do not cover — no gridded original, no near-miss page, no human verdict on the new
proposals yet — is **not** a pass: `verdict()` says so instead of declaring success.

Adoption is not just the setting. A minimum over 256 correlated hashes shifts the whole distance
distribution down 10-16 bits (§4.4), so **`calibrate_confidence` must be re-run** and
`matching/confidence.py`'s distance-to-score mapping re-derived. The ADOPT verdict says it too.

Lives in the project package like `marceline.embedding_pilot`, because one run needs `matching`,
`index` and the ownership of the originals together.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

import numpy as np

from index.models import FrameHash
from matching import crop_grid, crop_index
from matching.hamming import HashIndex, to_words
from matching.models import Reference, ReferenceFrameHash, ReferenceLeak, ReferenceMatch
from matching.pdq import MIN_QUALITY
from matching.references import STRICT_MAX_DISTANCE

# --- the pre-registered exit criterion (ADR 0039). Do not tune these to a result. ---
#: Share of a pilot file's near-miss pages that must come inside `RECOVERY_DISTANCE`.
MIN_NEAR_MISS_RECOVERED = 0.50
#: Bits a page the plain reference already matches may move *outward*.
MAX_OUTWARD_DRIFT = 4
#: Share of the new proposals a human may reject.
MAX_NEW_REJECTED = 0.20

#: **Both yardsticks are the matcher's own strict threshold, read from it — never a literal here**
#: (ADR 0039 amendment 2026-09-18). The pilot was written while `STRICT_MAX_DISTANCE` was 31 and
#: criterion 1 was stated at 36; the ADR 0015 amendment then moved the matcher to 36 and the two
#: halves of this file drifted apart, so the first run measured criterion 2 against a threshold the
#: matcher had not used for a day. `matching/tests/test_browser_parity_thresholds.py` is the same
#: fix for the same cause; `marceline/tests/test_crop_grid_pilot.py` guards this one.
RECOVERY_DISTANCE = STRICT_MAX_DISTANCE
#: What the matcher accepts today, for the "no harm" and "confirmed stays matched" halves.
MATCH_DISTANCE = STRICT_MAX_DISTANCE
#: The near-miss band of `reference-crop-grid.md` §1.3: close enough to be real, too far for the
#: matcher to take. It starts **above** what the matcher already accepts — a page the matcher
#: matches today is not a near miss and cannot be "recovered" — and ends at 72 because above that
#: the population becomes the whole index.
NEAR_MISS_MIN = STRICT_MAX_DISTANCE + 1
NEAR_MISS_MAX = 72
#: How wide the before/after scan looks. The band's own end: further is not a near miss.
SCAN_RADIUS = NEAR_MISS_MAX

# --- sizing, from measurements rather than from the ADR's estimates ---
#: Originals the operator chose to pilot (2026-09-18). ADR 0039 pre-registered two; eight is more
#: evidence for the same bars, so the criteria are unchanged and only the population grows.
PILOT_ORIGINALS = 8
#: Resident set per corner hash in the matcher's index, measured: 95.9 MB over a baseline for
#: 1,228,720 hashes (FAISS codes, `HashIndex`'s exact-rerank copy and its id array).
RSS_BYTES_PER_STAGE1_HASH = 82
#: CPU per moment for a 256-crop grid, measured with the shipped code on the development VPS:
#: 2.43 s from a 1280x534 master, 5.54 s from 1920x1080. ADR 0039's 1.9 s was 720p on the check
#: runner; real originals are mostly 1080p, so the range is what an estimate has to carry.
GRID_SECONDS_PER_MOMENT = (2.43, 5.54)
#: Cores on the operator's Mac, for turning core-hours into an evening.
CORES = 8


@dataclass(frozen=True)
class PilotFile:
    """One original that can be judged: the same file, plain and gridded, side by side."""

    sha256: str
    plain: list[int]
    gridded: int
    moments: int
    grids: int
    #: The recipe each side was produced with, because it is a confound of every number below.
    grid_fps: float | None = None
    grid_pipeline: str = ""
    plain_fps: tuple[float | None, ...] = ()
    plain_pipelines: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Both sides present and the gridded side really carries blobs."""
        return bool(self.plain) and self.grids > 0

    @property
    def fps_differs(self) -> bool:
        """The avoidable confound: the two sides were sampled at different rates.

        The pipeline tag always differs — a gridded reference is a different recipe by
        construction (ADR 0022) — but the sampling rate need not, and when it does the gain
        cannot be attributed to the lattice alone.
        """
        return any(value != self.grid_fps for value in self.plain_fps)


def pilot_files(sha256s: Collection[str] | None = None) -> list[PilotFile]:
    """Files that carry both a plain and a gridded reference, newest gridded row per file.

    ADR 0039 names references 1/40 and 8/76 — two files, each with an old row and a new one. This
    finds that shape generically rather than pinning the ids, because a re-fingerprint gives the
    new row whatever id it gets.
    """
    rows = Reference.objects.all()
    if sha256s is not None:
        rows = rows.filter(sha256__in=sorted(sha256s))
    by_file: dict[str, list[Reference]] = {}
    for reference in rows.order_by("pk"):
        by_file.setdefault(reference.sha256, []).append(reference)
    files: list[PilotFile] = []
    for digest, references in sorted(by_file.items()):
        gridded = [r for r in references if r.pipeline in crop_grid_pipelines()]
        if not gridded:
            continue
        newest = gridded[-1]
        moments = ReferenceFrameHash.objects.filter(reference=newest, dihedral=0)
        plain = [r for r in references if r.pk != newest.pk]
        files.append(
            PilotFile(
                sha256=digest,
                plain=[r.pk for r in plain],
                gridded=newest.pk,
                moments=moments.count(),
                grids=moments.filter(crop_grid__isnull=False).count(),
                grid_fps=newest.frame_fps,
                grid_pipeline=newest.pipeline,
                plain_fps=tuple(r.frame_fps for r in plain),
                plain_pipelines=tuple(r.pipeline for r in plain),
            )
        )
    return files


def crop_grid_pipelines() -> frozenset[str]:
    from matching.fingerprint import CROP_GRID_PIPELINES

    return CROP_GRID_PIPELINES


def _page_frames(min_quality: int) -> tuple[list[int], list[bytes]]:
    rows = list(
        FrameHash.objects.filter(quality__gte=min_quality, media__page__active=True)
        .order_by("pk")
        .values_list("media__page_id", "pdq")
    )
    return [int(row[0]) for row in rows], [bytes(row[1]) for row in rows]


def pages_with_frames(page_ids: Collection[int], min_quality: int = MIN_QUALITY) -> set[int]:
    """Of these pages, the ones the scan can see at all — the same filter as `_page_frames`.

    A page with no usable frame hash carries no signal for *either* side: it is unmatched today
    and it stays unmatched with the grid, whatever the lattice does. Counting it as a loss is what
    made the first run (2026-09-18) report 7 confirmed pages lost when the real number was 0. It
    is reported on a line of its own and excluded from every numerator and denominator.
    """
    return {
        int(page)
        for page in FrameHash.objects.filter(
            media__page_id__in=sorted(page_ids),
            quality__gte=min_quality,
            media__page__active=True,
        ).values_list("media__page_id", flat=True)
    }


def plain_distances(
    reference_ids: Sequence[int], min_quality: int = MIN_QUALITY, radius: int = SCAN_RADIUS
) -> dict[int, int]:
    """Best distance from each active page to any hash of these references — today's number."""
    hashes = list(
        ReferenceFrameHash.objects.filter(
            reference_id__in=sorted(reference_ids), quality__gte=min_quality
        )
        .order_by("pk")
        .values_list("pk", "pdq")
    )
    pages, values = _page_frames(min_quality)
    if not hashes or not pages:
        return {}
    index = HashIndex.from_pairs([(pk, bytes(pdq)) for pk, pdq in hashes])
    positions, _, distances = index.pairs_within(values, radius)
    best: dict[int, int] = {}
    for position, distance in zip(positions.tolist(), distances.tolist(), strict=True):
        page = pages[position]
        if distance < best.get(page, radius + 1):
            best[page] = int(distance)
    return best


def grid_distances(
    reference_id: int, min_quality: int = MIN_QUALITY, radius: int = SCAN_RADIUS
) -> dict[int, int]:
    """Best distance from each active page to the gridded reference, both stages, no requests.

    The uncropped hashes are in the blob as entry 0, so this is the whole of "with the grid": the
    plain scan of that reference plus the crop-grid minimum, whichever is smaller.
    """
    best = plain_distances([reference_id], min_quality, radius)
    pages, values = _page_frames(min_quality)
    stage1 = crop_index.load_stage1([reference_id], min_quality)
    if not len(stage1) or not pages:
        return best
    for hit in crop_index.search(stage1, to_words(values), radius=radius):
        page = pages[hit.position]
        if hit.distance < best.get(page, radius + 1):
            best[page] = hit.distance
    return best


@dataclass(frozen=True)
class Cost:
    """What a set of moments costs to produce, to store and to search. Measured, not estimated."""

    originals: int
    moments: int

    @property
    def blob_bytes(self) -> int:
        return self.moments * crop_grid.blob_bytes()

    @property
    def stage1_hashes(self) -> int:
        return self.moments * crop_grid.STAGE1_CROPS

    @property
    def rss_bytes(self) -> int:
        return self.stage1_hashes * RSS_BYTES_PER_STAGE1_HASH

    @property
    def core_hours(self) -> tuple[float, float]:
        low, high = GRID_SECONDS_PER_MOMENT
        return self.moments * low / 3600, self.moments * high / 3600

    def lines(self, label: str) -> list[str]:
        low, high = self.core_hours
        return [
            f"{label}: {self.originals} originals, {self.moments} moments",
            f"  blobs        {self.blob_bytes / 2**20:8.0f} MB",
            f"  matcher RSS  {self.rss_bytes / 2**20:8.0f} MB ({self.stage1_hashes} corner hashes)",
            f"  re-fingerprint on the Mac {low:.1f}-{high:.1f} core-hours "
            f"= {low / CORES:.1f}-{high / CORES:.1f} h on {CORES} cores",
        ]


@dataclass(frozen=True)
class Candidate:
    """An original that could be re-fingerprinted, and why it would be worth it."""

    reference_id: int
    sha256: str
    moments: int
    confirmed_pages: int
    near_miss_pages: int
    #: Only ever True when the ranking was asked to include files that already carry a grid.
    gridded: bool = False

    @property
    def score(self) -> int:
        """Confirmed copies plus near-miss pages: what the pilot's criteria are computed over.

        Criterion 2 is about pages the original already matches and criterion 1 about its near
        misses, so an original with neither can neither pass nor fail — it only costs CPU. Both
        columns are printed, so the operator can overrule the sum.
        """
        return self.confirmed_pages + self.near_miss_pages


def best_per_reference(
    min_quality: int = MIN_QUALITY, radius: int = SCAN_RADIUS
) -> dict[int, dict[int, int]]:
    """reference id -> {page id: best distance <= radius}, in one scan over the whole library."""
    rows = list(
        ReferenceFrameHash.objects.filter(quality__gte=min_quality)
        .order_by("pk")
        .values_list("reference_id", "pdq")
    )
    pages, values = _page_frames(min_quality)
    if not rows or not pages:
        return {}
    index = HashIndex.from_words(
        np.asarray([int(row[0]) for row in rows], dtype=np.int64),
        to_words([bytes(row[1]) for row in rows]),
    )
    positions, reference_ids, distances = index.pairs_within(to_words(values), radius)
    best: dict[int, dict[int, int]] = {}
    for position, reference_id, distance in zip(
        positions.tolist(), reference_ids.tolist(), distances.tolist(), strict=True
    ):
        page = pages[position]
        found = best.setdefault(int(reference_id), {})
        if distance < found.get(page, radius + 1):
            found[page] = int(distance)
    return best


def candidates(include_gridded: bool = False) -> list[Candidate]:
    """Every original without a crop grid yet, ranked by how much the pilot could learn from it.

    With `include_gridded`, files that already carry a grid stay in the ranking and are marked.
    That is not a selection — `select` never offers a file a second time — but the only way to ask
    "of the originals this ranking puts first, which ones is the measurement still missing?", which
    the first run needed and did not have: it ran without the top-ranked file of all (ADR 0039
    amendment 2026-09-18).

    **One entry per file, not per row.** The same original routinely has several references — a
    superseded grid beside the current one, a 2 fps re-fingerprint beside a 1 fps one (ADR 0022) —
    and re-fingerprinting the file produces one new row for all of them. Counting rows would have
    spent half of the operator's eight originals on two files: on the live index, ADR 0039's own
    pilot pair is references 1/40 and 8/76, which is two files and four rows. So the counts are
    merged across a file's rows and `reference_id` names the newest one, which is the row the new
    grid will be compared against.
    """
    gridded = {
        digest
        for digest in Reference.objects.filter(
            pk__in=crop_index.grid_rows().values("reference_id")
        ).values_list("sha256", flat=True)
    }
    distances = best_per_reference()
    by_file: dict[str, list[Reference]] = {}
    rows_qs = Reference.objects.order_by("pk")
    if not include_gridded:
        rows_qs = rows_qs.exclude(sha256__in=sorted(gridded))
    for reference in rows_qs:
        by_file.setdefault(reference.sha256, []).append(reference)
    rows = []
    for digest, references in by_file.items():
        ids = [reference.pk for reference in references]
        pages: dict[int, int] = {}
        for reference_id in ids:
            for page, value in distances.get(reference_id, {}).items():
                if value < pages.get(page, SCAN_RADIUS + 1):
                    pages[page] = value
        rows.append(
            Candidate(
                reference_id=ids[-1],
                sha256=digest,
                moments=ReferenceFrameHash.objects.filter(reference_id=ids[-1], dihedral=0).count(),
                confirmed_pages=len(_confirmed_pages(ids)),
                near_miss_pages=sum(
                    1 for value in pages.values() if NEAR_MISS_MIN <= value <= NEAR_MISS_MAX
                ),
                gridded=digest in gridded,
            )
        )
    return sorted(rows, key=lambda row: (-row.score, -row.confirmed_pages, row.reference_id))


def ranking_coverage(limit: int = PILOT_ORIGINALS) -> tuple[list[Candidate], list[Candidate]]:
    """The top `limit` files of `select`'s own ranking, and which of them carry no grid yet.

    `select` hides files that are already gridded, so after a partial import it can no longer say
    which of its *original* recommendations the measurement is still missing. This can.
    """
    ranked = candidates(include_gridded=True)[:limit]
    return ranked, [item for item in ranked if not item.gridded]


def recommend(limit: int = PILOT_ORIGINALS) -> tuple[list[Candidate], Cost, Cost]:
    """The `limit` originals worth piloting, what they cost, and what the whole library would.

    Read-only, no requests. Reference ids, digests and counts only — never a title or a URL.
    """
    ranked = candidates()
    chosen = ranked[:limit]
    whole = Cost(
        originals=Reference.objects.count(),
        moments=ReferenceFrameHash.objects.filter(dihedral=0).count(),
    )
    return chosen, Cost(len(chosen), sum(item.moments for item in chosen)), whole


def selection_lines(limit: int = PILOT_ORIGINALS) -> list[str]:
    chosen, pilot, whole = recommend(limit)
    out = [
        f"Recommended {len(chosen)} originals to re-fingerprint with a crop grid, ranked by "
        "confirmed copies + near-miss pages. One row per FILE: re-fingerprinting it replaces "
        "nothing and produces one new reference beside all of that file's existing rows.",
        "  reference  sha256        moments  confirmed  near-miss",
    ]
    out.extend(
        f"  {item.reference_id:>9}  {item.sha256[:12]}  {item.moments:>7}  "
        f"{item.confirmed_pages:>9}  {item.near_miss_pages:>9}"
        for item in chosen
    )
    if not chosen:
        out.append("  (every original already carries a grid, or there are none)")
    out.append("")
    out.extend(pilot.lines("Pilot"))
    out.append("")
    out.extend(whole.lines("Whole library, for the later rollout decision"))
    out.append("")
    out.extend(
        [
            "On the operator's Mac, for those reference ids:",
            "  uv run client/fingerprint.py <their files> --crop-grid --out fingerprints-grid",
            "  rsync -av fingerprints-grid/ devbox:marceline-fingerprints-grid/",
            "and on the server:",
            "  uv run python manage.py import_fingerprints ~/marceline-fingerprints-grid/",
            "They import ALONGSIDE the existing references (ADR 0022); nothing stored changes.",
            "Nothing was requested and nothing was written.",
        ]
    )
    return out


def _recipe(fps: float | None, pipeline: str) -> str:
    return f"{'n/a' if fps is None else f'{fps:g}'} fps, {pipeline}"


def recipe_lines(item: PilotFile) -> list[str]:
    """How each side of one pilot file was produced — a confound of every number it feeds.

    The gridded reference is always a different recipe (ADR 0022), so the lattice is never the
    only thing that changed; when the sampling rate differs too, it is even less alone. The first
    run compared 2 fps master/PIL grids against 1 fps rgb512 plain references and could not
    attribute its gain (ADR 0039 amendment 2026-09-18).
    """
    out = [
        f"grid:  reference {item.gridded}, {_recipe(item.grid_fps, item.grid_pipeline)}",
        f"plain: reference {', '.join(str(pk) for pk in item.plain) or 'none'}, "
        + " / ".join(
            sorted(
                {
                    _recipe(fps, pipeline)
                    for fps, pipeline in zip(item.plain_fps, item.plain_pipelines, strict=True)
                }
            )
        ),
    ]
    if item.fps_differs:
        out.append(
            "! the two sides were sampled at different rates, so a gain cannot be attributed "
            "to the lattice alone"
        )
    return out


def confound_lines(files: Sequence[PilotFile], limit: int = PILOT_ORIGINALS) -> list[str]:
    """What a reader must be told beside the three numbers, whatever they say.

    Two things the first run exposed: the two sides of a pilot file are not the same recipe, and
    the files that were measured are not necessarily the files the ranking asked for.
    """
    out = ["Confounds — state these with any number above (ADR 0039 amendment 2026-09-18):"]
    for item in files:
        out.append(f"  {item.sha256[:12]}")
        out.extend(f"    {line}" for line in recipe_lines(item))
    out.append(
        "  The gridded side is a different recipe in every case (master frames, Pillow's scaler), "
        "so even at an equal sampling rate the lattice is not the only thing that changed."
    )
    ranked, missing = ranking_coverage(limit)
    if ranked:
        names = ", ".join(str(item.reference_id) for item in missing)
        out.append(
            f"  Of the {len(ranked)} originals `--stage select` ranks highest, {len(missing)} "
            f"carry no grid yet" + (f": references {names}" if names else "")
        )
    return out


@dataclass(frozen=True)
class Plan:
    """What can be judged, and what the judging would cost. Reads only; requests nothing."""

    files: list[PilotFile]
    near_miss: dict[str, int]
    stage1_hashes: int
    library_moments: int
    library_grids: int

    def ready(self) -> list[PilotFile]:
        return [item for item in self.files if item.ready]

    def lines(self) -> list[str]:
        out = [
            f"Library: {self.library_grids} of {self.library_moments} reference moments carry a "
            f"crop grid.",
            f"Pilot files (the same original, plain and gridded, side by side): {len(self.files)}",
        ]
        for item in self.files:
            near = self.near_miss.get(item.sha256, 0)
            state = "" if item.ready else "  ! no plain reference to compare against"
            out.append(
                f"  {item.sha256[:12]} reference {item.gridded} (plain: "
                f"{', '.join(str(pk) for pk in item.plain) or 'none'}): "
                f"{item.grids} grids on {item.moments} moments, {near} near-miss pages "
                f"({NEAR_MISS_MIN}-{NEAR_MISS_MAX} bits today){state}"
            )
            out.extend(f"    {line}" for line in recipe_lines(item))
        out.append(
            f"Stage 1 would add {self.stage1_hashes} corner hashes to the matcher's index "
            f"(~{self.stage1_hashes * 96 // 1_228_720} MB of resident set, measured)."
        )
        if not self.files:
            out.append(
                "! no original carries a crop grid yet: re-fingerprint one on the operator's Mac "
                "(`client/fingerprint.py --crop-grid`) and import the file first"
            )
        out.append(
            "Nothing was requested and nothing was written. Unlike ADR 0037's pilot this one "
            "never fetches: the grid is compared against page hashes that are already stored."
        )
        return out


def plan(sha256s: Collection[str] | None = None) -> Plan:
    files = pilot_files(sha256s)
    near: dict[str, int] = {}
    for item in files:
        if item.plain:
            today = plain_distances(item.plain)
            near[item.sha256] = sum(
                1 for value in today.values() if NEAR_MISS_MIN <= value <= NEAR_MISS_MAX
            )
    moments, grids = _library_coverage()
    return Plan(
        files=files,
        near_miss=near,
        stage1_hashes=sum(item.grids for item in files) * crop_grid.STAGE1_CROPS,
        library_moments=moments,
        library_grids=grids,
    )


def _library_coverage() -> tuple[int, int]:
    moments = ReferenceFrameHash.objects.filter(dihedral=0).count()
    return moments, crop_index.coverage()[0]


@dataclass
class Measurement:
    """The three pre-registered numbers, and whether they clear the frozen bar."""

    files: int = 0
    near_miss: int = 0
    near_miss_recovered: int = 0
    matched_today: int = 0
    moved_outward: list[tuple[int, int, int]] = field(default_factory=list)
    #: Confirmed pages the matcher **matches today** — criterion 2's denominator, and the only
    #: population "becomes unmatched" can be about.
    confirmed_today: int = 0
    confirmed_lost: list[int] = field(default_factory=list)
    #: Confirmed pages with no frame hash the scan can see: no signal reaches them either way.
    confirmed_no_hash: int = 0
    #: Confirmed pages that do have hashes but sit beyond the strict threshold already today.
    confirmed_unmatched_today: int = 0
    new_proposals: int = 0
    new_reviewed: int = 0
    new_rejected: int = 0
    confounds: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.near_miss_recovered / self.near_miss if self.near_miss else 0.0

    @property
    def rejection_rate(self) -> float:
        return self.new_rejected / self.new_reviewed if self.new_reviewed else 0.0

    @property
    def recall_passes(self) -> bool:
        # An empty near-miss population cannot pass: there is nothing the criterion measured.
        return bool(self.near_miss) and self.recall >= MIN_NEAR_MISS_RECOVERED

    @property
    def harm_passes(self) -> bool:
        return not self.moved_outward and not self.confirmed_lost

    @property
    def precision_passes(self) -> bool:
        return bool(self.new_reviewed) and self.rejection_rate <= MAX_NEW_REJECTED

    @property
    def decidable(self) -> bool:
        """Everything the three criteria need is present. Silence is never a pass."""
        return bool(self.files) and bool(self.near_miss) and bool(self.new_reviewed)

    def verdict(self) -> str:
        if not self.files:
            return (
                "NOT MEASURED: no original carries both a plain and a gridded reference. "
                "Re-fingerprint the two pilot files with `client/fingerprint.py --crop-grid` and "
                "import them; they land alongside the existing rows."
            )
        if not self.near_miss:
            return (
                "NOT MEASURED: the pilot files have no near-miss pages "
                f"({NEAR_MISS_MIN}-{NEAR_MISS_MAX} bits) for criterion 1 to be about. Pick files "
                "with page evidence, as ADR 0039 did, or say that the population is gone."
            )
        if not self.recall_passes:
            return (
                "UNPROVEN — STOP. Fewer than half of the near-miss pages came inside "
                f"{RECOVERY_DISTANCE} bits, so the reference-side case is not made by our own "
                "data. Say so and stop: the cross-site result of reference-crop-grid.md §6.1 "
                "stands on its own for clustering, and nothing here overrides it. Do not widen "
                "the band, the threshold or the lattice to reach the bar."
            )
        if not self.harm_passes:
            return (
                "WITHDRAW: criterion 2 failed. The grid moved pages the originals already match, "
                "which is the one thing it was not allowed to do. ADR 0039 withdraws it."
            )
        if not self.new_reviewed:
            return (
                "NOT MEASURED: criterion 3 needs human verdicts on the new proposals and there "
                "are none yet. Review them in the moderation queue, then measure again. "
                f"{self.new_proposals} new proposal(s) are waiting."
            )
        if not self.precision_passes:
            return (
                "WITHDRAW: criterion 3 failed. More than one in five of the new proposals was "
                "rejected by a human. ADR 0039 withdraws the grid; it does not raise the bar."
            )
        return (
            "ADOPT: every pre-registered criterion is met. Two things follow, neither optional: "
            "re-fingerprint the rest of the library with `--crop-grid`, and **re-run "
            "`calibrate_confidence`** — a minimum over 256 correlated hashes moves the whole "
            "distance distribution down 10-16 bits, so the distance-to-score mapping of "
            "matching/confidence.py is on the old scale until it is re-derived."
        )

    def lines(self) -> list[str]:
        def mark(passed: bool, measured: bool = True) -> str:
            return ("PASS" if passed else "FAIL") if measured else "NOT MEASURED"

        recall_mark = mark(self.recall_passes, bool(self.near_miss))
        precision_mark = mark(self.precision_passes, bool(self.new_reviewed))

        drift = ", ".join(
            f"page {page} {before}->{after} "
            + (f"still inside {MATCH_DISTANCE}" if after <= MATCH_DISTANCE else "now unmatched")
            for page, before, after in self.moved_outward[:5]
        )
        return [
            f"Pilot files: {self.files}",
            f"1. Near-miss pages ({NEAR_MISS_MIN}-{NEAR_MISS_MAX} bits today) inside "
            f"{RECOVERY_DISTANCE} bits with the grid: {self.near_miss_recovered} of "
            f"{self.near_miss} = {self.recall:.1%} "
            f"(bar: >= {MIN_NEAR_MISS_RECOVERED:.0%}) {recall_mark}",
            f"2. Pages matched today ({self.matched_today}) that moved outward by more than "
            f"{MAX_OUTWARD_DRIFT} bits: {len(self.moved_outward)}"
            + (f" [{drift}]" if drift else "")
            + f"; confirmed pages lost: {len(self.confirmed_lost)} of {self.confirmed_today} "
            f"matched today {mark(self.harm_passes)}",
            f"   no page hash: {self.confirmed_no_hash} — no signal can reach these; confirmed "
            f"but beyond {MATCH_DISTANCE} bits today: {self.confirmed_unmatched_today}. Both are "
            "outside every numerator and denominator above.",
            f"3. New proposals rejected by a human: {self.new_rejected} of {self.new_reviewed} "
            f"reviewed ({self.new_proposals} new in total) = {self.rejection_rate:.1%} "
            f"(bar: <= {MAX_NEW_REJECTED:.0%}) {precision_mark}",
            *self.confounds,
            self.verdict(),
        ]


def measure(sha256s: Collection[str] | None = None) -> Measurement:
    """The pilot's three numbers, read from the plain and gridded rows of each pilot file."""
    result = Measurement()
    files = [item for item in pilot_files(sha256s) if item.ready]
    result.files = len(files)
    for item in files:
        today = plain_distances(item.plain)
        withgrid = grid_distances(item.gridded)
        far = SCAN_RADIUS + 1

        near = [page for page, value in today.items() if NEAR_MISS_MIN <= value <= NEAR_MISS_MAX]
        result.near_miss += len(near)
        result.near_miss_recovered += sum(
            1 for page in near if withgrid.get(page, far) <= RECOVERY_DISTANCE
        )

        matched = [page for page, value in today.items() if value <= MATCH_DISTANCE]
        result.matched_today += len(matched)
        for page in matched:
            after = withgrid.get(page, far)
            if after > today[page] + MAX_OUTWARD_DRIFT:
                result.moved_outward.append((page, today[page], after))

        # Criterion 2 is "no page *becomes* unmatched", so only pages the matcher matches today
        # can be lost. A confirmed page with no usable frame hash carries no signal either way and
        # one already beyond the threshold cannot fall out of it; both are counted on their own
        # line instead of being charged to the grid (ADR 0039 amendment 2026-09-18).
        confirmed = _confirmed_pages(item.plain)
        hashed = pages_with_frames(confirmed)
        matched_and_confirmed = sorted(confirmed & set(matched))
        result.confirmed_no_hash += len(confirmed - hashed)
        result.confirmed_today += len(matched_and_confirmed)
        result.confirmed_unmatched_today += len(confirmed & hashed) - len(matched_and_confirmed)
        result.confirmed_lost.extend(
            page for page in matched_and_confirmed if withgrid.get(page, far) > MATCH_DISTANCE
        )

        rows = ReferenceMatch.objects.filter(reference_id=item.gridded).exclude(page_id__in=matched)
        result.new_proposals += rows.count()
        reviewed = rows.exclude(status=ReferenceMatch.Status.PROPOSED).exclude(decided_by="auto")
        result.new_reviewed += reviewed.count()
        result.new_rejected += reviewed.filter(status=ReferenceMatch.Status.REJECTED).count()
    if files:
        result.confounds = confound_lines(files)
    return result


def _confirmed_pages(reference_ids: Sequence[int]) -> set[int]:
    """Pages a human confirmed for these references, in either ground-truth table."""
    pages: set[int] = set()
    for model in (ReferenceMatch, ReferenceLeak):
        pages.update(
            model.objects.filter(
                reference_id__in=sorted(reference_ids), status="confirmed"
            ).values_list("page_id", flat=True)
        )
    return pages
