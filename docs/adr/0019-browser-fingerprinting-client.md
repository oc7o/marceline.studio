# 0019 — The browser fingerprinting client and its own pipeline tag

Status: accepted (2026-09-16)

Extends ADR 0010 (creator references) and ADR 0017 (PDQ in the browser). Issue #29 stage B.

> **Amended 2026-09-16 by ADR 0022 (issue #70).** The frame-choice defect this ADR measured and
> deliberately left open has been fixed **on the server**, which is the side that was wrong:
> `iter_video_frames` samples with `fps=F:round=up`, so slot k is the picture at k, and the recipe
> is tagged `ffmpeg-rgb512-pdq-v1`. Re-running `make browser-parity` over the same six clips takes
> the as-stored pooled distance from **median 100 bits to median 2**, max 140 → 30, and rows past
> the match threshold from **344 of 480 to 0**. The "Measurement" section below is the *before*
> picture and is kept as the record of it; every statement in it about `ffmpeg-rgb512-pdq-v0`
> remains true of references still carrying that tag. `browser-rgb512-pdq-v0` is unchanged and
> still a separate tag — the remaining gap is the canvas downscale (median 2 bits, max 22 at
> 1280×720), not frame choice. See `docs/adr/0022-frame-sampling-grid.md`.

## Context

Stage A proved that the browser's PDQ hasher is bit-identical to the server's *for identical
pixels* (ADR 0017). What was missing is everything a creator can actually use: decoding a video in
the browser, a page to do it on, and an endpoint to receive the hashes. The operator went looking
for the tab and it was not there.

The open question was never the hash. It is **which pixels reach the hasher**. The server and the
CLI run `ffmpeg -vf fps=F,scale='min(512,iw)':-2` → PPM → RGB (`ffmpeg-rgb512-pdq-v0`). A browser
cannot run that: it decodes with the platform decoder and scales on a canvas, whose filter and
YUV→RGB matrix are not swscale's. If the two disagree even slightly, nothing fails loudly —
matches just get weaker, and it looks like a matching problem rather than a build problem.

## Measurement (real browser, 2026-09-16, issue #28)

Measured by the parity harness in `frontend/parity` (`make browser-parity`), which runs the app's
own `fingerprintFile()` — the code the "Add original" panel calls — in a real browser and compares
the result with `matching.fingerprint.fingerprint_video` on the same file. This **supersedes the
ffmpeg-emulated estimates** this ADR carried before; they were optimistic by two orders of
magnitude.

**What was actually run.** Playwright 1.63 headless **Chromium 153.0.8010.12** (Chrome for Testing
headless shell, `HeadlessChrome/153.0.8010.12`) on Linux x86_64. Six synthetic clips, 10 s each,
sampled at 1 fps: ffmpeg `testsrc` and `testsrc2` at 480×270/25, 640×360/30, 854×480/24 and
1280×720/30, yuv420p — no real or pirated media. 60 sampled positions × 8 orientations = 480 hash
rows. Codecs the browser reported and decoded: **H.264 in MP4, VP8 and VP9 in WebM**. It reports
**no HEVC** (`canPlayType` = "no") and claims AV1, which was not tested. **Firefox and WebKit were
not run** (each is a further browser download; see Cost in `frontend/parity/README.md`).

**As stored — the same `frame_index` on both sides.** This is what matching compares:

| clip | codec | rows | identical | median | p90 | max | > 31 | > 40 |
|---|---|---|---|---|---|---|---|---|
| testsrc 480×270 25 fps | H.264 | 80 | 0 % | 26 | 42 | 46 | 26 | 16 |
| testsrc2 640×360 30 fps | H.264 | 80 | 0 % | 120 | 138 | 140 | 80 | 80 |
| testsrc 854×480 24 fps | H.264 | 80 | 0 % | 40 | 54 | 56 | 62 | 36 |
| testsrc2 1280×720 30 fps | H.264 | 80 | 0 % | 116 | 126 | 130 | 80 | 80 |
| testsrc2 640×360 30 fps | VP9 | 80 | 0 % | 120 | 136 | 140 | 80 | 80 |
| testsrc 640×360 25 fps | VP8 | 80 | 0 % | 18 | 34 | 38 | 16 | 0 |
| **all clips pooled** | | **480** | **0 %** | **100** | **124** | **140** | **344** | **292** |

**It is not 0, and it is far worse than the emulated estimate predicted.** Not one row out of 480 is
identical, the pooled median is 100 bits, and 344 of 480 rows (72 %) are past the match threshold of
31; 292 (61 %) are past the loose threshold of 40.

**Why: the two pipelines sample different moments.** Hashing every decoded frame with the CLI recipe
gives a common ruler. Against it:

| side | offset of the frame it hashed from the nominal second | distance to that frame |
|---|---|---|
| browser | **+0 ms** (median; max +80 ms) | median 2, max 22 |
| CLI | **+458…+480 ms** (constant per clip) | 0 — 100 % identical |

ffmpeg's `fps` filter emits, for output slot *k*, the input picture at about *k* + 0.47 s and labels
it `pts_time = k`; confirmed independently with ffmpeg's own per-frame checksums (`fps=1` output
frame 0 of the 30 fps clip is input frame n=14, `pts_time` 0.4667). The browser's
`video.currentTime = k` seek lands on exactly *k*. So the two sides hash different pictures, and
`timestamp_s = frame_index / fps` is accurate for the browser but about half a second early for the
CLI and the server. On slow-moving `testsrc` that still leaves 18–40 bits; on fast-moving `testsrc2`
the hashes are unrelated (median ~120, i.e. noise).

