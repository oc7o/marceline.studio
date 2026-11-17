# 0022 — The server's frame sampling grid, and `ffmpeg-rgb512-pdq-v1`

Status: accepted (2026-09-16)

Extends ADR 0010 (creator references) and ADR 0019 (the browser client and its tag). Issue #70.

## Context

ADR 0019 measured, in a real browser, that a browser-hashed original and a CLI-hashed one are a
pooled median of **100 bits** apart as stored — 0 of 480 rows identical, 72 % past the match
threshold of 31 — and found the cause: the two sides hash **different moments**. It then explicitly
left the fix to a later decision, because moving either grid is a pipeline change.

The operator decided on 2026-09-16 that **the server is the side that is wrong**, and confirmed the
source videos still exist, so re-fingerprinting is possible.

It is wrong in a specific, checkable way. ffmpeg's `fps` filter assigns each input picture to the
output slot `round(t × fps)` and emits the **last** picture that fell into a slot. With the default
`round=near` the bucket for slot k is `[k−0.5, k+0.5)`, so slot k receives the picture just before
k + 0.5 and is then labelled `pts_time = k`. Everything downstream — `timestamp_s = frame_index /
fps`, the match's `best_reference_timestamp_s`, the mm:ss an operator reads in review — described a
moment about half a second earlier than the picture that was actually hashed.

## Measurement

Synthetic ffmpeg clips only, generated at test time. Every decoded picture was scaled exactly as
the pipeline scales it, PDQ-hashed, and paired with its real `pts_time` from ffprobe; each
candidate recipe's output slot k was then matched (Hamming distance 0) against that ruler. Offsets
are `pts of the picture slot k received` − `k`.

| recipe | 30 fps | 24 fps | 25 fps | 30000/1001 | VFR clip |
|---|---|---|---|---|---|
| `fps=1` (`round=near`, the old recipe) | +466.7 ms | +458.3 ms | +480.0 ms | +471.6 ms | +433 ms |
| `fps=1:round=down` / `round=zero` | +966.7 ms | +958.3 ms | +960.0 ms | +972.1 ms | — |
| **`fps=1:round=up`** / `round=inf` | **0.0 ms** | **0.0 ms** | **0.0 ms** | −27.4 ms | **0.0 ms** |

- `round=up` makes the bucket `(k−1, k]`, so slot k is the **last decoded picture at or before k** —
  the grid rule `docs/research/fingerprinting-client.md` writes down, and the rule a browser's
  `video.currentTime = k` seek follows.
- **No drift.** Over a 120 s clip at 30 fps all 120 slots land at exactly 0.0 ms.
- **Residual: at most one frame period, and only where no picture falls on the integer second.**
  At 30000/1001 fps the pictures nearest k = 1 sit at 0.9676 s and 1.0010 s; `round=up` correctly
  takes the earlier one, a median of 27.4 ms and at worst 32.4 ms before k. There is nothing closer
  to hash: that *is* the picture on screen at k.
- A non-zero stream start is handled the same way by both recipes: the grid is relative to the
  stream's first presentation time, not to zero.
- `fps=rate:round=up` at a clip's own native rate returns exactly the passthrough frames
  (3600 of 3600 identical), so the parity harness's native-frame ruler is unchanged by this switch
  and the before/after numbers below are comparable.

## Decision

**Sample references with `fps=F:round=up`** (`matching.frames.FPS_ROUND`, used by
`iter_video_frames`). The rest of the recipe is untouched: `scale='min(512,iw)':-2`, PPM, RGB, PDQ
with eight dihedral variants, quality gate 50.

**The recipe's tag becomes `ffmpeg-rgb512-pdq-v1`.** `PIPELINE_V0 = "ffmpeg-rgb512-pdq-v0"` keeps
meaning exactly what it means today — hashes of the picture at about k + 0.47 s, stored under the
label k. Both validate in `from_json`, both import, both match; `PIPELINES_BY_KIND` still refuses a
photo tag on a video document and the other way round, and an unknown tag is still refused. The
photo recipe `pillow-rgb-pdq-v0` is not affected by any of this.

**Old references keep working, and which grid they are on is visible.**

- `Reference.sha256` is unique **per pipeline** (`reference_unique_sha256_per_pipeline`, migration
  `matching/0016`) instead of globally, so a re-fingerprint of a file imports *beside* the old row.
  Nothing is rewritten, reinterpreted or deleted; retiring the v0 row is a human action in the
  admin, taken after the new one is confirmed.
