# 0015 — Frame evidence: timeline, common frames, confidence bands

Status: accepted (2026-09-15)

## Context

`ReferenceMatch` (ADR 0010) counts distinct page frames within Hamming distance 31 of non-shared
reference frames. Real data showed three gaps. A confirmed site04 leak (page 2616, reference 1)
has one poster at distance 36, just above the cut-off. Raising the global threshold would admit
more noise on an index that is about to grow. Page frames that repeat across unrelated videos
(studio intros, watermarks, title cards) would count as evidence. The suggestion cap of five per
reference also dropped pages backed by real evidence. Reviewers need an ordering, and any
automation must stay auditable.

## Decision

### Timeline verification (`matching.timeline`, pure)

Page frames get an order from their media: site02 hover frames `…_N.jpg` (position N, sampled
across the video in order) and preview-clip `frame_index` (sample position at 2 fps). Posters
and other single images have no order. `verify_timeline` works per ordered sequence with at least
2 matched frames. It takes the longest chain in position order whose reference timestamps do not
decrease (2 s tolerance for the 1 fps grid; each frame may pick any of its matched timestamps).
The chain may cover only part of the reference, because clips are leaks.
`score = consistent / ordered`. For clips it is multiplied by `1 − 0.25 × (1 − R²)` of timestamp
over position, so spacing must be roughly linear: a contiguous clip or an evenly spaced montage.
Hover frames need only order. With several sequences the score is their frame-weighted mean. A
timeline is *consistent* with at least 2 chain frames and at most `floor(25%)` outliers. Without
ordered frames the score is null, and confidence treats null as neutral, never as a penalty.
`ReferenceMatch` stores `timeline_score`, `consistent_frames` and `timeline_reason`, computed over
evidence frames.

### Looser distance (`LOOSE_MAX_DISTANCE = 40`)

The strict threshold stays 31. A frame whose best non-shared hit lies in 32–40 counts as evidence
only in one of two cases:
- It belongs to a consistent timeline chain of at least 3 frames, one of them strict.
- The page shares an active cluster or recognized mirror group with another page that has a
  current strong match for the same reference, and a human has not rejected that seed in either
  table. This corroboration is computed in memory from the same run, one hop only.
A page whose only hits are unadmitted loose frames gets no row. `loose_frames` records admitted
frames. CLI: `match_references --loose-max-distance 40`.

### Common frames and studio attribution (`matching.common_frames`)

`build_common_frames [--max-distance 8] [--min-pages 5] [--min-quality] [--dry-run]` runs a
vectorised self-join (`HashIndex.neighbor_pairs`, upper triangle in memory-bounded batches) over
quality-eligible page frames. It then builds deterministic star groups: the frame with the most
neighbours leads, and unassigned neighbours within the radius join it. A group needs frames on
pages of at least `min_pages` distinct videos. Pages count as one video when they share an active
cluster or mirror group (transitively), or when they have matching durations (`max(30 s, 3%)`) and
share at least 2 near-identical frame pairs (non-transitive leader grouping). Without this rule,
reposts of a real leak look exactly like an intro.

`CommonFrameGroup` stores the representative hash, radius, member frame/page/site counts, sample
page ids, `label`, `kind` (`studio_intro` / `watermark` / `generic` / `not_common` / `unlabeled`),
`studio_name`, status (proposed/confirmed/rejected), `labeled_at` and timestamps.
`CommonFrameMember` links each frame (at most one group) and its page to a group, so a UI can list
"pages showing this intro" and attribute pages to a studio later.

`label_common_group(group, kind, studio_name, status, label=None)` validates the input and sets
`labeled_at`. Labeled groups are rebuild anchors: every frame within their radius joins them,
whatever the page count, so labels and attribution follow new uploads. Unlabeled proposals are
recomputed. An unlabeled row whose representative lies within the radius of a new proposal keeps
its id; stale unlabeled rows are deleted. Reruns are idempotent. The labeling screen is a separate
task.