**The decoder and the canvas are not the problem.** When both sides hash the *same* picture they
agree to a median of 2 bits and a maximum of 6 — across H.264, VP8 and VP9 alike. The one exception
is the 1280×720 clip at 20–22 bits, where the canvas downscale to 512 px does the most work and its
filter differs most from swscale's. Bit-identical it is not; comparable it would be.

**Reproducibility.** Three consecutive Chromium runs over the same clips produced byte-identical
hashes (480/480 rows). An earlier run differed on the VP8 clip, where one seek landed a full second
early — so `<video>` seeking is not guaranteed frame-exact, and a browser fingerprint should not be
assumed reproducible to the bit across machines or runs.

## Decision

**The browser gets its own pipeline tag: `browser-rgb512-pdq-v0`.** It is stored in
`Reference.pipeline` and validated by `matching.fingerprint.PIPELINES`, so a browser-hashed
original is never silently treated as bit-comparable with a CLI-hashed one. Matching thresholds
depend on knowing which recipe produced a hash. `matching/tests/test_fingerprint.py` asserts the
two tags differ, that both import, and that an unknown tag is still refused.

**The browser recipe is: decode → canvas downscale to `min(512, width)` with an even height → RGB →
`hashDihedral`.** The downscale is kept: measured against the same decoded picture it costs a median
of 2 bits (max 6), rising to 20–22 only at 1280×720. Hashing the native-resolution frame instead was
never measured in a real browser — the emulated study preferred the downscale (median 6 vs 0) and
nothing has overturned that, but it is an estimate, not a measurement. Frames below quality 50 are
dropped on both sides.

**Frame choice is the open defect, not the pixels.** The client follows `t = k / fps` and stores
`timestamp_s = frame_index / fps`; the measurement above shows the *server* does not — its `fps`
filter hands back the picture at about `t + 0.47 s` under the label `t`. This ADR does not change
either side: doing so is a pipeline change that invalidates every reference hashed so far, and it
needs its own decision (candidates: `select` by pts or `fps=…:round=down` on the server, or seeking
to the matching instant in the browser). Until then the two recipes stay separate and are not
compared.