- `import_fingerprints` says `imported reference N (alongside reference M on
  ffmpeg-rgb512-pdq-v0)`, and `--compare` measures the grid gap against the old row instead of
  importing, naming both tags and saying that a large distance is expected there.
- `pipeline_label(tag)` renders a tag plus what it means for the grid. It is the admin *Pipeline*
  column (sortable, with the raw tag still in the sidebar filter) and a per-reference line plus a
  totals block in `reference_report`.

**Matching never pretends a v0 and a v1 reference are on the same grid.** They are two rows with
two tags and two sets of seconds, and `ReferenceMatch` is per reference, so a page matching both
shows up under both — which is the honest answer during the transition.
One rule had to change to keep that from being destructive: `find_shared_frames` compares
`SharedCandidate.sha256` and **never marks two references of the same file as sharing frames**.
Without it, the v0 and v1 rows of one original — half a second apart, and on slow-moving material
well within 31 bits — would declare each other's frames distributor common footage and delete the
evidence of *both* rows during exactly the window the operator needs them.

**`POST /api/v1/originals` still refuses a sha256 under any pipeline**, enforced in the endpoint
rather than by the constraint that no longer says it. Re-fingerprinting is an operator workflow on
originals nobody owns; a customer upload is a *claim*, and a second claim on a file somebody
already registered is precisely what must not slip through (ADR 0019).

**`matching.frames.sample_video_frames` is deliberately left on the default rounding.** That is the
page-media path: a `HashedFrame` there carries a position and no timestamp, a leaked preview clip
has its own unknown start and therefore shares no second-grid with anything, and changing it would
invalidate every stored `index.FrameHash` row and every `MediaReuse` link for no gain.

**The browser keeps `browser-rgb512-pdq-v0`.** Its grid was already right; what remains is pixels.

## Proof: the harness, re-run

`make browser-parity`, same six synthetic clips, same headless Chromium 153.0.8010.12, 480 hash
rows. **As stored — the same `frame_index` on both sides, which is what matching compares:**

| clip | codec | rows | identical | median | p90 | max | > 31 | > 40 |
|---|---|---|---|---|---|---|---|---|
| testsrc 480×270 25 fps | H.264 | 80 | 40 % | 2 | 4 | 4 | 0 | 0 |
| testsrc2 640×360 30 fps | H.264 | 80 | 40 % | 2 | 4 | 4 | 0 | 0 |
| testsrc 854×480 24 fps | H.264 | 80 | 15 % | 2 | 4 | 6 | 0 | 0 |
| testsrc2 1280×720 30 fps | H.264 | 80 | 0 % | 20 | 26 | 30 | 0 | 0 |
| testsrc2 640×360 30 fps | VP9 | 80 | 38 % | 2 | 4 | 6 | 0 | 0 |
| testsrc 640×360 25 fps | VP8 | 80 | 45 % | 2 | 4 | 4 | 0 | 0 |
| **all clips pooled** | | **480** | **30 %** | **2** | **18** | **30** | **0** | **0** |

Against ADR 0019's table for the same clips: pooled median **100 → 2**, max **140 → 30**, rows past
the match threshold **344 of 480 → 0**, past the loose threshold **292 → 0**, identical
**0 % → 30 %**.

**That is the honest success criterion met.** ADR 0019 measured that the two sides agree to a
median of 2 bits (max 22) when they hash the *same* picture; the as-stored distance has collapsed
to exactly that — median 2, and the only clip above single digits is the 1280×720 one at median 20,
max 30, which is the known canvas-downscale cost, now visible because the grid no longer swamps it.
The aligned table confirms the mechanism directly: the CLI side is now **+0 ms offset, 100 %
identical to the natively decoded picture at k**, where before it was a constant +458…+480 ms.

**Two things this does not claim.** Max 30 bits on the 720p clip is one bit under the match
threshold of 31 — comfortable is not the word for it, and it is why the browser tag stays separate.
And 30 % identical is not 100 %: the recipes are comparable now, not equal.

## Consequences

- **A migration ships with this change (`matching/0016`). `make migrate` from the main checkout and
  restart the workers** — worker processes keep the schema they started with.
- **The operator re-fingerprints the library.** 39 references, in batches, procedure in
  `client/README.md`. A re-run of `client/fingerprint.py` produces v1 files; they import next to
  the v0 rows; `match_references` runs; the old rows are retired in the admin once the new ones
  are confirmed. A reference whose source file is gone simply stays on v0 and keeps working.