During matching, a page frame is common when it is a stored member of a proposed or confirmed
group whose kind is not `not_common`, or when it lies within such a group's radius of the
representative (covers frames hashed after the last build). Common frames are never evidence,
exactly like shared reference frames; `common_frames` and `common_seconds` record them.

### Confidence and bands (`matching.confidence`, pure; `matching.calibration`)

`score_confidence` adds named weights and clamps the sum to 0..1:
- evidence `0.45 × (1 − 0.5^frames)`
- distance `0.20 × (1 − best / 40)`
- timeline `0.20 × score` (null → 0.5)
- duration `+0.10` within 1.5 s, `+0.05` within the cluster tolerance; shorter or longer pages get
  nothing, never a penalty
- corroboration `+0.15`
- common penalty `−0.15 × common / (common + evidence)`

Components are stored in `confidence_signals`. Band `high` needs at least the high threshold *and*
at least 2 evidence frames; `review` needs at least the review threshold; everything else is `low`.
Defaults: high 0.75, review 0.40. `ReferenceMatch.objects.review_queue(include_low=False)` returns
proposed rows ordered high → review (→ low), then by confidence. Low rows stay stored but hidden by
default.

Extended by ADR 0020 (2026-09-16) for photo collections, which have no order: they compute no
timeline at all, admit loose frames only through corroboration, gain an `images` component
(`0.20 × (1 − 0.5^(images−1))`) in place of the timeline and duration signals they cannot earn,
and reach the high band only with three independent (page frame, collection image) pairs instead
of two evidence frames.

`calibrate_confidence [--dry-run]` collects stored matches with a human decision in either table
(rejection wins; `decided_by=auto` is not a label). It prints count and precision per 0.1 bucket,
plus confirmed leaks without a match.
- With fewer than 20 labeled leaks it prints "insufficient labels" and keeps the defaults.
- Otherwise high is the lowest threshold on a 0.05 grid with at least 98% precision above it.
  Review is the highest threshold that still keeps 98% of labeled leaks out of the low band
  (capped at high).
- Without `--dry-run` it stores the result in the single `ConfidenceSettings` row (pk 1, readable
  by the UI) and re-bands stored matches.

### Auto-decision policy

Nothing is confirmed silently. Only when the Django setting `AUTO_CONFIRM_HIGH` is true (default
off; not set in `settings.py`) does matching confirm *proposed* high-band rows with
`decided_by=""`, recording `decided_by=auto`. Existing decided rows were migrated to `human`.
Admin status edits record `human`, and the admin action "Revert auto decisions" resets auto rows
to proposed/human. Each matching run turns `auto` rows no longer confirmed, and decided rows
without an author, into `human`, so a reset or reversal is never re-confirmed. Human statuses are
never overwritten.

### Suggestion cap

`suggest_leaks` never caps candidates backed by a ReferenceMatch, cluster or mirror relation.
Only duration-only candidates use `--limit-per-reference` (default 5).

### Scan once (2026-09-15)

A video needs to be scanned only once. Before this change the same image was downloaded and
PDQ-hashed several times (site04 mirrors site02 posters, re-crawls get new tokenized site03
URLs, identical bytes under different URLs). Every `match_references` run also compared all page
frames with all reference hashes twice (shared frames and evidence).

