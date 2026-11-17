# Matching: the full reference

`backend/matching/CLAUDE.md` keeps the invariants an agent must not break; this file is the detail
it used to carry — entry-point signatures, measurements, the derivation behind every number and the
narrative each rule grew out of. Moved here verbatim on 2026-09-18 (issue #79); nothing was removed.
The decisions themselves live in the ADRs and in `docs/matching/`.

The **browser-capture worker** half (guard, runner, `capture.mjs`, the one click, the refusal
report) moved out of `backend/matching/CLAUDE.md` the same day into
`backend/marceline/capture/CLAUDE.md`, and the **customer case file** to
`backend/marceline/CLAUDE.md`, next to `marceline/app/cases.py`. Their full detail is the
"Browser capture (ADR 0016)" and "The customer case file (ADR 0025)" sections below, unchanged.

---

# Matching and the customer case file

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Matching
- **Media kinds** (`index.Media.Kind`): `poster` (a video's representative still), `frame` (hover
  frames), `sprite` (thumbnail sheet, one fetch worth dozens of moments — issue #14, below),
  `preview` (short clip, video decoder) and `photo` — the work itself on photo-first sources
  (issue #69, first one site11.example), not a preview of anything. A `photo` is decoded on the
  poster path (`hash_poster`, one PDQ frame), shares the `image` hash-reuse family in
  `matching/media.py`, has no timeline position, is claimed with posters rather than behind hover
  frames, and is served in review like any other still. The safety gate (ADR 0018) treats every
  kind alike, including this one. `capture` (ADR 0016) is the odd one out: the browser capture
  worker writes it, its URL is the page's own URL plus `#browser-capture`, and `eligible_at`
  stays NULL forever — so no fetch path can request it and the media worker never claims it. It
  exists only to hold frame hashes.
- **Sprite sheets (issue #14).** A sprite is a grid of small thumbnails tiled into one image for
  the player's seek bar — one fetch, dozens of moments. The grid (columns x rows) must be known
  *before* hashing: PDQ of the whole mosaic matches nothing, and a wrong grid hashes garbage tiles
  the index can never clean up (Hard rules: never guess). Two routes, in this order:
  1. **Declared.** An adapter that read the grid off the site's own markup (a JSON player config,
     or a WebVTT thumbnail track's `#xywh=` cues) sets `MediaSpec.sprite_columns`/`sprite_rows`;
     `crawler.services._attach_media` carries it onto `index.Media.sprite_columns`/`sprite_rows`
     (both set or both NULL, a database constraint) and round-trips it through a listing card's
     `CrawlTask.context` JSON like any other media hint. Believed outright — exact and free.
  2. **Detected.** No adapter declared it: `matching.sprite_grid.detect_grid(width, height)`
     tries every (columns, rows) that exactly tiles the fetched sheet at a plausible tile size and
     a common video aspect ratio, and returns it only when **exactly one** candidate survives — a
     self-similar sheet (a 5x4 grid of 160x90 tiles is, geometrically, also a clean 10x8 grid of
     80x45 tiles at the same ratio) makes more than one candidate common, and geometry alone
     cannot choose, so it refuses (`None`) rather than pick.
  Neither route succeeding is a deliberate skip (`Media.Status.SKIPPED`, `"sprite grid unknown"`),
  never a retry — the same bytes would answer the same way again. The declared grid (or the
  detected one) reaches `matching.media_hash.hash_media_bytes(kind="sprite", sprite_grid=...)`,
  which cuts tiles with `matching.frames.split_sprite` and hashes each
  (`FrameHash.frame_index` = the tile's row-major position). `matching.timeline.frame_position`
  gives sprite tiles the same `Ordering.CLIP` treatment as a preview clip's sample position: a
  real generator lays a storyboard out left-to-right, top-to-bottom, so growing `frame_index`
  should track growing reference time the same way. The crawl runtime's hash children
  (`marceline/runtime/hash_pool.py`) carry `sprite_grid` over the pipe alongside `crop_bars`, and
  a sheet whose grid could not be read raises `UnsupportedMediaError` there exactly as in-thread
  hashing does.
- Entry points (all raise `matching.pdq.MediaDecodeError` on undecodable input;
  `FrameExtractionError` subclasses it):
  ```python
  from matching.frames import hash_poster, hash_sprite, hash_video
  from matching.pdq import HashedFrame, MediaDecodeError, MIN_QUALITY

  hash_poster(data: bytes, min_quality=MIN_QUALITY) -> list[HashedFrame]
  hash_sprite(data: bytes, *, grid=None, tile_size=None, min_quality=MIN_QUALITY) -> list[HashedFrame]
  hash_video(data: bytes, *, fps=None, count=None, max_frames=MAX_VIDEO_FRAMES, min_quality=MIN_QUALITY,
             max_duplicate_distance=None, timeout_s=FFMPEG_TIMEOUT_S) -> list[HashedFrame]
  ```
  Each `HashedFrame(frame_index, hash, quality)` becomes one `FrameHash` row; discard the bytes.
- `matching.pdq`: `to_hex` / `from_hex`, `hamming_distance`. PDQ is not flip-invariant and is
  sensitive to crops above ~1% per side.
- **Flips are the reference side's job, and they are already done** (issue #8,
  `docs/matching/page-side-flips.md`). Every import path stores all eight dihedral orientations of
  an original (`_hash_position`, `dihedral=True` everywhere by default, every pipeline tag) and the
  matcher masks only on quality and reference selection — so a mirrored *page* frame is matched by
  the reference's own mirrored variant, and page frames must not be expanded. Where no reference is
  involved (duplicates, clusters, common frames) both sides are unflipped and a mirror is invisible;
  `pdq.dihedral_from_hash(hash, orientation)` derives an orientation **from the stored hash, without
  pixels**, so closing that gap would need no storage, no migration and no re-fetch. It is an
  approximation — exact for `orig` and `flipp`, median 8 / max 34 bits elsewhere, because a negated
  DCT coefficient moves the median the bits threshold against — so never use it on a reference, and
  never present a derived hit as the reference's own. Nothing is wired to it: measure first with
  the read-only `manage.py mirror_report` (`matching/mirrors.py`).
- **Frame embeddings, the second signal (ADR 0037, off by default).** `matching/embedding.py` is
  the whole thing: the digest-pinned MobileCLIP-S0 **image** encoder through `onnxruntime`, its
  normalised output immediately reduced to a **512-bit sign sketch** (`SKETCH_BYTES` = 64), and a
  Hamming distance as the only operation on the result. Tag `EMBEDDING_PIPELINE =
  mobileclip-s0-fp32-sign512-v1`; a page sketch is only ever compared with a reference sketch of
  the same tag (ADR 0022's rule).
  - Nothing public there returns a float vector, `Encoder` exposes only `sketch`/`sketches`, and
    **only this module may open a session** — in the backend and in the client.
    `verify_model` refuses a file whose size, digest or graph (one `(batch, 3, 256, 256)` input,
    one `(batch, 512)` output) is not the pinned one, so a text tower or a head is rejected before
    a session exists. `matching/tests/test_embedding_boundaries.py` reads the source and asserts
    all of it, including that nothing in the backend names a tokenizer, face detector or age
    estimator. Never loosen those tests to make something else possible — write an ADR.
  - A missing model or wheel **raises** (`EmbeddingModelMissing` / `EmbeddingUnavailable`); never
    add a path that skips the signal silently, because a gap is indistinguishable from "not
    similar" afterwards.
  - Hashing: `frames.hash_frames/hash_poster/hash_video(embed=…)` take an injected
    `Embedder` (one picture in, 64 bytes out), so `matching.frames` still needs nothing but
    Pillow, numpy and pdqhash. `media_hash.hash_media_bytes(embed=True, model_path=…)` builds the
    process's single encoder; `HashedMedia.embedding_pipeline` and `HashedFrame.embedding` carry
    the result, `media.store_hashes(embedding_pipeline=…)` stores it, and `reuse_hashes` copies a
    source's sketches with its hashes (same bytes, same decoder, same picture).
  - **Memory, not CPU, is the limit**: +121 MB measured per loaded session, so
    `embedding.hash_processes` reduces `RUNTIME_HASH_PROCESSES` to what `MemAvailable` allows and
    refuses to start when not even one fits; `HashPool(embed=True)` raises its RSS ceiling by
    `MODEL_RSS_MB` or every child would be recycled as a leak. The legacy per-site `media_worker`
    needs an explicit `--embed` (twenty processes × a model do not fit); the setting switches on
    the crawl runtime's media lane.
  - Search: `matching/sketch_index.py` `SketchIndex.nearest(sketches, k)` — the same exact FAISS
    `IndexBinaryFlat` as ADR 0024, top-k only. **Never add a radius query**: a threshold on a
    sketch distance is the operation this signal must not offer.
  - Candidates: `matching/embedding_candidates.py` `refresh()` writes `EmbeddingCandidate` rows
    (top-k per page frame, collapsed to the best moment per original, `mutual` when both sides
    list each other) and turns the mutual ones into `ReferenceMatch(candidate_source='embedding')`
    — zero evidence frames, zero matched frames, no timeline, confidence 0, band `review`.
    `matching.references` never reads that table; a pair PDQ already matched is left alone; a
    human decision is never withdrawn. The invariant is enforced by a database constraint
    (`reference_match_embedding_is_never_evidence`), by `confidence.band_for(embedding_only=True)`
    and by `calibration.apply_bands` excluding those rows — keep all three.
  - Reference side: format v4 (`embedding_pipeline` + an optional per-frame `embedding`, unflipped
    orientation only — an embedding is roughly flip-invariant, so one sketch per moment, not
    eight). `references.attach_embeddings(fingerprint)` adds sketches to an existing original
    after comparing **every** position's PDQ hash bit for bit; `import_fingerprints` calls it when
    a file on the same grid brings sketches the stored row lacks.
  - Settings: `MATCHING_EMBEDDINGS_ENABLED` (off), `MATCHING_EMBEDDING_MODEL_PATH`,
    `MATCHING_EMBEDDING_TOP_K` (10), all pinned in `OPERATIONAL_DEFAULTS`. Tests never need the
    model: `matching/tests/sketches.py` has a fake encoder of the same shape.
- **The reference-side crop grid, off by default** (ADR 0039, issue #80,
  `docs/matching/reference-crop-grid.md`). A site trims 1-3 % off the edges asymmetrically, which
  costs PDQ 44-70 bits; a lattice of 256 crops per *reference* moment takes a 3 % re-frame from
  0.02 to 0.95 inside the threshold of 31 for no measurable precision (805,683 negative pairs, none
  within 48 bits of any crop). It lives on the reference side because we own the originals, which
  makes it **retroactive over every stored page hash** — no re-fetch, no budget.
  - `matching/crop_grid.py` is pure (numpy + Pillow; the client imports it too): the lattice, the
    measured constants, `grid_specs` (stored order, the 16 corners a guaranteed prefix), `render`
    (crop the **master**, then scale — cropping the 512 px output costs 8 bits median), `build`,
    `pack`/`unpack`, `prune` (optional dedup, off) and `best_distance`/`best_distances`.
  - `matching/crop_index.py` is the database half and the two-stage search: corner crops into the
    exact `IndexBinaryFlat` at `STAGE1_RADIUS` (64), then the moment's blob decides. Stage 1 loads
    only the blob **prefix** (`SUBSTRING`), 39 MB instead of 629. **Never add a radius query over a
    blob**: the two stages exist so the 19.7 M grid hashes stay out of the index. Measured +96 MB
    of resident set at library scale, with the per-moment side columns kept per moment (per hash
    cost 29 MB more).
  - **Storage is one `bytea` on the `dihedral = 0` row** (`ReferenceFrameHash.crop_grid`), never
    256 rows (9.6 GB against 629 MB). Postgres TOASTs it, so `load_reference_hashes` is unaffected.
    A database constraint enforces whole hashes, the stage-1 prefix and the orientation.
  - **A grid hit is ordinary evidence**, unlike the sketch: `incremental.scan_grid` merges its
    minima into `ReferenceCandidate` and `references.aggregate_hash_matches(grid_hits=)` replays
    them into the same `FrameMatch` lists, at the same threshold, with the moment's own timestamp.
    There is no candidate source for it and no separate band.
  - `incremental.scan_settings` records `crop_grid`, so switching the setting costs one full
    rescan. **Adoption also requires re-running `calibrate_confidence`**: a minimum over 256
    correlated hashes moves the whole distribution down 10-16 bits.
  - Reference side: pipeline tag `CROP_GRID_PIPELINE` (a different recipe — master frames from
    ffmpeg, Pillow's scaler — so it imports *alongside* under ADR 0022), format version 5 with a
    document-level `crop_grid` lattice and a per-moment blob in hex, `client/fingerprint.py
    --crop-grid`. 2.43-5.54 s of CPU per sampled second, so hours per batch.
  - Settings: `MATCHING_REFERENCE_CROP_GRID` (off), pinned in `OPERATIONAL_DEFAULTS`. Pilot:
    `marceline/crop_grid_pilot.py`, CLI `crop_grid_pilot`, eight originals by operator decision
    (2026-09-18); it makes no request and refuses to declare success on what it did not measure.
- `matching.hamming.HashIndex`: `HashIndex.from_pairs([(hash_id, hash_bytes), ...])`, then
  `index.query(hash, max_distance) -> [(hash_id, distance)]`, `query_many`, `pairs_within` or
  `neighbor_pairs`. `add(ids, hashes)` appends without rebuilding (incremental matching).
- **Search backends (ADR 0024, `matching.ann`).** The interface above never changes; only the
  engine underneath does. `auto` (the default) uses the numpy XOR/popcount scan below
  `ann.MIN_FAISS_SIZE` (4096 hashes) and FAISS `IndexBinaryFlat` above it. **`IndexBinaryFlat` is
  exact** — same pairs as the numpy scan, ~30x faster (0.75 ms vs 22 ms per query at 432k hashes),
  so the speedup costs no recall at all. Override per index with `HashIndex(..., backend=...)` or
  globally with the `MATCHING_ANN_BACKEND` setting (`auto` / `numpy` / `flat` / `ivf`).
- **FAISS threads are capped** (`ann.search_threads`): `MATCHING_FAISS_THREADS` (Django setting or
  environment variable, default `DEFAULT_THREADS` = 2) OpenMP threads per search, and one thread
  for batches below `SMALL_BATCH` (64 queries), where OpenMP start-up costs more than the scan.
  FAISS's own default (every core) starved crawlers, media workers and Postgres on the dev box.
- **`ivf` is approximate and opt-in; do not make it the default.** At our scale it is both lossy
  (98.7% pair recall at nprobe 32) *and* slower end to end, because k-means training costs more
  than the exact scan it replaces. ADR 0024 has the full recall table.
- Candidates from any FAISS backend are **always re-ranked exactly** against the index's own uint64
  copy of the hashes, so a reported distance is never approximate and a result is never above the
  threshold. Re-ranking removes false positives; it cannot recover a neighbour the index failed to
  return, which is why an approximate backend's misses are real misses.
- A missing `faiss` wheel falls back to the numpy scan: correct but slower, never wrong.
- `matching/duplicates.py` and `matching/evaluate.py` are pure (no ORM); DB access lives in
  `matching/services.py`. Candidate pairs are created with `propose_pairs` (never overwrites human
  labels); `label_pair` sets a label.

- `matching/clustering.py` is pure: deterministic strict-majority admissions, no automatic cluster
  merges. `services.build_clusters` collects PDQ/SHA256/external-link/mirror-poster evidence and imports SAME
  PairLabels transitively as confirmed memberships. Conflicting human decisions abort the transaction.
- Clusters have no stored original; admin computes the earliest known upload (then first_seen;
  unknown upload dates last) among active members. Empty clusters survive page/member deletion.
  Confirmed/rejected decisions are preserved; explicit SAME labels may move confirmed memberships
  to a shared cluster. Evaluation merges cluster decisions with explicit PairLabels taking precedence.

- Creator originals: `matching.references` owns atomic imports, matching, and reports (ADR 0010).
  `frames.iter_video_frames(path, fps=...)` streams a whole local video without copying it or
  applying the preview frame cap. Store hashes and metadata only; default eight dihedral variants.
  Reference timestamps are sample-grid seconds (`frame_index / fps`), and since issue #70 that is
  the truth: `fps=F:round=up` (`frames.FPS_ROUND`) hands back the last picture at or before second
  k, so slot k really is the picture at k. The filter's default rounding returned the picture at
  about k + 0.47 s under the label k — measured 0.0 ms residual now, 0.0 ms of drift over 120 s,
  and at worst one frame period (32 ms) early where no picture lands on the integer second.
  **`frames.sample_video_frames` (page preview clips) deliberately keeps the default rounding**:
  its frames carry a position and no timestamp, and changing it would invalidate every stored
  `index.FrameHash` row. Match each eligible page
  frame once per reference; preserve human statuses and delete only stale proposals in scope.
  CLI: `add_reference`, `match_references`, `reference_report`; admin import is disabled.
- Several creators per original (issue #64, ADR 0010 amendment 2026-09-16): `Reference.creator` is
  the primary creator (rights holder), `Reference.creators` (M2M, related name `referenced_in`)
  holds everyone, primary included. The invariant lives in `matching.creators.set_creators`, which
  every import path calls inside its transaction — never in a DB trigger. Read helpers there:
  `creators_of`, `creator_ids`, `creator_names`, `creator_label` ("A + B"). Filtering and linking
  use **all** creators (shared frames, leak duration links, capture triage, media claims,
  `select_references`, the recall helper); only the rights-holder FK and the API's `creator_name`
  stay primary-only. `add_reference` / `import_fingerprints` take `--creator` repeatedly, the first
  occurrence primary.
- `matching.fingerprint` is Django-free (tested): `client/fingerprint.py` runs it on the operator's
  Mac. `fingerprint_video(path, fps=, dihedral=, title=, creators=, sha256=, timeout_s=,
  progress=) -> Fingerprint` is the reference hashing loop; `add_reference` =
  `fingerprint_video` + `references.import_fingerprint(creator=, creators=, fingerprint=, ...)`.
  `to_json` / `from_json` (strict, `FingerprintFormatError`) define the JSON format (hashes +
  metadata, never paths or host/user names): v2 stores `creator` as a list of names, primary
  first, and v1 (one name or null) is still read; `compare_frames` gives parity stats.
  `Reference.pipeline` tags the recipe (`PIPELINE = "ffmpeg-rgb512-pdq-v1"`; bump it when frames
  or hashing change), `Reference.client` records where it ran (`source`: server / local-cli).
  CLI: `import_fingerprints`.
- Pipeline tags and re-fingerprinting (issue #70, ADR 0022). `PIPELINE_V0` is the superseded video
  recipe; it still validates, still imports and still matches — old references are never
  reinterpreted. `pipeline_label(tag)` renders a tag plus what it means for the grid, and is what
  the admin *Pipeline* column and `reference_report` print, so an operator can see at a glance
  which originals are still on the old grid (admin sidebar filter: `pipeline`).
  A reference's identity is **(sha256, pipeline, frame_fps)** (`reference_unique_sha256_pipeline_fps`,
  migration `matching/0018`, NULLs not distinct so photo collections stay unique), so a
  re-fingerprint under a new pipeline *or at another sampling rate* (e.g. 2 fps, `client/README.md`)
  imports *beside* the old row, inheriting its cast —
  `import_fingerprints` says "alongside reference N on …", `--compare` measures the grid gap
  instead of importing, and nothing is deleted until the operator retires the old row in the
  admin. `matching.fingerprint.ALIGNED_PIPELINES` names the recipes whose slot k is the picture at
  k. Two references of the same *file* never mark each other's frames shared
  (`find_shared_frames` compares `SharedCandidate.sha256`), or re-fingerprinting would wipe out
  the evidence of both rows. `POST /api/v1/originals` still refuses a sha256 under **any**
  pipeline: a customer upload is a claim, and that check is now enforced in the endpoint itself.
  The photo recipe `PHOTO_PIPELINE` is untouched by all of this.
- Photo collections (issue #69, ADR 0020): a `Reference` with `kind=photos` is ONE collection of
  N images, because a leak reposts the set. `frame_index` is the image's ordinal and
  `timestamp_s` repeats it — a position, never a time, so check `Reference.kind` before rendering
  mm:ss (admin `format_moments(photos=)`, review `common.moment(kind, position)`). `frame_fps`
  and `duration_s` are NULL, enforced by `reference_shape_matches_kind`; `sha256` is the
  collection digest (`fingerprint.collection_sha256`: order-independent, domain-separated, so
  adding an image makes a *different* collection). `fingerprint_photos(paths, dihedral=, title=,
  creators=, progress=)` decodes with Pillow at the image's own resolution — exactly what
  `hash_poster` does to a page image — under its own tag `PHOTO_PIPELINE = "pillow-rgb-pdq-v0"`;
  `PIPELINES_BY_KIND` makes `from_json` refuse a photo pipeline on a video document and back.
  JSON format v3 adds a required `kind`; photo documents carry `image_count` and no timestamps,
  v1/v2 are videos. CLI: `client/fingerprint.py --photos DIR` (one file per folder; suffixes in
  `IMAGE_SUFFIXES`, what Pillow 12.3 really decodes), imported by `import_fingerprints` as usual.
- Collection evidence (ADR 0020). A collection has no order, so `PageEvidence(ordered=False)`
  computes no timeline at all (`timeline_score` NULL, already neutral in confidence, never a
  penalty) and only cluster/mirror corroboration can admit a loose 32..40 bit frame. The unit is
  the independent (page frame, collection image) pair: `confidence.collection_images(matched_
  positions, evidence_frames)` is the smaller of the two, so one page picture matching three
  similar photos counts once, and so do three page frames showing one photo. Confidence adds
  `images = 0.20 × (1 − 0.5^(images−1))` for collections only, standing in for the timeline and
  duration signals they cannot earn; `strong` needs 2 image pairs and band `high` needs
  `HIGH_MIN_COLLECTION_IMAGES` (3) where a video needs 2 evidence frames. `calibrate_confidence`
  re-bands video rows in one UPDATE and collection rows one by one for that reason.
- Shared reference frames (distributor intros/outros): `mark_shared_frames()` recomputes
  `ReferenceFrameHash.shared` for all references (dihedral-0 within threshold of another
  reference, same creator included, or matching pages from `COMMON_PAGE_GROUPS` distinct
  cluster/page groups, skipping pages linked to *any* of the reference's creators). Shared
  frames are not evidence: `ReferenceMatch.strength` is `strong` only with >= 2 distinct page
  frames matching non-shared frames (`evidence_frames`); `matched_seconds` / `shared_seconds`
  record the moments. The pure parts (`find_shared_frames`, `aggregate_matches`) take no ORM.
- Frame evidence (ADR 0015). `matching.timeline` (pure): `frame_position(kind, url, frame_index,
  media_id)` gives hover frames (`…_N.jpg`) and preview clips (`frame_index`) an order; posters
  have none. `verify_timeline` returns `score` (0..1, None without order), `consistent_frames`
  (longest in-order chain), `reason`. `PageEvidence.resolve()` admits frames between
  `max_distance` and `LOOSE_MAX_DISTANCE` (40) only in a consistent timeline chain of
  >= `LOOSE_MIN_CONSISTENT_FRAMES` with a strict frame, or with cluster/mirror corroboration by
  another page's current strong match (`loose_frames`). Never raise the strict threshold instead.
- Common frames: `matching.common_frames.build_common_frames` (CLI `build_common_frames
  --max-distance 8 --min-pages 5 --min-quality --dry-run --include-inactive`) groups near-identical page frames on
  >= min-pages distinct videos (clusters, mirror groups, same-duration copies and pages whose group
  frames come from one file — `CandidateFrame.file_key`, media sha256 or its URL-key reuse
  source's — count once; the report line counts candidates that rule dropped) into
  `CommonFrameGroup` + `CommonFrameMember` (frame/page link). Idempotent; groups with `labeled_at`
  (set by `label_common_group(group, kind, studio_name, status, label=None)`) are anchors that keep
  labels and collect new frames. Frames in **confirmed** groups with kind != `not_common`
  (`DISCOUNTING_STATUSES`; stored members or within radius of the representative) are not
  evidence — a proposal is the daily rebuild's guess and discounts nothing until a human confirms
  it (an intro-heavy leak copied to many pages looks exactly like an intro): `common_frames`,
  `common_seconds`. Uses `HashIndex.neighbor_pairs` (vectorised self-join, ~5 s at 78k active
  frames, 30 s at 188k). Candidates are active pages only (issue #71); `--include-inactive` /
  `load_candidate_frames(include_inactive=True)` widens the **labeling queue** to untargeted pages,
  where today's only branding-shaped material sits — discounting is unchanged either way.
  Scheduled daily before `match_references` (ADR 0012), always active-only.
  **Measured 2026-09-17 (`docs/matching/0061-common-frames-review.md`, issue #61): the mechanism
  had never fired** — 0 confirmed groups, 0 discounted frames, and no match's evidence frame
  anywhere in the index looked like branding. Don't add image recognition here; the recall that is
  actually missing is lost to the sites' own re-encoding and letterboxing (≥ 39 % of same-picture
  cross-site frame pairs already sit beyond distance 31).
  **It has fired since: the operator labeled all 7 proposals of the `--include-inactive` run on
  2026-09-18** (issue #16) — three confirmed (two `studio_intro`, one `watermark`) and therefore
  discounting, three rejected with the note "same video", one left proposed because the media gate
  withheld every sample image. Two things follow, both shipped:
  - **The copy rule may not ask a page for frames it does not have.** `copy_frames_required`:
    `COPY_MIN_SHARED_FRAMES` (2) normally, but never more than the poorer page holds, and two
    pages of one group already share that group's frame. 64 % of the index's frames come from
    single-frame media, so a poster-only repost could never reach two shared frames and one video
    on ten pages counted as ten videos — which is exactly what the three rejections were. Measured
    read-only over the live index (276,564 frames, active and inactive): 7 proposals → 4, and the
    three that vanish are the three he rejected; every group he confirmed survives.
  - **A gated sample says so.** `media_gated` on the review API's sample pages: a page that is
    `clear` but scored below `SAFETY_GATE_MIN_SCORE` has images the gate withholds, and "No page
    images" alone reads as "this page has none".
- Confidence: `matching.confidence` (pure, named weights) scores evidence frames, best distance,
  timeline (None = neutral), exact/near duration (shorter pages never penalised), corroboration
  and a common-frame penalty into `ReferenceMatch.confidence`/`band` (`high` needs >= 2 evidence
  frames). Thresholds live in the `ConfidenceSettings` row (defaults until calibrated);
  `calibrate_confidence [--dry-run] [--replace-override]` (`matching.calibration`) prints precision
  per bucket. Auto decisions are not labels.
- **A negative sample is a rejection the matcher scored, and calibration refuses rather than emit a
  threshold the labels cannot carry** (issue #90, ADR 0015 amendment 2026-09-18). Only a pair with a
  stored `ReferenceMatch` has a confidence to place on the curve; a rejection recorded only as a
  `ReferenceLeak` is counted, excluded and **never read as 0** — at 0 it would just move the chosen
  high from 0.00 to 0.05 and make the guards pass on 161 pairs nothing ever scored. Guards, each
  named in the output, all of them writing nothing at all when they fire: < 20 scored positives,
  < 20 scored negatives, no grid step at or above `max(HIGH_THRESHOLD_FLOOR 0.50, review in force)`
  whose decided pairs reach a 95 % Wilson **lower bound** of 98 % precision (≈ 190 pairs above it),
  a high that would fall without new scored negatives, and thresholds calibration did not write
  (`ConfidenceSettings.calibrated_high/_review` vs the ones in force) — those are the operator's,
  shown before → after and kept unless `--replace-override`. Never relax one to get a number. `AUTO_CONFIRM_HIGH` (Django
  setting, default off) confirms proposed high-band rows with `decided_by=auto`; a changed or reset
  auto decision becomes `human` on the next run. `ReferenceMatch.objects.review_queue(include_low=
  False)`: proposed, high → review (→ low), then confidence.
- **Copy extent (issue #62, ADR 0015 amendment 2026-09-17).** `matching/copy_extent.py` is pure:
  `classify(ExtentInputs)` → `full_copy | excerpt | compilation_candidate | unknown` plus a reason
  code and coverage, thresholds as named constants with their measurement; `matched_span`,
  `page_relation`, `combine_extents` (most cautious wins across one file's references).
  `references.refresh_copy_extents(page_ids)` stores it after every `match_references` run for the
  touched pages and for rows of another `CLASSIFIER_VERSION` (bump it when a rule changes; the
  scheduler then re-runs matching). Poster-only evidence is `unknown`, never `excerpt`. **Never a
  filter**: no status, band or row changes. The future notice gate asks
  `requires_exception_review(match)` (true unless a *current* `full_copy`). Customers get
  `extent: full_copy | excerpt | not_determined` (`originals.extent_of`); a compilation candidate is
  `not_determined` for them.
- Scan once (ADR 0015). Media worker: before downloading, `media.find_url_source` looks for a
  DONE media with the same `services.media_content_key` (site02/site04 mirror path with
  suffix, site03 path without token/expiry; never generic paths); after downloading,
  `find_sha256_source` looks for identical bytes. `reuse_hashes` copies the frame hashes, records
  `MediaReuse` and never crosses safety-flagged pages (source and target must be clear, same
  decoder). Page-level cluster key: `services.mirror_key`.
- Incremental matching (`matching.incremental`): `ReferenceCandidate` caches page frames within
  the radius of a reference; runs scan only frames created since the last scan (1 h overlap) and
  new/changed references, then recompute shared flags and every aggregate from candidate frames
  (identical to a full run). `MatchRunState` (pk 1) holds the **scan** fingerprint
  (`incremental.scan_settings`: `SCAN_VERSION`, radius = the larger of strict and loose distance,
  min quality, hash size, exact vs approximate search); a change or `--full` rescans everything.
  Confidence thresholds, shared reference frames, common-frame groups, imported or retired
  references and human decisions only change aggregation, which every run recomputes, so they
  never force a full rescan (ADR 0015 amendment 2026-09-17). Anything that changes which
  (frame, reference, distance) triples lie within the radius must go into `scan_settings`. Code that edits FrameHash/ReferenceFrameHash rows in place must instead
  create new rows or require `--full`. Pure scan: `scan_candidates`, `HashIndex.pairs_within`.
- Browser capture triage (ADR 0016): the queue that aims capture. `matching.triage` is pure:
  source components (`review_band_match`,
  `confirmed_leak_without_frames`, `linked_cluster_member`, `exact_duration_single_thumb`,
  `metadata_candidate`) with
  named weights, `combine_priority`, `page_exclusions` / `pair_exclusion`,
  `changed_substantially`, window and budget helpers. `matching.capture_triage` holds the DB
  side: `collect_candidates(gate_signals)`, `refresh_candidates(now, gate_signals=, limit=,
  dry_run=)`, `budget_view`, and `next_capture_batch(now)`, which only the future worker uses
  (empty unless `BROWSER_CAPTURE_ENABLED` and inside `BROWSER_CAPTURE_WINDOW_UTC`).
  `BrowserCaptureCandidate` allows one open (queued/failed) row per page. Captured rows are never
  touched or re-queued unless the URL, duration or whole media set changed.
  - Never queue: pages not safety-clear, pages with any `safety_matches`, removed pages, disabled
    sites, sites excluded from capture (`catalog.Site.capture_enabled`, issue #78 — an operator
    switch, off the reviewed `SitePolicy`/`seed_sources` path so a re-seed can never flip it back
    on; default True, unset for every site until the operator decides one; `capture_play_click`
    below is the second such switch, default False), age gates (unapproved
    self-declaration or verification provider), login pages, pending media, human-rejected pairs,
    and confirmed high pairs with ≥ 3 evidence frames.
  - Gate signals come from crawler tables, so only `marceline.scheduler.crawler_gate_signals`
    can supply them. Rows refreshed without them (`gate_checked=false`) are never batched.
  - CLI: `triage_browser_capture [--dry-run] [--limit N]`. Scheduler job:
    `browser_capture_triage`, daily after `match_references`, runs even when capture is disabled.
- **Browser capture worker (ADR 0016; the operator enabled capture 2026-09-16).**
  `marceline/capture/` lives in the project package because one capture needs `crawler` (robots,
  the block/challenge detector), `matching` (the queue, PDQ) and `index` together — a combination
  no app may import, like `marceline.scheduler`. CLI `capture_worker [--site DOMAIN] [--limit N]`,
  deliberately **not** in `make workers`.
  - `plan.py` is pure: `CapturePlan` carries every cap (frames, sampling seconds, bytes,
    watchdog) and `sample_positions` spreads samples evenly at ~1/minute, skipping the first and
    last 15 s. The caps *are* the cost control — the worker cannot sample more than the plan says.
  - `runner.py` drives `capture.mjs` as a Node subprocess and resolves Playwright from the
    `frontend/parity` harness install (issue #28). **No Python browser dependency**: nothing in
    `backend/pyproject.toml` can open a browser. Frames return as base64 PNG, in memory only.
  - **`guard.py` is the only place a browser may be pointed at a page.** `capture_page` re-reads
    safety status, score, availability, `Site.enabled` and `Site.capture_enabled` from the
    database *immediately before* the browser starts and raises `CaptureDenied`
    (`CAPTURE_DISABLED_REASON` for the capture switch, distinct from `SITE_DISABLED_REASON`) — the
    same ordering `index/media_fetch.py` uses for image fetches. Never call a runner directly; the
    queue filter is not the guarantee.
  - `crawler.blocking.detect_block` judges the loaded page, so a login, CAPTCHA, paywall or age
    gate is abandoned with a recorded reason and never worked around (ADR 0007); DRM is detected,
    never decrypted. robots.txt and the per-site delay apply before the browser opens.
    **`html` is capped at 50,000 characters (`capture.mjs` `HTML_LIMIT`) before it reaches
    Python** (message-size only), but `classify_age_gate`'s "gate vs. overlay" link count has to be
    the whole document's or a normal page can be undercounted into a false gate (site id 12,
    issue #78) — `capture.mjs` counts `<a href>` before truncating and ships it as `linkCount`;
    `detect_block`/`classify_age_gate` take it as `link_count` instead of recounting on the
    truncated string. The HTTP crawler path passes the full body and no override.
  - **One click on the page's own play control, per site, off by default** (ADR 0016 amendment
    2026-09-18, issue #78). `catalog.Site.capture_play_click` is the operator switch (default
    False, admin-editable, off the `SitePolicy`/`seed_sources` path like `capture_enabled`).
    **The whole rule lives in `marceline/capture/click.py` and is pure**: `capture.mjs` describes
    elements, `choose_click_target` picks at most one, the browser clicks the index it is handed.
    Never add a clause to the browser side. An element is admitted only when it does not navigate,
    sits in the page's own player, is drawn ≥ 24x24 px, comes from a frame the embed allowlist
    permitted, reads as a play control, and carries **no** `FORBIDDEN_TOKENS` token (consent, age,
    login, subscribe, pay, download, share, ad, close, dismiss, **skip**) on itself, its text, its
    ARIA label or any ancestor — which is why **ad skipping is structurally impossible** and a
    pre-roll still ends the visit with nothing. The click is offered only *after* `detect_block`,
    so a gate is abandoned before any interaction, and exactly once: `ClickPolicy` spends a budget
    of 1, `runner._talk` has no loop, `capture.mjs` counts too. Still empty afterwards →
    `diagnosis.NO_PLAYER_AFTER_CLICK` (scanned once). Never a second click, keyboard input or
    scrolling trick.
  - **The browser sends the approved age self-declaration cookies** of `Site.age_confirmation`
    (same field, same `catalog.sources.parse_age_confirmation` as the HTTP crawler, which refuses
    session/login/anti-bot cookie names), read fresh per visit and scoped to the site's domain.
    They are the only cookies it ever holds. An age *verification* provider is unchanged: triage
    excludes the site, `detect_block` abandons the visit. Approving a site is a data change, never
    a code change.
  - The click is recorded on `capture_run.diagnostics.click`: allowed / chosen / performed, the
    candidate count, the kind, the **selector shape** (tag, ARIA role, ≤ 3 identifier tokens) and
    whether a player existed afterwards. **Never the element's text, ARIA label, title or a URL** —
    those are read only to refuse an element. `capture_worker --refusal-report` sums it up.
  - **Every request the page makes is asked for** (issue #78, ADR 0016 amendment 2026-09-17):
    `capture.mjs` holds it until `requests.RequestPolicy.refusal` answers — decoration types
    refused, subframes only from the site / enabled sites / its `SiteHost`s, everything else only
    if its host's robots.txt allows it. Never let the browser send a request past that check.
    Stylesheets are allowed on purpose (players need them). `diagnosis.no_video_reason` (pure)
    names why a page gave no video; `unsupported_media` is the only retryable one.
    `BROWSER_CAPTURE_ENGINE` = `chromium` (default) | `firefox` | `chrome`;
    docs/research/browser-capture-no-video.md has the engine and frame-parity measurements.
  - Output: a `Media(kind=capture)` row plus `FrameHash` rows written through
    `matching.media.store_hashes`, which re-checks the gate. `matching.timeline` orders capture
    frames like a clip. Per-capture cost (wall time, bytes, requests) lands in
    `BrowserCaptureCandidate.capture_run`; a worker skip there is also what makes "scan once" work.
  - **A refusal is reconstructable, never just a count** (issue #78 amendment 2026-09-18).
    `RequestPolicy.host_refusals()` stores, per host, its refusal reason, request count and the
    path *shapes* involved (`requests.path_pattern` — never a full URL) in
    `capture_run.diagnostics.refused_hosts`, for every reason that names a host worth reviewing
    (embed-not-allowlisted, robots-disallowed, robots-unavailable). A visit that never opens a
    browser at all — the page's own robots.txt (`requests.page_robots_refusal`) or a
    safety/availability deny (`guard.capture_denial_detail`, page id + rule, never the page's text
    or URL) — is stored on the separate `BrowserCaptureCandidate.refusal_detail` field instead of
    `capture_run`, so it stays retryable rather than looking "scanned once". `capture_worker
    --refusal-report` answers "which host blocked us" from stored rows alone, no browser needed,
    and marks a host already covered by the embed allowlist rule. `diagnosis.no_video_reason` now
    names an un-allowlisted embed host too, as the last resort after every more specific reason.


- Ground truth: `ReferenceLeak` holds proposed/confirmed/rejected reference/page decisions.
  `matching.leak_scoring` is pure; `matching.leaks` ranks and persists suggestions, preserving
  human decisions in both leak and match tables. Duration-only candidates need a creator link
  (a link to any of the original's creators counts) and a difference ≤1.5 s; matches and
  cluster/mirror evidence admit clips at any duration.
  Title similarity is display-only. Weights: strong 1.0, weak 0.9, cluster/mirror 0.8,
  exact duration 0.5 (maximum, not sum). Strong current matches propagate cluster-source
  proposals to active members; stale proposals are removed, human decisions preserved.
  Media claims prioritize expiring URLs, then pages linked to any tracked creator, then a page's
  poster over its hover frames, then ordinary media (issue #9).
  CLI: `suggest_leaks --reference ID --creator NAME --limit-per-reference 5 --dry-run` (all flags
  optional). Dry runs write nothing; `rank_candidates` is read-only.
- `matching.m2.m2_report` / CLI `m2_report --creator NAME --max-distance 36` measures reference/page
  pairs: confirmed leaks union confirmed matches, found strong / weak / linked (a confirmed leak
  without its own match that shares an active cluster with a strong match for the same reference),
  recall for strong, strong+weak and strong+weak+linked (the M2 exit check uses the last), rejected
  matches divided by all reviewed matches, outstanding review counts and data-backed miss reasons via
  `HashIndex`.
  No report writes. Admin leak review uses text links only, never embedded media. See ADR 0010
  “Ground truth and M2 report” for weights, denominators and small-sample caveats.

## The customer case file (ADR 0025)
- `matching.LeakCase` (one per `Reference`) + `matching.CaseFinding` (one per page of it) are the
  customer's case: **one case, many URLs**, the shape `index.SafetyIncident` uses — reused as a
  shape, never as that table. A re-upload is a new finding on the **same** case, and a case that
  was fully removed **reopens** (`reopened_at`, `reopen_count`). There is no closed state: the
  operator defined handled as "removed *and* not re-uploaded anywhere", so the removals log is
  living state, not an archive.
- **These tables store observations, never derivations.** Only what no other table records: when a
  transition was *first* seen (`discovered_at`, `verified_at`, `removed_at`, `reappeared_at`). A
  stamp is written once and never moves. `Page.availability_checked_at` is the **last** check and
  keeps moving — never present it as a removal date.
- `marceline/app/cases.py` owns the read model and `sync_cases`, which reconciles **on read**,
  idempotently, one transaction per case, and only for originals the caller's organisation owns.
  It lives in the project package because it needs `marceline/app/safety.py` and `matching` may
  not import `marceline`. `CASE_SYNC_ON_READ` (default on) switches reconciliation on read; off
  (ADR 0028: the public cache must not write on read), views read `stored_cases` — the last sync's
  state, filtered through today's visibility — and `manage.py sync_cases` (`sync_all_cases`, every
  organisation) keeps them current; the scheduler job `sync_cases` runs it every 3 h right after
  `match_references`. `_sync_case` takes a
  transaction-scoped advisory lock per original before reading, so concurrent readers and the
  sync command never create two cases or stamp a deleted one — never `SELECT … FOR UPDATE` on the
  `Reference` row, which matching holds for a whole run.
- Endpoints: `GET /api/v1/app/findings`, `/api/v1/app/cases`, `/api/v1/app/cases/{original_id}`,
  `/api/v1/app/removals`. Same rules as the originals view: scoped by organisation, 404 (byte-
  identical to an unknown id), safety-flagged pages never listed *or counted*, no media field.
- **Every value is a machine-readable code — the UI is bilingual and owns the words.** A German or
  English sentence in a customer payload is a bug. Timeline steps are
  `{step, status: done|pending|unavailable, at, on, reason, blocked_by_issue}`, always all six
  customer steps in order (`confirmed`, the three notice steps, `removed`, `rechecked`).
- **Confirmed only, our step dated to the day** (ADR 0025 amendment 2026-09-17). Customer views read
  through `originals.confirmed_findings_for`; an unconfirmed page is no finding, count, case or
  removal. `confirmed` carries `on` (a date), findings and removals `confirmed_on`, the case
  `opened_on` / `reopened_on`. Never add `discovered_at`, `verified_at`, `first_seen`, `opened_at`
  or any other stamp that would let a customer measure review latency.
- **Say what cannot be known; never invent it.** `notice_prepared` / `notice_sent` /
  `notice_acknowledged` are `unavailable` (#33 and #46 are open), and
  `not_available` names the absent facts as codes (`cooperation_history`,
  `reupload_watch_coverage`, …; hoster metadata and jurisdiction are per finding since ADR 0027).
  Never ship an empty placeholder object for them — a placeholder later reads as data.
- A removals entry needs **both** `verified_at` and `removed_at`; `handled` also requires that
  nothing else of the same original is up. A page nobody confirmed that vanished is not a takedown.
- **A case file never changes owner** (ADR 0045, issue #85). It hangs off the `Reference`, which
  moves when `ownership.reassign` moves an original, so the move deletes the case and its findings
  in the same transaction and the next `sync_cases` rebuilds one for the new owner from the match
  rows. What does not survive a move is the previous owner's own record: `opened_at`, the reopen
  history and every per-finding `verified_at` / `removed_at` / `reappeared_at` stamp.
- A finding that stops being visible (rejected, or newly safety-flagged) is **deleted**, and the
  case goes with its last finding: an empty case with `reopen_count = 2` would itself tell a
  customer something is hidden from them.

## Hashing a customer upload (issue #59, ADR 0044) — off by default
- **`matching/cloud_hash.py` is pure and Django-free**, so the process that opens a customer's
  uploaded video can be started without database credentials. It decides four things and each is a
  test: the child's environment (`child_environment` — `PATH`, a locale, nothing else), the limits
  (`rlimits` — address space, CPU, open files, and **`RLIMIT_FSIZE = 0`**, so the decoder cannot
  create a file at all, which is what makes "no frame ever leaves the worker" a property of the
  process), the ffprobe allowlist (`probe_command`, `parse_probe`, `refusal` — container format,
  video codec, pixel count, duration, stream count) and the two argument lists, asserted element by
  element. **Never remove `-protocol_whitelist file`** from either: a crafted playlist is how
  ffmpeg is turned into a fetcher or a local-file reader, and never put a customer-supplied string
  into an ffmpeg argument — the only variable part is the spool path, which our side generates.
- **`matching/cloud_decoder.py` is the `python -m` entry point** (`-E -s`, not `-I`, which would
  stop `-m` putting the package root on the path). JSON spec in on stdin, one JSON document out on
  stdout, and **no Django import** — a test reads the file and asserts it.
- **Progress crosses the containment boundary as two integers** (ADR 0044 amendment 2026-09-18).
  The customer's screen shows how far the fingerprinting has come, and the numbers behind it come
  back over the pipe the child already has rather than through anything new: the decoder writes
  JSON **lines** on stdout — `{"progress": {"frames": n, "total": m|null}}` while it hashes, then
  its one answer document — and `cloud_hash.parse_progress` reduces each line to two integers,
  coerced on arrival. A line that cannot be reduced to two integers is dropped. So the only things
  that ever come out of the process holding the customer's video are the hashes and a pair of
  counters, and there is no route by which a file name, a path or a decoder's complaint about the
  bytes could reach a log or a database row. The child still holds **no database credential** and
  the parent still never opens the file.
  - The child's **stderr is not read at all** any more (`DEVNULL`): a decoder's error text can
    quote the bytes it choked on, and the parent writes to logs and to the database.
  - `hash_file` therefore drives the child with `Popen` and its own deadline instead of
    `subprocess.run`, and kills the whole **process group** on a timeout — which also reaps ffmpeg,
    which the old path did not. The reader keeps only the tail of an incomplete line, so the
    progress stream costs no memory and the long answer document is simply appended to.
  - `expected_positions(duration_s, fps)` turns the duration ffprobe read into the denominator, or
    `None` when there is none: the sample grid is one picture every `1/fps` seconds, so a file
    whose real length differs from its declared one ends a little above or below the estimate and
    the screen clamps its bar. **Never invent a denominator** and never present either number as a
    time — ADR 0044 §7 and `frontend/src/i18n/wording.test.ts` ban a duration promise, and this is
    the screen that would want one.
- **The recipe is not new.** The decoder calls `fingerprint_video`, so a cloud-fingerprinted
  original carries `PIPELINE` and is bit-comparable with a CLI or `add_reference` one;
  `matching/tests/test_cloud_hash.py` generates a clip with ffmpeg at test time and asserts the two
  documents are equal, hash for hash. ADR 0022 is satisfied because it really is the same recipe on
  a different machine, as `add_reference` and the CLI already are.
- **No crop grid (ADR 0039) and no embedding sketches (ADR 0037) from an upload in v1**: 256 crops
  per moment is two orders of magnitude more work on a box with one core to spare, and the encoder
  would need a 45 MB model inside a container that deliberately has no network. The job row records
  the recipe that ran, so a second job kind can be added later without changing an interface.
