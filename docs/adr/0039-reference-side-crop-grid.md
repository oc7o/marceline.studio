# 0039 — A crop grid on the reference side

Status: **accepted, off by default** (2026-09-18; proposed the same day). The operator adopted
the decision and chose a pilot of **eight originals** — *"We start low key. Let's say 8
originals."* — rather than the two this ADR pre-registered or the whole library. The code is built
(issue #80) and everything is behind `MATCHING_REFERENCE_CROP_GRID`, which is **false**; the pilot
decides the rollout. Extends ADR 0010 (creator references), ADR 0015 (frame evidence), ADR 0022
(the sampling grid and pipeline identity) and ADR 0024 (the Hamming index). Measurements:
`docs/matching/reference-crop-grid.md`.

## What shipped (2026-09-18, issue #80)

| Decision | Where |
|---|---|
| The lattice, the blob layout, the writer and the minimum | `matching/crop_grid.py` (pure) |
| One packed `bytea` per moment | `matching.ReferenceFrameHash.crop_grid`, migration `matching/0022` |
| The two-stage search | `matching/crop_index.py` |
| The setting, off | `MATCHING_REFERENCE_CROP_GRID` |
| A grid hit as ordinary evidence | `incremental.scan_grid`, `references.aggregate_hash_matches(grid_hits=)` |
| The new pipeline tag | `fingerprint.CROP_GRID_PIPELINE = ffmpeg-master-pil512-pdq-grid256-v1` |
| Producing grids | `client/fingerprint.py --crop-grid`, format version 5, `import_fingerprints` |
| The pilot | `manage.py crop_grid_pilot --stage select\|plan\|measure` |

**Three numbers came out different from the estimates above, and the estimates are the ones that
were wrong:**

- **Matcher RAM: +96 MB, not +79 MB** (measured over a bare numpy+faiss baseline at 1,228,720
  corner hashes). §8.2 counted two copies of the codes and left out `HashIndex`'s id array (9.4 MB),
  the per-moment side columns and the search buffers. At the 500-original ceiling that is ~1.1 GB
  rather than ~900 MB: the ceiling is nearer than this ADR said, not further.
- **Generation: 2.43 s of CPU per sampled second from a 1280x534 master and 5.54 s from
  1920x1080**, measured with the shipped code. §8.1's 1.9 s was 720p on the check runner. Real
  originals are mostly 1080p, so the whole library is on the order of 100 core-hours —
  **12-15 h on 8 cores, not 5 h**. The eight-original pilot is roughly a fifth of that.
- The JSON transfer is **twice** the 629 MB of blobs, because the format stores hashes as hex.

Two things this ADR asked for were deliberately **not** done: the strict threshold stays at 31
(raising it to 36 is the separate decision of §5 and `reference-crop-grid.md` §10.1), and
`calibrate_confidence` has not been re-run — it must be, on adoption, and the pilot's ADOPT verdict
says so.

> **Numbering.** 0038 is *Index suppression*, taken in a parallel branch.

## Context

Three studies agree on where PDQ breaks, and on why. `docs/matching/black-bars.md` established on 80
real page images that there is no padding in the index; `docs/matching/second-signal.md` §4.6
narrowed the residual to one thing — an **asymmetric 1–3 % re-frame**, which costs PDQ 44 bits where
a regrade, a sharpen and a re-encode cost 0–2 — and concluded that no hash-family answer exists,
because a *symmetric centre-crop* second hash recovers none of it. ADR 0037 was written on that
conclusion.

The conclusion was right about the cause and wrong about the remedy, and the reason is a design
choice nobody questioned: the earlier crop search was **symmetric** (it cannot express an
asymmetric trim at all), **one point** (the centre 75 %) and, where it was a search, on an **8 %
grid** — four times too coarse for a 1–3 % trim. `docs/matching/reference-crop-grid.md` redoes it
with a four-edge lattice and finds:

- a **256-crop lattice** (step 2 %, max 6 % per edge) takes a 3 % re-frame from **0.02 to 0.95**
  inside the production threshold of 31 bits, and the full measured site pipeline with it;
- the precision cost is **zero within the resolution of 805,683 genuine negative pairs**: not one
  unrelated page picture came within **48 bits** of any crop of a moment, at 16, 81, 256 or 2,401
  crops, and the closest of the whole study sits at 54. The threshold is 31;
- on our own index, **4 of 4** cross-site page pairs that PDQ loses at 32 bits come inside the
  threshold with a one-sided grid, three of them to 0–2 bits, while a 390,625-combination negative
  control over 200 unrelated pairs never got below 78 bits.

The asymmetry that makes this affordable: **we own the originals**. Grid generation is offline,
one-off, on the operator's Mac, and **fully retroactive over all 203,315 page hashes already
stored** — no re-fetch, no re-hash, no request budget. A page-side crop provably cannot undo a
page-side crop (if the page is `crop_p(original)`, matching needs the *reference* at `crop_p`), and
the measurement on eight real near-miss pages confirms it.

## Decision

**Store a lattice of crops per reference moment, search 16 of them, decide on all of them.**

1. **The lattice.** Four independent edge trims, step **2 %**, maximum **6 %** per edge → 256 crops
   per moment (`matching.crop_grid.lattice()`). Both numbers are measured: a 3 % step gives 0.88
   recall and a 4 % step 0.41; a 10 % box buys 5 % re-frames and a 20 % box buys almost nothing
   affordable. Past about 3 % of re-frame the grid stops being the cheap answer, and that is the
   stated limit of this decision.
2. **Cut the crops from the master frame, never from the 512 px picture.** Cropping the downscaled
   picture costs a median of 8 bits (p90 12, max 34) — a third of the threshold budget for nothing.
   `client/fingerprint.py` emits frames at the master width (≥ 1024 px) for grid generation;
   `matching.crop_grid.MIN_SOURCE_WIDTH` records the measurement.
3. **Storage: one packed `bytea` per moment, not 256 rows.** `matching_referenceframehash` costs
   486 bytes per row, so rows would be 9.6 GB; the blob is N × 32 bytes in the lattice's own order,
   **8.2 KB per moment and 629 MB for the whole current library**, on the `dihedral = 0` row. The
   dihedral expansion does **not** multiply with the grid: mirroring stays the job of the eight
   uncropped orientations.
4. **Search in two stages.** Stage 1 puts the **16 corner crops** of each moment into the existing
   exact `IndexBinaryFlat` (ADR 0024) — +1,228,720 hashes, **+79 MB of RAM** — and searches at
   radius 64. Stage 2 reads the candidate moment's blob and takes the exact minimum over all 256
   crops. Composite recall on a 3 % re-frame: **0.95**. Stage 1's false rate (5.0e-6 per pair) costs
   about 35,000 blob reads per full scan, each 256 XORs — it buys cheap work, never a false match,
   because stage 2 decides.
5. **The decision threshold stays PDQ's.** A grid hit is a PDQ hit in PDQ's own units, so it is
   **evidence** in the sense of ADR 0015 — unlike ADR 0037's sketch, which by its own measurement
   can never be. But a minimum over 256 correlated hashes shifts the whole distance distribution
   down by 10–16 bits, so `calibrate_confidence` must be re-run on adoption and
   `matching/confidence.py`'s distance-to-score mapping re-derived.
6. **Pipeline identity (ADR 0022).** A reference with a crop grid is a different recipe, so it
   carries a new `pipeline` tag and imports **alongside** the existing rows, exactly as the v0/v1
   grids do. Nothing is migrated in place and no stored hash changes.
7. **Off by default**, behind `MATCHING_REFERENCE_CROP_GRID`, which decides only whether the
   matcher puts stage-1 crops in the index and consults the blobs. A library without grids behaves
   exactly as today.

**Not adopted, with the measurement that rejected each** (`reference-crop-grid.md` §7): content-box
normalisation by edge energy or entropy (worse at every setting, on both re-framed and plain
pairs); aspect padding or cover-cropping (124 bits on a copy that matches at 12 today); aspect
squash-normalisation (2 bits gained, a whole-index re-hash to get them); a page-side crop or
"un-crop" grid (cannot work, and the apparent 2–12 bit gain on real pages is the generic
minimum-over-a-grid shift); phase-correlation alignment (recovers the transform, but needs the
pixels of *both* sides at comparison time, costs 251 ms per candidate pair, and loses to the grid
on the real cross-site pairs).

## Consequences

**Good.**

- The page side is untouched and the change is **retroactive**: every page hash already stored is
  compared against the new grids with no request. That is the one thing ADR 0037 cannot offer.
- No new dependency: numpy and Pillow, both already in the lockfile. No model file, no ONNX
  runtime, no per-process resident set.
- The browser fingerprinting client (ADR 0017/0019) needs no change at all — but that cuts both
  ways: **an original a customer registers in their own browser gets no grid**, because 256 crops
  per moment is 256× the work that tab does, so it keeps matching exactly as today. ADR 0037 has
  the same blind spot for the same kind of reason, and neither closes it.
- Timeline verification, `copy_extent`, common-frame discounting and shared-frame marking are
  unaffected: every crop of a moment carries that moment's timestamp, and the blob hangs off the
  moment's own row.

**Costs, stated plainly.**

- **A re-fingerprint of every original on the operator's Mac**: 19.7 M grid hashes at 7.37 ms, i.e.
  40 core-hours, about 5 h on 8 cores for the current 44 originals, plus 629 MB to transfer.
- **+79 MB of matcher RAM and +629 MB of blob storage today**, both linear in the library. At 500
  originals that is ~900 MB of RAM and ~7 GB of blobs, which is where ADR 0024's IVF backend or a
  per-organisation index becomes necessary. This decision does not scale past that and says so.
- **A second full rescan** the first time grids appear, because the scan fingerprint changes.
- **Nothing here helps the sub-second sampling offset** of issue #73 — the other half of the recall
  problem — which remains ADR 0037's or a 2 fps re-fingerprint's job.

## Pilot, pre-registered

Re-fingerprint the pilot originals with a 256-crop grid, import them alongside, and re-match.
This ADR pre-registered **files A and B** (references 1/40 and 8/76, the two originals with the
most page evidence); the operator widened that to **eight** on 2026-09-18. The criteria below are
unchanged — eight files is more evidence for the same frozen bars, not a different test — and
`crop_grid_pilot --stage select` prints which eight, ranked by confirmed copies plus near-miss
pages, with the storage, RAM and CPU they cost.

- **Criterion 1 (recall).** Of the near-miss pages of those files — the ones 32–72 bits from a
  reference today — **at least half** come inside 36 bits.
- **Criterion 2 (no harm).** No page those originals match today moves outward by more than 4 bits,
  and no page moves from confirmed to unmatched.
- **Criterion 3 (precision).** The number of *new* proposed matches for those two originals that a
  human rejects is at most one in five.

Fail criterion 1 and the reference-side case is unproven from our own data — say so and stop; the
cross-site result of `reference-crop-grid.md` §6.1 stands on its own for clustering. Fail criterion
2 or 3 and the grid is withdrawn.

> The literals **32–72** and **36** above — and the **31** the implementation used for "matches
> today" in criterion 2 — were the matcher's thresholds when this was written. The amendment of
> 2026-09-18 (below) replaces all of them with `STRICT_MAX_DISTANCE` itself and narrows criterion
> 1's band accordingly. **The bars did not move.**

## Amendment 2026-09-18 — the instrument read a threshold the matcher had stopped using

The pilot ran for the first time on 2026-09-18 and returned **UNPROVEN — STOP**. Reading the run
found two faults in the *instrument*, both with one cause: `matching.references.STRICT_MAX_DISTANCE`
was raised from 31 to 36 (ADR 0015 amendment) **after** this pilot was written, and
`marceline/crop_grid_pilot.py` was not updated with it. This amendment fixes the instrument. It is
written after the numbers were seen, so it states exactly what changed, what did not, and what the
first run says under both readings.

### The criteria, before and after

| | before | after |
|---|---|---|
| Criterion 1, band | near miss = **32–72** bits from the plain reference today | near miss = **above `STRICT_MAX_DISTANCE`, up to 72** bits — today (36, 72] |
| Criterion 1, recovery | inside a literal **36** bits with the grid | inside **`STRICT_MAX_DISTANCE`** with the grid |
| Criterion 1, bar | at least half | **at least half — unchanged** |
| Criterion 2, matched today | distance ≤ a local literal **31** | distance ≤ **`STRICT_MAX_DISTANCE`** |
| Criterion 2, "lost" | a confirmed page whose distance with the grid exceeds 31, **whether or not anything matched it today** | a confirmed page that **was matched today** inside the threshold and is not inside it with the grid |
| Criterion 2, drift | no matched page moves outward by more than 4 bits | **unchanged**, and the report now says whether the page is still inside the threshold |
| Pages with no frame hash | silently counted as losses | excluded from **every numerator and denominator**, reported on their own line: *"no page hash: N — no signal can reach these"* |
| Criterion 3 | at most one in five of the new proposals rejected | **unchanged** |

**No bar moved.** Half, four bits, one in five are exactly what was pre-registered, and
`verdict()`'s refusal to declare success on what it did not measure — including the sentence *"Do
not widen the band, the threshold or the lattice to reach the bar"* — is untouched. The two
yardsticks now derive from `STRICT_MAX_DISTANCE`, and a test fails if they and the matcher diverge
again (`marceline/tests/test_crop_grid_pilot.py`, the same guard
`matching/tests/test_browser_parity_thresholds.py` gave the browser parity harness on the same day,
for the same cause).

### The first run under both readings

| | as printed (2026-09-18) | corrected |
|---|---|---|
| Criterion 1 | 10 of 55 near-miss pages inside 36 bits = **18.2 %** | 6 of 51 = **11.8 %** |
| Criterion 2 | **7** confirmed pages "lost" | **0** lost; 3 of the 7 have no page frame hash at all, 3 moved *inward* and sit inside 36, 1 outward move (page 11476, 28 → 34) is still inside the threshold |
| Criterion 3 | 1 of 6 reviewed rejected = 16.7 % | unchanged |
| Verdict | UNPROVEN — STOP | UNPROVEN — STOP |

**The corrected reading of criterion 1 is worse, not better** — 11.8 % against 18.2 %, because four
of the 55 "near-miss" pages were already inside the matcher's threshold today and all four had been
counted as recovered. That is the proof this amendment is not a post-hoc rescue: it lowers the
number the decision turns on. Criterion 2 goes the other way, from a **false `WITHDRAW`** to a
clean pass, and a false withdrawal is exactly as damaging as a false adoption.

### The confounds the run exposed, now printed with every result

- **Recipe.** The gridded references were produced at **2 fps** against **1 fps** on every plain
  row, on a different downscale path (`ffmpeg-rgb512-pdq-v1` → `ffmpeg-master-pil512-pdq-grid256-v1`).
  Both push the gridded side up, so a *gain* cannot be attributed to the lattice alone. `measure`
  now prints each file's two recipes and flags a differing sampling rate.
- **Population.** Four of the eight originals `--stage select` ranked highest carry no grid — among
  them the top-ranked one, this ADR's own pre-registered **file A** (references 1/40). `measure`
  now prints which of the top-ranked originals are still missing, because `select` itself hides
  files that are already gridded and so cannot say.

### What a valid re-measurement requires

1. The missing recommended originals fingerprinted **with the grid**: references **40, 74, 56, 59**
   (the ranking's own order), file A first.
2. At a sampling rate and pipeline that **match their plain references** — 1 fps — or the 2 fps
   recipe declared part of the signal and costed as such (it doubles the moments, so it doubles the
   blobs, the corner hashes, the matcher's resident set and the Mac's CPU).
3. `import_fingerprints` (they land *alongside*, ADR 0022), then `crop_grid_pilot --stage measure`.

Until then the standing verdict is the one the command printed: UNPROVEN — STOP.