- **During the transition a page can match twice**, once per grid, and both appear in the review
  queue. Human decisions on the v0 row are not carried over to the v1 row; this is one more reason
  to retire the old rows promptly rather than run both indefinitely.
- Reference `timestamp_s`, `best_reference_timestamp_s` and every mm:ss in admin and review are now
  the moment of the picture that was hashed, for v1 rows. For v0 rows they remain about half a
  second early, which is what the tag now says out loud.
- Page frame hashes (`index.FrameHash`) are untouched, so no re-crawl and no re-decode is needed.

## Alternatives rejected

- **`fps=…:round=down`** — named as a candidate in issue #70 and in
  `docs/research/fingerprinting-client.md`. **Measured and wrong:** +958…+976 ms, twice as far off
  as the default, because it makes the bucket `[k, k+1)` and the filter still emits its last
  picture. The research doc has been corrected.
- **A `select`-by-pts expression.** It can express the rule, but it needs `selected_n`/
  `prev_selected_t` arithmetic that drifts on non-integer frame rates, and it buys nothing over a
  one-word filter option that measures at 0.0 ms.
- **One `ffmpeg -ss` seek per sample.** Exact, but it is one process per sampled second (3 600 for
  an hour of video at 1 fps) and accurate seeking lands on the first picture at or *after* k — the
  opposite side of the boundary from the browser. Worse on both counts.
- **Changing the browser instead** (seek to where ffmpeg would have landed). It would have made the
  *client* implement a bug, left `timestamp_s` wrong on the server anyway, and ADR 0019 measured
  that `<video>` seeking is not reliably frame-exact (one run landed a full second early), so the
  client is the worse place to encode a precise grid.
- **Doing nothing and keeping the tags apart.** The grid offset is not only a browser-parity
  problem: it makes every stored reference timestamp wrong by half a second. The operator has the
  files, so the cost of fixing it is a re-run, not a loss.
- **Re-tagging or rewriting the existing v0 rows.** That is the one thing a pipeline tag exists to
  prevent. They keep their tag, their hashes and their matches.

## Amendment 2026-09-17: the sampling rate is part of a reference's identity

**Context.** Issue #73 and the matching review measured why single-poster leak pages are found by
one grid and missed by the other: a poster matches only when it lies within about a quarter of a
second of a sampled moment (distance ≈ 10 + 80 × offset in seconds on the live index), and the v0
and v1 rows of one file, half a second apart, each found 6 of 8 poster pages and together 8 of 8.
The review estimates 2 fps at about 85–100 % poster recall. Denser sampling is the fix, but
`Reference.sha256` was unique per *pipeline*, so a 2 fps re-run of a v1 original was skipped as
"already imported".

**Decision.** Identity is **(sha256, pipeline, frame_fps)**:
`reference_unique_sha256_pipeline_fps` (migration `matching/0018`, `nulls_distinct=False` so a
photo collection, which has no rate, stays unique per digest and pipeline). Chosen over a tag
suffix (`…-v1@2fps`) because the rate is already a column, every tag check (`VIDEO_PIPELINES`,
`ALIGNED_PIPELINES`, labels, admin filter) stays untouched, and the grid of a 2 fps v1 row really
is the v1 grid at twice the density: slot 2k is the picture at k.

- `import_fingerprint` / `add_reference` refuse only the same digest on the same pipeline **at the
  same rate**. `import_fingerprints` imports a denser file beside the existing row
  (`alongside reference M on ffmpeg-rgb512-pdq-v1 at 1 fps`) and inherits its cast as before.
- `--compare` across rates compares the moments both grids sample, keyed by timestamp: on the same
  machine and pipeline they are the same pictures, so parity is the check that the denser file is
  the same original, not a different decode.
- The client names a non-default rate in the file (`<name>.<sha256>.2fps.json`), so a 2 fps run into
  a folder of 1 fps files is new work, not a skip. It also no longer reads a multi-GB original a
  second time after decoding: `fingerprint_video(file_stat=)` checks size and modification time
  taken before the digest instead. Hardware decoding (videotoolbox) stays off until a parity run
  on the Mac shows it hashes the same pictures.
- Two rows of one file never mark each other's frames shared (`sha256` comparison), whatever
  their rate, exactly as for two pipelines.