**Reuse before download** (`matching.media.find_url_source`, called by the media worker after
the safety, capture and scheme checks, before any request; a sprite sheet is subject to the same
reuse — its grid, declared or detected, is only needed once hashing actually happens, issue #14).
A pending media is finished with
copies of another media's frame hashes (same `frame_index`, `pdq`, `quality`) when both URLs
have the same `services.media_content_key`, which exists only for known mirror CDNs:
- site02/site04 mirrors: the ADR 0009 mirror path *including* its `_main` / `_N` suffix
  (each suffix is another image), any host, prefix or query string (`services.mirror_key` is
  the page-level key without the suffix, used for cluster evidence);
- site03's third-party CDN: the media path
  without the rotating token and 10-digit expiry.
Generic host-independent paths are never keys: site01, site22 and site18 share
`contents/videos_screenshots/{bucket}/{id}/preview.jpg` ids for unrelated videos.

**Reuse after download** (`find_sha256_source`). Downloaded bytes whose sha256 equals a hashed
media's are not decoded again; the copy keeps the new sha256, size and content type and the
source's dimensions.

Rules for both: the source is DONE, has at least one frame hash, uses the same decoder (poster
and frame are both single images; preview clips only match clips) and is on a safety-clear page;
the target page is re-checked as clear inside the transaction (a page flagged meanwhile is skipped
exactly like `store_hashes` does, and reviewed/blocked pages are never claimed). URL-key copies
store no sha256, size or dimensions: nothing was downloaded. `matching.MediaReuse` records media,
source, reason (`url_key` / `sha256`), matched key and copied frame count; re-hashing a media from
bytes deletes its record. The worker counts reuses per reason (`media_worker` prints them).
The URL lookup is a substring prefilter (`url LIKE %needle%`, status done) without an index:
~17 ms per keyed claim at 34k media (sequential scan). Past a few hundred thousand media it needs
a trigram or stored-key index on `index.Media` (index app change).

**Incremental matching** (`matching.incremental`). The scan result is sparse: only page frames
within the matching radius of a reference frame can be evidence, count toward shared-frame page
groups or be discounted as common. `ReferenceCandidate` caches (page frame, reference, smallest
distance <= `max(max_distance, loose_max_distance)`), both sides quality-eligible. A normal run
scans
- page frames with `created_at` >= the previous scan start minus `FRAME_OVERLAP` (1 h, for media
  transactions that commit after their timestamp) against all references, and
- references that are new or whose eligible frames changed (`reference_signatures`: count and
  last frame id per reference) against all page frames, after deleting their old candidates.
Frame deletions cascade to the cache. Everything downstream is then recomputed on every run from
the candidate frames only, not partially: `mark_shared_frames(page_frames=…)` over candidates
within `max_distance`, and `aggregate_matches` over all candidate frames of the selected
references with current clusters, mirror groups, common groups, durations, human decisions and
thresholds. `page_frames` comes from a SQL count. Because every (reference, page) aggregate is
rebuilt from complete frame sets, corroboration, cluster and duration changes need no rescan and
the result equals a full run on the same data (tested).

`MatchRunState` (single row, pk 1) stores the fingerprint and its readable `settings`, reference
signatures, `frames_scanned_at`, and the last mode, run time, full-run time, scanned frames,
candidate count and scan seconds. The fingerprint digests scan version, `max_distance`,
`loose_max_distance`, `min_quality`, the confidence thresholds, the shared reference-frame ids
and the discounting common-frame groups (id, representative, radius; stored members always lie
within the radius). It is compared before the scan with the value saved after the previous run
(after shared flags were recomputed). A mismatch, or `match_references --full`, deletes the
cache and rescans everything. Runs lock the state row, so they serialize. Output ends with
`Scan: full|incremental (…)`.

Measured on the dev DB in a READ ONLY transaction (2026-09-15; 34k media, 33.4k eligible page
frames, 8 references with 168k hashes):
- **URL keys:** 134/134 DONE mirror-key pairs and 12/12 site03 token-key pairs had equal sha256
  and PDQ distance 0. Of 20,532 pending media on clear pages, 11,214 have a key, but only 107 are
  reusable now: 105 site02 (mirror) and 2 site03. site02 hover frames `_N` and
  `imgs/thumbs/…` have no site04 copy, and earlier site03 duplicates expired before being
  hashed.
- **sha256:** 380 groups of DONE media share bytes (832 media, 452 redundant hashings): 149
  site02/site04, 115 site03, 92 within site02 (a poster equal to a hover frame, placeholder
  frames), 21 site01.
- **Matching time:** the old run took 197 s for shared frames plus 172 s for evidence. A full
  vectorised scan takes 189 s and finds 23 candidate pairs. Shared frames on candidates take 14 s
  (almost all of it the reference × reference comparison) and evidence 0.6 s. An incremental run
  costs about 5.6 ms per new page frame plus ~15 s. 17.6k frames hashed in the last 3 h during the
  site02 backlog took 100 s. A new reference against all frames takes 26 s. At 2,000 new frames
  per 3 h a run takes ~26 s, against ~205 s full or ~370 s before.

Known limit: a page frame whose transaction commits more than `FRAME_OVERLAP` after its
`created_at` is missed until the next full run (media worker transactions take milliseconds).
Frames edited in place (not done by any code path) are not rescanned.

## Evaluation (dev DB, READ ONLY transaction, 2026-09-15)

8 references, 30,730 quality-eligible page frames; pure code only (the new tables did not exist).
- **Timeline:** page 48 (ref 1, hover 3→1013 s, 4→1128 s, 10→2980 s) score 1.0, 3/3 consistent.
  Page 1937 (ref 6, montage clip) score 0.99, 9/9, R² 0.96.
- **Loose distance:** page 48 gains hover 4 at distance 38 via its timeline (3 evidence frames).
  Page 2616 (ref 1, poster, distance 36, cluster 306 with strong page 48) now matches as weak with
  1 loose frame, confidence 0.595, band **review**.
- **New match:** page 7406 (site03, ref 5, not previously stored) is strong at 31 with 2 frames,
  6 with loose, timeline 0.99, band high. It is unreviewed.
- **Confidence with default thresholds:**
  - 48: 0.81, high
  - 1937: 0.91, high
  - 6650 (human-confirmed weak poster, distance 20): 0.475, review
- **Common frames at the defaults:** 0 groups; grouping takes 15.6 s, and a 60k-frame scale check
  60 s. Without the video/copy rule, 71 groups (84 at distance 16) would form. All of them are
  site03 reposts of the same video (active clusters 33, 141, 142), so the rule prevented
  discounting real evidence. No current match or matched-page frame would be discounted. The index
  has no cross-video intros yet.
- **`calibrate_confidence --dry-run`:** 4 labeled pairs (4 leaks, 0 false matches) → "insufficient
  labels", defaults kept.
- **Suggestions (dry run, new cap rule):** refs 1–8 give 0, 0, 1, 0, 1, 0, 1, 0, the same as the
  old rule, because no undecided evidence-backed candidates are stored yet. After a matching run,
  page 7406 would add one uncapped suggestion for ref 5.

This is a retrospective check on a handful of labels, not an accuracy estimate.

## Consequences

Matching keeps one in-memory reference index and now queries it at radius 40. Before scan once
a run cost one query per page frame twice (shared frames and evidence, ~370 s at 33k frames);
with the candidate cache a normal run scans only new frames and references (see Scan once).
Common-frame
grouping is O(frames²) vectorised; past a few hundred thousand frames it needs an ANN/FAISS
index (ADR needed). Confidence weights are hand-set until at least 20 labeled leaks exist.
Thresholds then come from data, but weights stay fixed and documented. The copy rule can
under-count intro pages when unrelated videos have similar durations and share two intro frames.
That is the safe direction: missed discounting beats lost recall. `build_common_frames` runs
daily from the scheduler, before `match_references` (ADR 0012); a changed group set makes the next
match run a full scan. The review API does not set `decided_by`; matching reconciles unauthored
decisions as `human`.

## Amendment 2026-09-17: narrower scan fingerprint, confirmed common frames only

Review findings M2 and a common-frame follow-up; code in `matching/incremental.py` and
`matching/common_frames.py`.

- **The fingerprint covers only what the scan depends on** (`incremental.scan_settings`):
  `SCAN_VERSION`, the scan radius (the larger of `max_distance` and `loose_max_distance`), the
  minimum quality, the hash size and whether the search is exact or approximate (ADR 0024, an
  approximate backend can miss neighbours). Nothing else changes which (page frame, reference,
  distance) triples lie within the radius. Confidence thresholds, shared reference frames,
  common-frame groups, imported or retired references and human decisions only change the
  aggregation, which every run recomputes from the candidates anyway, so they no longer force a
  full rescan. Until now an import, a retirement, a common-frame rebuild or a calibration cost a
  full ~110 s scan on the live index. New and changed references are still covered by
  `reference_signatures`, and a retired reference leaves no signature behind. Tests prove an
  incremental run after such changes equals a full run.
- **Only confirmed common-frame groups discount evidence** (`DISCOUNTING_STATUSES`). A proposed
  group is the daily rebuild's guess; letting it discount silently weakened real leaks before
  anyone had looked at it (an intro-heavy leak copied to many pages looks exactly like an intro).
  "Proposed or confirmed" in *Common frames* above now reads "confirmed", and the last sentence of
  *Consequences* no longer holds: a changed group set does not make the next run a full scan. The
  scheduler still re-runs matching after a common-frame label changes (ADR 0012).

## Amendment 2026-09-17: copy extent — full copy, excerpt or compilation (issue #62)

Clips are leaks, and that stays a *matching* rule. It must not become a *notice* rule: an excerpt
or a compilation needs a licence/exception review before anyone asserts infringement. So every
match now also says how much of the original the page carries (`matching/copy_extent.py`, pure;
`references.refresh_copy_extents`, DB):

- **Stored on `ReferenceMatch`** (migration `matching/0019`): `copy_extent`
  (`full_copy | excerpt | compilation_candidate | unknown`), `copy_extent_reason` (the deciding
  rule), the inputs `copy_span_s` (first to last in-order matched moment of the original plus one
  sample period, taken from the timeline chain rather than every candidate second, so an outlier
  cannot widen it), `copy_coverage` (span / original duration), `copy_other_originals` and
  `copy_extent_version` (`CLASSIFIER_VERSION`).
- **Rules, in order:** photo collection or no original duration → unknown; another video file with
  a current non-rejected strong match on the page → compilation candidate (pipeline twins of the
  same sha256 and photo collections do not count); fewer than 3 in-order frames or no order at all
  (posters) → unknown, never excerpt; timeline score < 0.75 → unknown; page clearly longer than
  the original → compilation candidate; clearly shorter (below 90 % and beyond the duration
  tolerance) → excerpt; span ≥ 0.6 with a matching page duration, or ≥ 0.8 without one → full copy;
  span < 0.5 with ≥ 4 in-order frames and no page duration → excerpt; otherwise unknown. Page
  duration is an indicator here as everywhere: it only ever moves a copy *towards* review.
- **Measured** on the 29 human-confirmed matches of 2026-09-17: 8 have a verified timeline (site03
  preview clips, site02 hover frames), all with a matching page duration and a span of 0.69 to
  0.98 → all full copies; the other 21 are poster or single-image evidence → unknown. No confirmed
  excerpt exists yet, so the excerpt thresholds are reasoned, not fitted; revisit them once
  reviewers have confirmed some.
- **Recomputed incrementally:** every `match_references` run writes the span for the rows it
  aggregates and then re-derives the extent of every match on the pages it touched (including
  pages that lost a proposal) plus every row another classifier version wrote. A `--reference`
  subset run therefore still updates the other originals' rows on a shared page. `updated_at` is
  not touched. The scheduler re-runs matching while any row is on another version.
- **Never a filter.** The extent changes no status, band, strength or row set; excerpts stay
  matches, cluster members and customer findings.
- **Surfaces:** admin column and filter; review API `copy_extent*` and `exception_review_required`
  on matches, `copy_extent` on cluster member links; customer findings carry
  `extent: full_copy | excerpt | not_determined` (a compilation candidate is `not_determined` —
  naming it would reveal another original on the page), the most cautious value across one file's
  references.
- **The notice gate** (not built) asks `copy_extent.requires_exception_review(match)`: true for an
  excerpt, a compilation candidate, unknown, no match, and any row of another classifier version.
  A current full copy only means this one review is not triggered by the extent; every other notice
  gate still applies.

## Amendment 2026-09-18: the strict threshold is 36, and it is one named constant

"The strict threshold stays 31" above no longer holds. It is **36**, and it lives in exactly one
place: `matching.references.STRICT_MAX_DISTANCE`, beside `LOOSE_MAX_DISTANCE`. The candidate scan's
shared-frame marking, evidence aggregation, `match_references`, `m2_report` and the staff
overview's M2 summary all read it; a test asserts every one of those defaults is the same object,
because a measurement taken at a threshold the matcher does not use is worse than no measurement.

Three neighbouring numbers are **not** this decision and stay where they are:
`matching.duplicates.DEFAULT_MAX_DISTANCE` (31 — whether two *pages* show the same video),
`matching.pdq.MATCH_DISTANCE` (31 — Meta's recommended starting point for PDQ itself, used by the
hash-level tests) and `build_common_frames --max-distance` (8 — whether two frames are the same
picture). `LOOSE_MAX_DISTANCE` stays 40.

**Why.** Measured on the development index on 2026-09-18 over the 217 (original, page) pairs a
human has decided — 67 confirmed, 150 rejected, the rejected ones hard negatives — precision is
**identical at 31 and 36** (0.984; the one false pair inside both sits at distance 8 and is
untouched by any threshold), while recall rises from **0.910 to 0.940**. The first *additional*
false pair appears at **40**, so the raise leaves four bits of margin. Nothing moves between 31 and
35: the entire gain sits on 36, which is where page 2616 — the poster this ADR's Context section
was written around — has always been. Full measurement, including what a real run does and how to
roll it out: `docs/matching/strict-threshold-36.md`.

**Two consequences, both measured rather than assumed.**

- **No full rescan.** Since the 2026-09-17 amendment above, the scan fingerprint is keyed on the
  scan *radius*, which is `max(strict, loose)` — 40 either way. The fingerprint is byte-identical
  at 31 and 36, so the next run is an ordinary incremental one. (A full rescan would cost 113 s
  against a 3 h cadence, so this is a convenience, not a constraint.)
- **Calibration is unaffected in kind, stale in degree.** `max_distance` never enters
  `score_confidence` — the distance component is scaled by the *loose* distance — and bands are
  cut-offs on confidence, not on distance. So the stored thresholds stay coherent and nothing must
  be recalibrated before this ships. But a run at 36 rewrites nine confidences and adds nine rows,
  so `calibrate_confidence` should run **after** the first matching run at 36, never before. The
  stored `review >= 0.35` is what now caps the change: a lone poster at distance 36 scores 0.345
  and lands in the hidden `low` band, so three of the nine new rows are created and not shown.

`mark_shared_frames` reads the same threshold, so the raise could have marked more reference
frames as common footage and taken evidence away. It does not on this index: 196 shared frame
positions / 1,568 shared hash rows at 31, and exactly the same at 36.

**Effect of one real run, simulated in a rolled-back transaction:** +9 match rows, none lost — 6
`review`, 3 `low`, **0 `high`** (every new row has a single evidence frame, and the high band needs
two). The staff review queue goes 1 → 7 and customer-visible matching copies 58 → 64 on 52 → 58
pages. Those are the same six rows: an undecided `review` row already counts as a matching copy for
a customer, so the raise reaches customers at the same moment it reaches the queue.

## Amendment 2026-09-18: what a negative sample is, and when calibration refuses (issue #90)

`calibrate_confidence` would have stored `high >= 0.00`. On the development index of 2026-09-18 it
read 96 labelled pairs — 95 confirmed, **one** rejected — so every threshold on the grid reached
the 98 % point estimate, the lowest one won, and a real run would have re-banded every match with
two evidence frames into the notice-eligible band and overwritten the operator's hand-set
0.75 / 0.35 of 2026-09-17. Nothing was scheduled, so nothing happened; the run was never made.

### A negative is a rejection the matcher scored

Calibration maps a **confidence** to a precision, so a sample must be a pair that has one: a
stored `ReferenceMatch` row. The 160 rejections that exist only as `ReferenceLeak` rows are
`suggest_leaks` duration-only candidates — a human verdict on a *suggestion*, not on our evidence,
for a pair nothing ever scored. They are counted and excluded by name, and **not** read as
confidence 0:

- 0 would be a fabricated value; the scorer never looked at that pair.
- It would be the wrong kind of fix. Those samples sit below every candidate threshold, so they
  change precision only at 0.00: the chosen high would move from 0.00 to **0.05** — still every
  two-frame match in the high band — while the record would show 161 "negatives" and the guard
  below would pass on them. A cosmetic repair of the symptom is worse than the symptom.

Rejections *with* a match row were already counted and still are (rejection in either table wins).
There is no third class today: `match_references` deletes only *proposed* stale rows, so a rejected
pair keeps its row and its confidence. Near-miss pairs that never produced a row would need a new
scan, which calibration does not do.

### The guards, in code

`matching.confidence.calibrate` derives nothing, keeps whatever is in force and names the guard;
`matching.calibration.calibrate_confidence` writes nothing at all in that case — not even the
report — and prints the sample accounting either way.

- **`insufficient positives`** — fewer than `MIN_CALIBRATION_POSITIVES` (20), as before.
- **`insufficient negatives`** — fewer than `MIN_CALIBRATION_NEGATIVES` (**20**). A precision
  target is a statement about the negatives a threshold admits, so it can only be tested against
  negatives that were scored. With one, the test cannot even fail: precision drops below 98 % only
  once more than `positives_above / 49` negatives sit above the threshold. 20 is the bar the
  positive side has carried since this ADR, and with zero admitted the rule of three puts the 95 %
  upper bound on the admitted share at 3/20 = 15 %; below that the estimate has no resolution at
  all.
- **`precision unproven`** — the chosen high is the lowest grid step whose decided pairs carry a
  95 % **Wilson lower bound** of `HIGH_TARGET_PRECISION`, not a point estimate. 100 % over 20 pairs
  bounds precision at 0.84 and buys nothing; with no false match above the threshold the bound
  reaches 0.98 at about **190** decided pairs above it. That is the number of labels a calibrated
  high band costs, and saying so is the honest answer — today's 96 cannot carry one.
- **The floor.** The grid starts at `max(HIGH_THRESHOLD_FLOOR, review in force)`, so a stored high
  is never below the review threshold and never below **0.50**: under 0.50 a score is carried by
  the evidence term alone (0.45 at most), with nothing from distance, timeline, duration or
  corroboration. That is not notice-grade, whatever a small sample says.
- **`high would fall without new negatives`** — more positives make a *lower* threshold provable,
  which is not a reason to lower the band. A derived high below the stored one is refused unless
  the scored negatives have grown since the stored thresholds were derived
  (`ConfidenceSettings.labeled_negatives`).
- **`operator override`** — `ConfidenceSettings.calibrated_high` / `calibrated_review` record what
  calibration itself derived beside what is in force. They differ exactly when a human set the
  thresholds, so an override needs no box to tick; a row from before the field pair (both NULL) has
  unknown provenance and is read as an override, which is the safe direction. The run prints
  before → after and keeps the override unless `--replace-override` is passed.

The three candidate-source guards of ADR 0037 / ADR 0040 (the database constraint,
`confidence.band_for`, `calibration.apply_bands`) are untouched.

### Consequences

- The stored 0.75 / 0.35 stay in force, and now say so with a reason instead of by the operator
  remembering not to run the command.
- **Calibration is blocked on labelled negatives, not on labelled leaks.** The cheapest source is
  the undecided pairs that already carry a stored match; a rejected duration-only suggestion, the
  overwhelming majority of today's rejections, moves nothing.
- **#80 (crop grid) step 4 waits on this.** Adopting the grid requires re-running
  `calibrate_confidence`, because a minimum over 256 correlated hashes moves the whole distance
  distribution down 10–16 bits. Until roughly 190 decided pairs sit above a candidate threshold and
  20 of the decided pairs are scored rejections, that re-run will refuse, and the grid must be
  adopted (or not) against the thresholds already in force.