> **Resolved by ADR 0022 (issue #70), 2026-09-16.** The server moved, with `fps=F:round=up` under
> the new tag `ffmpeg-rgb512-pdq-v1`. Of the candidates listed here, `round=down` was measured and
> is *worse* (+958…+976 ms); `round=up` measures 0.0 ms. The two recipes now sample the same
> moments and are still tagged separately, because the pixels still differ.

**Decode on the page, hash in a Worker.** `fingerprintWorker.ts` computes the eight dihedral hashes,
which is the CPU-bound part; the page decodes, because an `HTMLVideoElement` exists only there.
WebCodecs `VideoDecoder` would be faster but needs a demuxer to feed it encoded chunks — a new npm
dependency (Mediabunny) that this change deliberately does not add. `<video>` uses the same platform
decoders, so codec coverage is identical; only the seek per frame is slower.

**`POST /api/v1/originals`** accepts exactly the JSON `matching.fingerprint.to_json` produces
(v2: `creator` is a list, primary first), validates it with `from_json`, and imports through
`matching.references.import_fingerprint` + `matching.creators.set_creators`. Staff session auth,
like the rest of the review API. `Reference.client["source"]` is `browser`.

**An upload is a claim, not proof of ownership.** Server-side:

- the row is bound to the uploading account (`Reference.uploaded_by`, null for CLI imports);
- the same file twice is refused (unique sha256), with the existing original named;
- an upload whose dihedral-0 hashes are ≥ 50 % identical to an original **another** account (or an
  operator CLI import) already registered is refused with 409 and both rows named, because a
  re-encode of the same video has a different digest but nearly the same hashes;
- the same account may re-register its own material from another master;
- the endpoint writes one `Reference` and its hashes and **nothing else**: no match, no leak, no
  takedown, and `mark_shared_frames` is left to `match_references`, which recomputes it anyway.

**Deliberately left to human review** (not solvable server-side, per
`docs/research/fingerprinting-client.md`): identity and account verification before matches are
shown; deciding who holds the rights when two accounts claim overlapping hashes (the server freezes
the second claim, it does not adjudicate it); cross-checking against a creator's public trailers;
and the creator-signed notice plus human review before any takedown. A client under the user's
control cannot be made tamper-proof, so none of this belongs in the client.

**Counters.** `codec_unsupported` (the browser refused to decode at all) and `client_failed` (every
other local failure) are recorded as `matching.ClientEvent` rows through
`POST /api/v1/originals/client-events`, readable in the Django admin filtered by kind. They carry
the decoder message, container and codec and the account — never a file name, size or path. These
counts are the input for deciding cloud fingerprinting (issue #59,
`docs/research/cloud-fingerprinting.md`), which is exactly what that research asked for.

## Consequences

- **A migration ships with this change** (`matching/0014`): `Reference.uploaded_by` and the new
  `ClientEvent` table. Apply it from the main checkout (`make migrate`) and restart the workers.
- Browser-hashed and CLI-hashed originals of the same video will both exist and are **not**
  comparable bit for bit. Calibration and any future threshold work must split by `pipeline`.
  `import_fingerprints --compare` already reports a pipeline mismatch.
- **A browser-uploaded original is weaker evidence than a CLI-imported one, and the measurement
  says by how much.** Indexed page frames are hashed by the server's ffmpeg path, so they sit on
  the CLI's ~+0.47 s grid. A browser-hashed reference samples half a second away from it, which on
  fast-moving material puts the hashes 100+ bits apart — no match at 31, and none at the loose 40
  either. Matching still works through *some* frames (a leak clip and an original rarely share a
  sampling grid anyway, and `match_references` compares every page frame against every reference
  frame), but the per-second correspondence the timeline check relies on is degraded. Closing the
  grid gap is the highest-value follow-up from this measurement; until it is closed, the CLI stays
  the better path for an original that must match reliably.
- The CLI stays the supported path for large batches and for anything the browser will not decode;
  the Originals screen keeps showing the `add_reference` command next to the upload panel.
- Hashing in the browser is slow: one seek per sampled second plus 8 PDQ hashes. A feature-length
  original is minutes of work with the tab open. Throughput on a real creator laptop is still
  unmeasured.
- SHA-256 is computed in TypeScript (`lib/sha256.ts`), because WebCrypto's `digest` is one-shot and
  multi-gigabyte originals cannot be held in memory. It is slower than a WASM hasher and is the
  first thing to replace if digesting dominates.

## Alternatives rejected

- **Keeping `ffmpeg-rgb512-pdq-v0` for browser uploads, or folding the two tags into one.** The
  real-browser measurement settles this: the frames are literally not the same frames (a ~0.47 s
  grid offset, pooled median 100 bits, 0 of 480 rows identical). Reusing the tag would hide that in
  the one field that exists to record it. The tags stay separate until the sampling grids are made
  to agree — and that change would itself need a new tag.
- **A tolerance that declares the pipelines equal.** Distances are already in the same range as the
  match threshold; a tolerance would silently degrade matching instead of being visible.
- **Hashing the native-resolution frame** (the research doc's canonical-pipeline idea). Measurably
  worse against today's references (median 6, max 26). It only pays off once the *server* also
  moves to a luma-plane pipeline, which is a separate decision.
- **WebCodecs + Mediabunny now.** Faster, but a new dependency and a demuxer to keep in sync; the
  slow path works and proves the product question first.
- **Uploading the file** (cloud fingerprinting, #59). Contradicts the promise this feature exists
  for; ruled out for the MVP by `docs/research/cloud-fingerprinting.md`.

## Amendment 2026-09-17: customers add originals in the app (issue #29)

The page above served staff. Customers now get their own: **"Add an original"** at
`/app/originals/new`, posting to **`POST /api/v1/app/fingerprints`** (`marceline/app/fingerprints.py`).
The video still never leaves the computer, and the document is still exactly what
`matching.fingerprint.to_json` writes — the browser now writes **format version 3** with
`kind: "video"`, like the CLI. Import path unchanged: `from_json` → `import_fingerprint` →
`ownership.register` (`Via.CUSTOMER`). Decided on top of the staff endpoint:

- **Recipe.** Only `browser-rgb512-pdq-v1`, videos, all eight orientations, at `SUPPORTED_FPS`
  (1 and the 2 fps pilot; the app's rate is the one constant `DEFAULT_FPS` in
  `frontend/src/lib/videoFingerprint.ts`). The tag stays separate from `ffmpeg-rgb512-pdq-v1`: as
  measured in ADR 0022, same moments, pixels a median of 2 bits apart, max 30 at 1280×720.
- **Size.** At most `MAX_POSITIONS` = 10,800 sampled positions (3 h at 1 fps); the client refuses a
  longer video before hashing. That document is about 12.5 MB of JSON, so this one route reads
  bodies up to 16 MiB; every other route keeps Django's 2.5 MB.
- **Owner** = the caller's organisation, never a request field.
- **Rights declaration required.** "I own, or am authorised to enforce, the rights in this video"
  (and its German version), versioned in `backend/marceline/app/rights_confirmation.json`; the
  frontend test fails when its strings drift from that file. Stored as `marceline.RightsConfirmation`
  (original, organisation, account, wording version, language, time). A claim, not proof.
- **Idempotent.** A file this organisation already owns, under any recipe or rate, answers 200 with
  that original and imports nothing; a first registration answers 201.
- **Claims on other material stop at a human.** Same sha256 owned by another organisation or by
  nobody (an operator import), or ≥ 50 % of the dihedral-0 hashes equal to such an original: one
  409 `claim_needs_review`, byte-identical for all three cases, naming nobody. The details go to the
  application log only. (Duplicate detection now compares organisations, not accounts.)
- **Cast.** The customer types the rights holder and co-performers. A name equal to an existing
  creator's name (case-insensitive, unique) links it; aliases are never matched (they hold studio
  names). Any other name becomes a `catalog.Creator` with **`needs_review`**, which is not a crawl
  target, not linked to crawled pages and not used for discovery until staff mark it reviewed in the
  admin (merging a duplicate by hand first). No creator is merged automatically.
- **Rate limit** per organisation, rolling hour, in Postgres (`marceline.UploadAttempt`): 30
  registrations, 60 client events. Client events answer without totals.
- **Performance.** The screen is a lazy route; the client (Worker, PDQ WASM) is imported when the
  customer presses start. SHA-256 moved into the Worker and runs *beside* frame hashing (the page
  seeks, the Worker digests and hashes), so neither blocks the tab. Main chunk before/after: 566.5 /
  571.8 kB (171.6 / 172.4 kB gzip) — the add-original strings in two languages; the client left the
  main chunk (staff screen imports it on demand too).
- **Parity re-run after moving the digest into the Worker** (`make browser-parity`, 2026-09-17,
  headless Chromium 153, the six synthetic clips at 1 fps): as stored, pooled **median 2 bits, p90
  18, max 30, 0 of 480 rows past 31** — unchanged from ADR 0022; offsets +0 ms. The Worker's
  SHA-256 equalled the digest of all six clip files (checked with `--keep-clips`). The browser tag
  therefore stays `browser-rgb512-pdq-v1`, comparable with but not equal to the CLI's.
- **Production:** not yet — see ADR 0028's amendment of the same day.
- **Photo collections** stay CLI-only: a browser photo recipe needs its own tag and a parity
  measurement against Pillow (JPEG decoders and colour management differ), which is a follow-up.