**Not decided here.** Whether and when to re-fingerprint at 2 fps is the operator's call; the
procedure is in `client/README.md` ("Denser sampling"). 2 fps doubles reference hashes and the full
scan (measured 31 s for 580k hashes × 55k page frames at two FAISS threads, synthetic, in memory).
4 fps waits: the per-run reference hash snapshot (`matching.reference_hashes`, RE3) now keeps
memory at ~80 bytes per hash instead of Python tuples, but a 4 fps library quadruples scan time and
should be measured on the 2 fps one first.

**Also in this amendment: the browser recipe becomes `browser-rgb512-pdq-v1`.** `targetSize` floored
the scaled height (1280×723 → 512×288) and left odd heights of frames narrower than 512 alone, where
ffmpeg's `scale='min(512,iw)':-2` rounds to the nearest even height for every frame (512×290,
480×271 → 480×272; checked against ffmpeg). Hashes of such sources change, so the tag changes;
`browser-rgb512-pdq-v0` still validates, imports and matches. `make browser-parity` has not been
re-run for this change (it needs the browser download); the synthetic parity clips are 16:9 at even
sizes, whose geometry is unchanged.

**Correction 2026-09-17 (issue #73, `docs/matching/issue-73-v1-reference-recall.md`).** The
recall estimate above — "distance ≈ 10 + 80 × offset in seconds", "2 fps at about 85–100 % poster
recall" — is not supported by the two grids now in the database. Measured over both files that
carry a v0 and a v1 row, two pictures ~0.47 s apart are a **median 96–104 bits** apart (p10 46–48),
and only **3.5–4.7 %** of them stay within the match threshold of 31; at 1.0 s apart it is
2.1–4.7 %. Denser sampling is still the right direction — the v0+v1 pair, effectively a 2 fps grid,
is what finds 10 of the 11 pages either grid sees — but *how* dense is open, because nobody has
measured the curve below half a second. Do that on one real original (8 fps, δ = 0.125…1.0 s)
before choosing between 2 fps and 4 fps; the numbers here decide nothing about 0.25 s offsets.
That investigation also found that the two grids of one file disagree in both directions (v1 loses
two single-poster pages and gains two), so the v0 rows of refs 1 and 8 stay until a denser v1 row
has been matched.

**Amendment 2026-09-18 (issue #73 closed): 2 fps is a per-original purchase, not the fix, and a
file's older grid is a sampling phase we keep.** The correction above asked what density is worth.
The grid-level form of that question is answerable with the rows we have: put a query picture at the
**midpoint** between two samples of a grid of spacing S — the worst case for that grid — and ask
how often the nearer sample is within 31 bits (`docs/matching/issue-73-v1-reference-recall.md` §5,
2,762–2,993 moments per line):

| grid spacing | worst-case offset | file A | file B |
|---|---|---:|---:|
| 4 s | ~2.0 s | 3.9 % | 0.9 % |
| 2 s | ~1.0 s | 7.0 % | 3.6 % |
| **1 s (today)** | ~0.5 s | **15.6 %** | **8.9 %** |

Each halving roughly doubles the worst case over that range. Below half a second our own rows
cannot reach, but `docs/matching/second-signal.md` §4.2 measured it on independent material (119
anchors, single sample): P(≤ 31) = 0.07 at 1.0 s, 0.23 at 0.5 s, 0.42 at 0.25 s, 0.59 at 0.125 s —
so the factor per halving decelerates from 3.3 to 1.8 to 1.4. Carrying that shape onto our two
files (which are 2–4× harder at the same offset) gives **~28 % / ~16 % for 2 fps and ~40 % / ~22 %
for 4 fps** (worst case throughout — a query exactly between two samples; the average over a
random moment is higher). A 2 fps row is therefore worth about **1.8× the hits** on the poster-only
page shape, which is worth buying per original — and the 85–100 % this ADR once estimated is out of
reach,
because two ceilings do not move with fps: cost (8 fps is ~5 M hashes and ~0.9 GB in the per-run
snapshot for today's 44 originals, growing per customer) and **geometry** (10 of the 46 near-miss
pages of those two files are 36–50 bits from the nearest moment we hold even with the better of two
phases — the re-frame, whose remedy is ADR 0039). What else remains for the poster-only shape is a
second picture on the page: site adapters that harvest every thumbnail a page exposes.

Also revised: the earlier "median 96–104 bits at ~0.47 s" paired the two grids **by row order**,
which misaligns after the first quality-dropped second. Joined on `frame_index`, as matching
compares them, it is a median of **68** bits (file A, p10 30, 12.0 % within 31) and **78** (file B,
p10 38, 5.8 %).

**The rule this fixes in place.** A file's older grid is not legacy to be retired on principle; it
is a second sampling **phase**, 0.47 s from the current one, and phase diversity is the only
coverage of an arbitrary poster moment that is already paid for. `reference_report` now prints,
per file, which of its rows matches pages no sibling matches (*"a row with pages of its own is why
its file keeps both grids"*) — on today's index reference 8 does and reference 1 does not. That
report answers it for the pages already found; **retire a row when its file has a denser v1 row
that has been matched**, not because today's pages happen to be covered, because the phase also
covers the pages the crawler has not found yet.

## Amendment 2026-09-18 (issue #29): v1 measured at last, and the stale frame it found

The passage above says `make browser-parity` "has not been re-run" for `browser-rgb512-pdq-v1`.
It has now, five times, same six synthetic clips, same headless Chromium 153.0.8010.12, 480 hash
rows per run. Two things came out of it.

**The recipe is fine.** After the fix below, three consecutive runs produced this table, which is
the v0 table of this ADR to the digit:

| clip | codec | rows | identical | median | p90 | max | > 36 | > 40 |
|---|---|---|---|---|---|---|---|---|
| testsrc 480×270 25 fps | H.264 | 80 | 40 % | 2 | 4 | 4 | 0 | 0 |
| testsrc2 640×360 30 fps | H.264 | 80 | 40 % | 2 | 4 | 4 | 0 | 0 |
| testsrc 854×480 24 fps | H.264 | 80 | 15 % | 2 | 4 | 6 | 0 | 0 |
| testsrc2 1280×720 30 fps | H.264 | 80 | 0 % | 20 | 26 | 30 | 0 | 0 |
| testsrc2 640×360 30 fps | VP9 | 80 | 38 % | 2 | 4 | 6 | 0 | 0 |
| testsrc 640×360 25 fps | VP8 | 80 | 45 % | 2 | 4 | 4 | 0 | 0 |
| **all clips pooled** | | **480** | **30 %** | **2** | **18** | **30** | **0** | **0** |

`--fail-over 36` exits 0. The thresholds in the table are no longer typed into the harness: it
reads `STRICT_MAX_DISTANCE` and `LOOSE_MAX_DISTANCE` out of `matching/references.py`, which is why
the column says 36 where the older tables say 31 — the constant was raised by the ADR 0015
amendment and the harness had not noticed. The 854×480 clip is the one whose geometry the v0 → v1
change actually moved (512×287 → 512×288, `targetSize` rounding); its numbers are unchanged, so
this ADR's "the synthetic parity clips are 16:9 at even sizes" was the right conclusion from a
slightly wrong premise.

**A stale frame, one position in sixty.** The first two runs each carried exactly eight bad rows —
one sampled position, all eight orientations — 60–64 bits in the first run, 100–104 in the second,
and on a different clip each time. The aligned table said what happened: browser offset max
**−1000 ms**, distance 2 bits to the frame the browser actually hashed. It hashed the *previous*
second, correctly. `seeked` reports that the seek finished, not that its picture has reached the
compositor, and `canvas.drawImage(video)` draws what the compositor has; under load the old picture
is still there. `fingerprintClient.ts` (`seekTo`) now arms `requestVideoFrameCallback` before the
seek, skips a presented frame more than 0.25 s before the target, and caps the wait at 250 ms after
`seeked` so a seek landing inside the picture already on screen — which presents nothing — cannot
hang. Chrome and Edge have the callback; where it is missing nothing changes.

**The tag stays `browser-rgb512-pdq-v1`.** A tag names a recipe, and the recipe is untouched: a
correctly presented frame hashes exactly as before. What changed is that the client no longer
sometimes hashes a different picture than the one it recorded a timestamp for — a bug, not a grid.
Nothing already imported needs re-fingerprinting for this; a browser-uploaded original from before
today may simply carry a few moments whose hash belongs to the second before them.

**Why it matters beyond the harness.** These clips are ten seconds long. At one position in sixty,
a three-hour upload would carry roughly 180 moments hashed off-by-one-second — silently weaker
evidence, invisible to everyone, and exactly the failure the tag separation was meant to make
measurable rather than assumed.
