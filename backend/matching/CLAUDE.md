# Matching

What must not break here, and the single place each rule is answered. The full detail —
signatures, measurements and the derivation behind every number — moved verbatim to
`docs/conventions/matching.md` on 2026-09-18; the reasoning is in the ADR each line names. The
browser-capture **worker** moved the same day to `backend/marceline/capture/CLAUDE.md` (its triage
stays here) and the **customer case file** to `backend/marceline/CLAUDE.md`, next to
`marceline/app/cases.py`. The root `CLAUDE.md` hard rules and `backend/CLAUDE.md` apply here too.

## Hashing and media
- Entry points (`matching.frames`): `hash_poster`, `hash_sprite`, `hash_video`; all raise
  `matching.pdq.MediaDecodeError` (`FrameExtractionError` subclasses it). Each returned
  `HashedFrame` becomes one `FrameHash` row; **discard the bytes**. PDQ (`matching.pdq`) is not
  flip-invariant and is sensitive to crops above ~1 % per side.
- **Media kinds** (`index.Media.Kind`): `poster`, `frame`, `sprite` (issue #14), `preview`, `photo`
  — the work itself on photo-first sources, not a preview of anything (issue #69) — and `capture`
  (ADR 0016), whose `eligible_at` stays NULL forever, so no fetch path can request it; it exists
  only to hold frame hashes. The safety gate (ADR 0018) treats every kind alike.
- **A sprite's grid must be known before hashing, never guessed** (issue #14): declared by the
  adapter (`MediaSpec.sprite_columns`/`sprite_rows`, both set or both NULL — a database constraint)
  or detected by `matching.sprite_grid.detect_grid`, which returns `None` rather than choose
  between two geometrically valid grids. Neither route → `Media.Status.SKIPPED`, **never a retry**:
  the same bytes would answer the same way, and a wrong grid hashes garbage tiles the index can
  never clean up.
- **Flips are the reference side's job, and they are already done** (issue #8,
  `docs/matching/page-side-flips.md`): every import path stores all eight dihedral orientations of
  an original, so **page frames must not be expanded**. `pdq.dihedral_from_hash` derives an
  orientation from a stored hash without pixels, but it is an approximation — never use it on a
  reference, and never present a derived hit as the reference's own. Nothing is wired to it:
  measure first with the read-only `manage.py mirror_report` (`matching/mirrors.py`).

## Search index (ADR 0024, `matching.ann`)
`matching.hamming.HashIndex` is the interface; only the engine underneath changes (`auto` =
numpy scan below `ann.MIN_FAISS_SIZE`, the **exact** `IndexBinaryFlat` above; `MATCHING_ANN_BACKEND`
overrides; a missing `faiss` wheel falls back to numpy — slower, never wrong).
- **`ivf` is approximate and opt-in; do not make it the default** — at our scale it is both lossy
  and slower end to end. Candidates from any FAISS backend are **always re-ranked exactly**, so a
  reported distance is never approximate; re-ranking cannot recover a neighbour the index failed to
  return, which is why an approximate backend's misses are real misses.
- **FAISS threads are capped** (`ann.search_threads`, `MATCHING_FAISS_THREADS`): its own default
  (every core) starved crawlers, media workers and Postgres on the dev box.

## The three signals — all off by default, and two of them are never evidence
- **Frame embeddings** (ADR 0037, `MATCHING_EMBEDDINGS_ENABLED`, `matching/embedding.py`): the
  digest-pinned image encoder, its output immediately reduced to a 512-bit sign sketch
  (`SKETCH_BYTES`), and a Hamming distance as the only operation on the result. **No face
  detection, no biometrics, no person identification, no age estimation.**
  - Nothing public returns a float vector, `Encoder` exposes only `sketch`/`sketches`, and **only
    this module may open a session** — in the backend and in the client. `verify_model` refuses a
    file whose size, digest or graph is not the pinned one.
    `matching/tests/test_embedding_boundaries.py` reads the source and asserts all of it, including
    that nothing in the backend names a tokenizer, face detector or age estimator. **Never loosen
    those tests to make something else possible — write an ADR.**
  - A missing model or wheel **raises** (`EmbeddingModelMissing` / `EmbeddingUnavailable`); never
    add a path that skips the signal silently, because a gap is indistinguishable from "not
    similar" afterwards.
  - Search is `matching/sketch_index.py` `SketchIndex.nearest`, top-k only. **Never add a radius
    query**: a threshold on a sketch distance is the operation this signal must not offer.
  - A page sketch is only ever compared with a reference sketch of the same `EMBEDDING_PIPELINE`
    tag (ADR 0022's rule). Not retroactive: a missing sketch means "no information". Tests never
    need the model — `matching/tests/sketches.py` has a fake encoder of the same shape.
- **Metadata scoring** (ADR 0040 + its 2026-09-18 amendment, `docs/matching/metadata-scoring.md`,
  `MATCHING_METADATA_CANDIDATES_ENABLED`): `matching/metadata_score.py` is the pure rule (no ORM, no
  clock, no I/O, no picture) and `matching/metadata_candidates.py` the database side, which **writes
  nothing and asks nobody**. An admitted pair is a browser-capture candidate and nothing else; PDQ
  then decides by itself. **Duration is an indicator, never a hard filter**: the rule only ever
  admits on a coincidence, never rejects — clips are leaks.
- **A candidate source never becomes evidence, and three independent guards say so** for both
  signals: the database constraints `reference_match_embedding_is_never_evidence` and
  `reference_match_metadata_is_never_evidence`, `confidence.band_for` and `calibration.apply_bands`
  excluding those rows. **Keep all three.** `ReferenceMatch.CANDIDATE_ONLY_SOURCES` names the set
  once; such a row carries zero evidence frames and band `review`, never `high`, so never a notice,
  and is promoted only by a later PDQ match taking it over or by a human confirming it.
- **The reference-side crop grid** (ADR 0039, issue #80, `docs/matching/reference-crop-grid.md`,
  `MATCHING_REFERENCE_CROP_GRID`) is the exception: **a grid hit is ordinary PDQ evidence**, in
  PDQ's own units, through the same path at the same threshold with the moment's own timestamp — no
  candidate source, no separate band. It sits on the reference side, so it is retroactive over
  every page hash already stored: no re-fetch, no budget.
  - `matching/crop_grid.py` is pure (the client imports it too); `matching/crop_index.py` is the
    database half and the two-stage search: corner crops (a guaranteed prefix of `grid_specs`) into
    the exact `IndexBinaryFlat` at `crop_grid.STAGE1_RADIUS`, then the moment's blob decides.
    **Never add a radius query over a blob** — the two stages exist so the grid hashes stay out of
    the index.
  - **Storage is one packed `bytea` on the `dihedral = 0` row** (`ReferenceFrameHash.crop_grid`),
    never 256 rows; a database constraint enforces whole hashes, the stage-1 prefix and the orientation.
  - `incremental.scan_settings` records `crop_grid`, so switching the setting costs one full rescan,
    and **adoption also requires re-running `calibrate_confidence`**: a minimum over 256 correlated
    hashes moves the whole distribution down. Its own pipeline tag `CROP_GRID_PIPELINE` makes a
    gridded reference import *alongside* the plain one (ADR 0022). Pilot: `crop_grid_pilot`
    (`marceline/crop_grid_pilot.py`) — no request, and it refuses to declare success on what it did
    not measure.
  - **The pilot's yardsticks are `references.STRICT_MAX_DISTANCE`, never a literal of its own**
    (ADR 0039 amendment 2026-09-18). It carried `MATCH_DISTANCE = 31` after the matcher moved to
    36, so its first run reported 7 confirmed pages lost where the real number was 0 and counted
    four already-matched pages as recovered. The near-miss band is (strict, 72]; criterion 2 can
    only lose a page that was *matched today*; a page with no frame hash is excluded and counted on
    its own line; `measure` prints its confounds. The bars are frozen and a test fails if the two
    thresholds diverge again. First run: UNPROVEN — STOP (`docs/matching/reference-crop-grid.md`
    §10b).

## References, pipelines and creators
- `matching.references` owns atomic imports, matching and reports (ADR 0010): store hashes and
  metadata only, match each eligible page frame once per reference, preserve human statuses, delete
  only stale proposals in scope. Admin import is disabled. `matching.fingerprint` is Django-free
  (tested) because `client/fingerprint.py` runs it on the operator's Mac; its strict `to_json` /
  `from_json` define the JSON format — **hashes and metadata, never paths or host/user names**.
- **A reference's identity is (sha256, pipeline, frame_fps)** (issue #70, ADR 0022,
  `reference_unique_sha256_pipeline_fps`): a re-fingerprint under a new pipeline tag or another
  sampling rate imports *beside* the old row and inherits its cast — old references are never
  reinterpreted, and nothing is deleted until the operator retires the row. Bump the tag when
  frames or hashing change. `POST /api/v1/originals` still refuses a sha256 under **any** pipeline:
  a customer upload is a claim. Two references of the same *file* never mark each other's frames
  shared, or a re-fingerprint would wipe out the evidence of both rows.
- Reference timestamps are sample-grid seconds, and since issue #70 slot k really is the picture at
  k (`frames.FPS_ROUND`; `fingerprint.ALIGNED_PIPELINES` names the recipes that hold it).
  **`frames.sample_video_frames` (page preview clips) deliberately keeps the default rounding** —
  its frames carry a position and no timestamp, and changing it would invalidate every stored
  `index.FrameHash` row.
- **Several creators per original** (issue #64, ADR 0010 amendment): `Reference.creator` is the
  primary creator (rights holder), `Reference.creators` holds everyone. The invariant lives in
  `matching.creators.set_creators`, which every import path calls inside its transaction — **never
  in a DB trigger**. Filtering and linking use **all** creators; only the rights-holder FK and the
  API's `creator_name` stay primary-only.
- **Photo collections** (issue #69, ADR 0020): a `Reference` with `kind=photos` is ONE collection of
  N images, because a leak reposts the set. `frame_index` is the image's ordinal and `timestamp_s`
  repeats it — a position, never a time, so **check `Reference.kind` before rendering mm:ss**.
  `frame_fps`/`duration_s` are NULL (`reference_shape_matches_kind`) and `sha256` is the
  order-independent collection digest. A collection has no order, so `PageEvidence(ordered=False)`
  computes no timeline at all (a NULL `timeline_score` is neutral, **never a penalty**).
  - **A browser-hashed set is its own tag** (issue #116, ADR 0053): `BROWSER_PHOTO_PIPELINE`
    (`browser-rgb-pdq-v0`) beside the operator's `pillow-rgb-pdq-v0`. Same recipe shape, a
    different **decoder** — `createImageBitmap` is *asked* for no EXIF transform and no colour
    conversion and may ignore both — and nobody has measured the gap, which is exactly when ADR
    0022 says a recipe gets a tag of its own. `make browser-parity PARITY_ARGS="--photos"` measures
    it in a real browser; `matching/tests/fixtures/photos/collection.json` pins everything else
    (`test_photo_fixtures.py` here, `frontend/src/lib/photoFingerprint.test.ts` there) — same eight
    hashes per image, same quality, same ordinals, same collection digest. **Never weaken either
    assertion**, and regenerate the fixture only with `python -m matching.tests.generate_photo_fixtures`.

## Evidence, confidence and extent
- Frame evidence (ADR 0015). `matching.timeline` is pure; posters have no order. `PageEvidence.
  resolve()` admits frames between `max_distance` and `LOOSE_MAX_DISTANCE` only in a consistent
  timeline chain with a strict frame, or with cluster/mirror corroboration. **Never raise the
  strict threshold instead** — `references.STRICT_MAX_DISTANCE` is the one constant every caller
  reads (`docs/matching/strict-threshold-36.md`).
- **Shared frames are not evidence** (distributor intros/outros, `mark_shared_frames()`):
  `ReferenceMatch.strength` is `strong` only with >= 2 distinct page frames matching non-shared
  frames (`evidence_frames`).
- Common frames (`matching.common_frames`, CLI `build_common_frames`, ADR 0012): **only groups a
  human confirmed discount anything** — a proposal is the daily rebuild's guess, and an intro-heavy
  leak copied to many pages looks exactly like an intro. Candidates are active pages only (issue
  #71); `--include-inactive` widens the **labeling queue**, never the scheduled run. It had never
  fired up to 2026-09-17 and fired first on 2026-09-18 (issues #61 and #16,
  `docs/matching/0061-common-frames-review.md`): of the 7 `--include-inactive` proposals the
  operator confirmed 3 and rejected 3 as "same video". **Don't add image recognition here** — the
  recall actually missing is lost to the sites' own re-encoding and letterboxing.
  - **The copy rule may not ask a page for frames it does not have.** `copy_frames_required`:
    `COPY_MIN_SHARED_FRAMES` (2) normally, but **never more than the poorer page holds**, and two
    pages of one group already share that group's frame. 64 % of the index's frames come from
    single-frame media, so without it a poster-only repost can never reach two shared frames and
    one video on ten pages counts as ten videos — which is exactly what the three rejections were.
  - **A gated sample says so**: `media_gated` on the review API's sample pages — a page that is
    `clear` but scored below `SAFETY_GATE_MIN_SCORE` has images the gate withholds, and "No page
    images" alone reads as "this page has none".
- Confidence: `matching.confidence` is pure with named weights; thresholds live in the
  `ConfidenceSettings` row, and `calibrate_confidence [--replace-override]`
  (`matching.calibration`) prints precision per bucket. **Auto decisions are not labels**;
  `AUTO_CONFIRM_HIGH` (default off) confirms proposed high-band rows with `decided_by=auto`.
- **A negative sample is a rejection the matcher scored, and calibration refuses rather than emit a
  threshold the labels cannot carry** (issue #90, ADR 0015 amendment 2026-09-18). Only a pair with a
  stored `ReferenceMatch` has a confidence to place on the curve; a rejection recorded only as a
  `ReferenceLeak` is counted, excluded and **never read as 0** — at 0 it would move the chosen high
  from 0.00 to 0.05 and make the guards pass on 161 pairs nothing ever scored. Five guards, each
  named in the output, and a firing guard writes **nothing at all**: < 20 scored positives, < 20
  scored negatives, no grid step at or above `max(HIGH_THRESHOLD_FLOOR 0.50, review in force)`
  clearing a 95 % Wilson **lower bound** of 98 % precision, a high that would fall without new
  scored negatives, and thresholds calibration did not write
  (`ConfidenceSettings.calibrated_high/_review` vs the ones in force — the operator's, kept unless
  `--replace-override`). **Never relax one to get a number.**
- **Copy extent is never a filter** (issue #62, ADR 0015 amendment, `matching/copy_extent.py`,
  pure): no status, band or row changes. Poster-only evidence is `unknown`, never `excerpt`. Bump
  `CLASSIFIER_VERSION` when a rule changes; the notice gate asks `requires_exception_review(match)`
  and customers get `originals.extent_of`.
- `matching/duplicates.py`, `matching/evaluate.py` and `matching/clustering.py` are pure (no ORM);
  DB access lives in `matching/services.py`. `propose_pairs` never overwrites human labels.
  Clustering does deterministic strict-majority admissions with **no automatic cluster merges**,
  and conflicting human decisions abort the transaction.

## Scanning once, and scanning incrementally
- **Scan once** (ADR 0015): `media.find_url_source` (same `services.media_content_key`) before
  downloading, `find_sha256_source` (identical bytes) after. `reuse_hashes` copies the frame
  hashes, records `MediaReuse` and **never crosses safety-flagged pages** — source and target must
  be clear, same decoder.
- Incremental matching (`matching.incremental`): `MatchRunState` holds the **scan** fingerprint
  (`incremental.scan_settings`); a change or `--full` rescans everything. **Anything that changes
  which (frame, reference, distance) triples lie within the radius must go into `scan_settings`** —
  thresholds, shared frames, common groups and human decisions only change aggregation, which every
  run recomputes (ADR 0015 amendment 2026-09-17). **Code that edits `FrameHash` /
  `ReferenceFrameHash` rows in place must instead create new rows or require `--full`.**

## Browser capture (ADR 0016)
- Triage: `matching.triage` is pure (sources `review_band_match`, `confirmed_leak_without_frames`,
  `linked_cluster_member`, `exact_duration_single_thumb`, `metadata_candidate`; exclusions, window
  and budget helpers), `matching.capture_triage` is the database side. CLI
  `triage_browser_capture`; scheduler job `browser_capture_triage`, which runs even when capture is
  disabled.
  - **Never queue**: pages not safety-clear, pages with any `safety_matches`, removed pages,
    disabled sites, sites with `catalog.Site.capture_enabled` off (issue #78 — an operator switch,
    deliberately off the reviewed `SitePolicy`/`seed_sources` path so a re-seed can never flip it
    back on; `Site.capture_play_click` is the second such switch, default False), age gates, login
    pages, pending media, human-rejected pairs, and confirmed high pairs with >= 3 evidence frames.
  - Gate signals come from crawler tables, so only `marceline.scheduler.crawler_gate_signals` can
    supply them; rows refreshed without them (`gate_checked=false`) are never batched.
- **The worker half has its own file: `backend/marceline/capture/CLAUDE.md`** — the guard that
  re-reads the gate immediately before the browser opens, the one permitted click, the age
  self-declaration cookies and the per-request refusal policy. It lives in `marceline/capture/`
  because one capture needs `crawler`, `matching` and `index` together, a combination no app may
  import. **PDQ hashes only — never a frame, screenshot or video**, and its output re-enters here
  through `matching.media.store_hashes`, which re-checks the gate.

## Ground truth
`ReferenceLeak` holds proposed/confirmed/rejected decisions; `matching.leak_scoring` is pure and
`matching.leaks` ranks and persists suggestions, **preserving human decisions in both leak and match
tables**. Duration-only candidates need a creator link and a difference <= 1.5 s; matches and
cluster/mirror evidence admit clips at any duration. Title similarity is display-only. Dry runs
write nothing, `rank_candidates` is read-only, and `matching.m2.m2_report` (CLI `m2_report`,
`--max-distance` defaulting to `references.STRICT_MAX_DISTANCE`) writes nothing either. **Admin leak
review uses text links only, never embedded media.** Weights and denominators: ADR 0010.

## The customer case file (ADR 0025)
**The rules moved to `backend/marceline/CLAUDE.md`** with the code that owns them
(`marceline/app/cases.py`, `sync_cases`). `matching.LeakCase` and `matching.CaseFinding` are still
matching's own tables — **one case, many URLs**, observations only, and `matching` may not import
`marceline`, which is why the read model lives over there. **A case file never changes owner**
(ADR 0045): it hangs off the `Reference`, so `ownership.reassign` deletes it with its findings in the
same transaction and the next `sync_cases` rebuilds one for the new owner from the match rows.

## Hashing a customer upload (issue #59, ADR 0044) — off by default
- **`matching/cloud_hash.py` is pure and Django-free**, so the process that opens a customer's
  uploaded video can be started without database credentials. It decides four things and each is a
  test: the child's environment (`child_environment`), the limits (`rlimits` — address space, CPU,
  open files, and **`RLIMIT_FSIZE = 0`**, so the decoder cannot create a file at all, which is what
  makes "no frame ever leaves the worker" a property of the process), the ffprobe allowlist
  (`probe_command`, `parse_probe`, `refusal`) and the two argument lists, asserted element by
  element. **Never remove `-protocol_whitelist file`** from either: a crafted playlist is how
  ffmpeg is turned into a fetcher or a local-file reader, and never put a customer-supplied string
  into an ffmpeg argument — the only variable part is the spool path, which our side generates.
- **`matching/cloud_decoder.py` is the `python -m` entry point** (`-E -s`, not `-I`, which would
  stop `-m` putting the package root on the path): JSON spec in on stdin, JSON **lines** out on
  stdout — progress while it hashes, then one answer document — and **no Django import**; a test
  reads the file and asserts it.
- **Only the document and two integers cross back** (ADR 0044 amendment 2026-09-18).
  `progress_line` / `parse_progress` are the wire format and `parse_progress` coerces: a line that
  is not two integers is dropped, so no text built from the customer's file reaches a log or a row.
  The child's **stderr is `DEVNULL`**; `hash_file` kills the process group on its own deadline.
  `expected_positions` is the denominator or `None` — never invent one, never present it as a time.
- **The recipe is not new.** The decoder calls `fingerprint_video`, so a cloud-fingerprinted
  original carries `PIPELINE` and is bit-comparable with a CLI or `add_reference` one — ADR 0022 is
  satisfied because it really is the same recipe on a different machine
  (`matching/tests/test_cloud_hash.py` asserts it hash for hash).
- **No crop grid (ADR 0039) and no embedding sketches (ADR 0037) from an upload in v1.** The job row
  records the recipe that ran, so a second job kind can be added later without changing an interface.
