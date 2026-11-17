# 0037 — A frame embedding as a second, independent signal

Status: **accepted for a pilot** (2026-09-17), built and **off by default**
(`MATCHING_EMBEDDINGS_ENABLED`). The operator approved the dependency and the pilot; adoption
waits on the pre-registered numbers in § Pilot. Extends ADR 0010 (creator references), ADR 0015
(frame evidence), ADR 0022 (the sampling grid and pipeline tags) and ADR 0024 (the Hamming index).
Measurements: `docs/matching/second-signal.md`, and this decision's own in
`docs/matching/embedding-pilot.md`.

> **Numbering.** This ADR was drafted as 0036; 0036 is the Next.js customer app, so it is 0037.
> There is no ADR 0036 by this name.

## Context

PDQ on whole frames is our only content signal. Three studies now agree on where it breaks:

- the same picture re-hosted by two sites is a **median 28 bits** apart and **44 % of the pairs we
  can see are outside the threshold of 31** (`second-signal.md` §1.2, extending
  `0061-common-frames-review.md` §2.4 from radius 40 to 48; the population is still truncated);
- two pictures **0.5 s apart** are a median 60 bits apart, so only 23 % of them stay inside the
  threshold — a page showing one poster matches a 1 fps reference grid by luck
  (`second-signal.md` §4.2, reproducing `issue-73-v1-reference-recall.md` §1 on independent video);
- **the cause is the re-frame**, and nothing else. A 1–3 % asymmetric edge trim costs PDQ **44 bits**
  and leaves 3 % of pairs inside the threshold; 2–6 % costs 84 bits. A colour/contrast regrade,
  sharpening and blur cost **0–2 bits**, and `docs/matching/black-bars.md` established on 80 real
  page images that there is no padding in our index at all (`second-signal.md` §4.6).

The prize is measurable on our own index: **31 active pages sit 32–64 bits from a reference against
52 inside 31**, and 19 of those 31 already carry both a creator link and a duration matching an
original (`second-signal.md` §1.1). Nothing in the hash family reaches them — a centre-crop second
hash recovers a *symmetric* crop fully and an asymmetric re-frame not at all; the 64-bit hashes have
2–8 bits of headroom between a true pair and the nearest unrelated one, which is less than a JPEG
re-encode costs; and pHash 256, the only classic hash with a real bit budget, fails on the same
transforms in the same order because it is the same DCT of the same whole frame.

A small CPU vision embedding does reach them: cosine 0.913 through the full site pipeline against
PDQ's 58 bits, and it degrades smoothly across the sub-second offset instead of falling off a cliff.

But one measurement constrains the whole design. Over **805,683 real negative pairs** (both sides
above our PDQ quality floor of 50), MobileCLIP-S0's nearest unrelated pair scores **0.947** — higher
than the **0.933** median of a genuine same-picture pair; CLIP's is 0.927 against 0.940. The worst
of that tail is not noise: it is the same location at a different time, which in our domain reads
"the same creator in the same room in a different video". **An embedding therefore has no safe
fixed threshold and can never be evidence.** What it does have is ranking: recall@10 against a real
1 fps reference grid is 0.98–1.00 on every transform measured, including the ones where PDQ's
top hit is wrong.

## Decision

**Add one frame embedding beside the PDQ hash, as a candidate generator only.**

1. **Model:** MobileCLIP-S0 image encoder, fp32 ONNX (45 MB), through `onnxruntime` on CPU. Chosen
   over CLIP ViT-B/32 on resident set at comparable accuracy. The int8 export of MobileCLIP is
   refused: 4× slower and its scores collapse. No torch, no GPU.
   Cost, **re-measured with the shipped code** on the check runner (`docs/matching/embedding-pilot.md`):
   **55 ms per frame** on one thread (41 ms on two, 27 ms on four) against PDQ's 0.9 ms, and
   **+121 MB of resident set** per loaded session, not the 288 MB `second-signal.md` §5.1 reported
   for a whole harness process. The budget in code is 160 MB per hashing process.
2. **The model file is fetched, not committed.** 45 MB of weights are not source code: it is
   downloaded once per machine and pinned by sha256
   (`17d3c037b1d488c10c50e09f6009ea5a198caef4e0e8f4ea5617b7cb2d067ac0`, 45,543,630 bytes) by
   `manage.py fetch_embedding_model` on the server and `client/fingerprint.py --fetch-model` on the
   operator's Mac. It lives outside the repository (`~/.cache/marceline/models/`, or
   `MATCHING_EMBEDDING_MODEL_PATH`). A missing or unpinned file **raises with an actionable
   message** — never a silent run that produces no sketches, which would leave a hole in the index
   that nothing downstream could tell from "this picture has no second signal".
3. **Storage:** a **512-bit sign sketch** (64 bytes) per frame, in a `bytea` beside the PDQ hash —
   `index.FrameHash.embedding` (+ `embedding_pipeline`) and `matching.ReferenceFrameHash.embedding`.
   Measured: the sketch keeps recall@10 exactly (0.994, the same as fp32) while losing rank-1
   precision we do not need. **17 MB for the whole index today**, 1.8 MB/day of growth.
   The reference side stores **one** sketch per moment, not eight: an embedding is roughly
   flip-invariant (mirrored rank-1 recall 0.95 against PDQ's 0.03), so the dihedral expansion does
   not apply to it, and a database constraint keeps a sketch off every flipped orientation.
4. **Search:** the same exact `IndexBinaryFlat` of ADR 0024, in its own small wrapper
   (`matching/sketch_index.py`) — **0.21 ms per query at 265 k vectors**. No `pgvector`, no new
   index type, no new search backend. It is a **top-k** search and deliberately offers no radius
   query: a threshold on a sketch distance is the one operation this signal must not provide.
5. **Role, and the invariant that holds it there:** the embedding may only create candidate rows
   (`matching.EmbeddingCandidate`, top-k per page frame, `mutual` when both sides list each other)
   and, from the mutual ones, a `ReferenceMatch` with `candidate_source='embedding'`. Such a row
   carries **zero evidence frames, zero matched frames, no timeline, confidence 0 and band
   `review`** — it enters the human review queue and nothing else. Everything downstream is
   untouched: the evidence rules, the timeline, common-frame discounting, the confidence bands.
   An embedding-only page can reach the **review** band and **never** the high band, and therefore
   never the notice gate (ADR 0035 needs `high` plus a human confirmation).
   Enforced three times independently, in code and in tests, not only here:
   a database `CheckConstraint` (`reference_match_embedding_is_never_evidence`),
   `matching.confidence.band_for(embedding_only=True)`, and
   `matching.calibration.apply_bands`, which excludes these rows from re-banding.
   **Promotion uses the paths that already exist**: a later PDQ match takes the same
   `(reference, page)` row over and sets `candidate_source='pdq'`, or a human confirms it in
   review. A pair PDQ already matched is never touched by this signal at all.
6. **Pipeline identity:** the embedding gets a tag of its own
   (`mobileclip-s0-fp32-sign512-v1`), stored next to the frame pipeline tag
   (`Reference.embedding_pipeline`, `FrameHash.embedding_pipeline`), and a page sketch is
   only ever compared with a reference sketch carrying the same tag. Changing the model is a new
   tag and a re-embedding, exactly as ADR 0022 made the frame grid. Fingerprint file format v4
   carries the tag and an optional per-frame `embedding`; an unknown tag, a sketch on a flipped
   orientation, a sketch without a tag and a tag with a missing sketch are all refused.
7. **Where it runs:** in the media lane's existing hash path (`matching.media_hash.hash_media_bytes`
   and the crawl runtime's hash child processes), on the same pixels PDQ sees, in the same call,
   before they are discarded. Two consequences of memory, not of taste:
   - `RUNTIME_HASH_PROCESSES` becomes a memory decision. `matching.embedding.hash_processes`
     reduces it to what `MemAvailable` allows (160 MB each plus 256 MB of headroom) and **refuses
     to start** when not even one fits, rather than letting the lane be OOM-killed;
   - the **legacy per-site `media_worker` does not embed on the setting alone**. There is one per
     site — about twenty processes — and twenty copies of the model do not fit on a box with 1–2 GB
     spare. It takes an explicit `--embed` for a single site (the pilot's re-fetch). The rollout is
     therefore "the crawl runtime's sites first", which is where ADR 0030 is heading anyway.

   The reference side runs in `client/fingerprint.py --embeddings` on the operator's Mac, because
   the server never sees an original; the hashes-only JSON grows one 64-byte field per moment.
   `import_fingerprints` **attaches** those sketches to the existing reference when the grid is
   identical (every position's PDQ hash compared bit for bit first) instead of importing 600,000
   duplicate hashes beside it, and `add_reference --embeddings` does the same on the server.
8. **The browser path stays PDQ-only.** A 45 MB model in a customer's browser is not proportionate
   (ADR 0017/0019), so a browser-side fingerprint simply has no second signal. That is a documented
   gap, not a bug.

### What this decision explicitly is not

No face detection, no face recognition, no biometric or body measurement, no person identification,
no age estimation — not as an output and not as an intermediate step. The implementation keeps that
structural rather than promised, and `matching/tests/test_embedding_boundaries.py` reads the source
to prove each part:

- the only value that ever leaves the encoder is one normalised 512-dimensional vector, immediately
  reduced to 512 sign bits, and the only operation performed on the result is a Hamming distance to
  another frame's bits. Nothing public in `matching/embedding.py` returns a float vector, and the
  session's output has **exactly one consumer**, the sign reduction;
- there is no classifier head, no detector, no crop proposal, no landmark and no text output:
  `verify_model` refuses any file whose graph is not exactly one `(batch, 3, 256, 256)` image input
  and one `(batch, 512)` output, so a text tower (a second input) or a head (a second output) is
  rejected before a session is built;
- MobileCLIP's **text tower is not shipped** — the pinned file is the `vision_model` export — so
  the model cannot be asked a question about a picture at all, and a test asserts that nothing in
  the backend names a text encoder, tokenizer, face detector or age estimator;
- only `matching/embedding.py` may open an onnxruntime session, in the backend and in the client.

## Pilot, and the exit criterion

The pilot must answer one question on **our own data**: does the embedding link the page frames PDQ
loses, without flooding review? It is implemented as `manage.py embedding_pilot --stage
plan|fetch|measure` (`marceline/embedding_pilot.py`), with the criterion frozen as module
constants so it cannot be adjusted after the numbers are in.

1. **Reference side, free and retroactive.** Re-fingerprint the 44 originals on the operator's Mac
   with embeddings (68 min of CPU for 76,795 moments) and import them; the sketches attach to the
   existing references. Nothing is re-matched yet.
2. **Page side, bounded.** Re-fetch the media of two named sets through `index.media_fetch`
   (which re-reads the safety gate immediately before every request, as `black-bars.md` did for its
   115 requests) and embed them in memory: (a) the **31 near-miss pages** of `second-signal.md` §1.1,
   and (b) both sides of the **2,474 cross-site page pairs** of §1.2. Budget: ≈ 3,000 media
   requests, about 6 % of one day's summed media budget, spread over a day, no site above 10 % of
   its own (`--site`, `--limit`; the plan stage prints the share per site and warns above it).
   Expect a high 404 rate on tokenised hosts — `black-bars.md` measured 100 % expiry on
   site03.example — and report it rather than working around it. A picture whose PDQ hash no longer
   matches the stored one is a *different* picture: it is counted and skipped, never described by a
   sketch belonging to another frame.
3. **Measure three things.**
   - **Recall on what PDQ loses:** of the cross-site frame pairs beyond 31 bits with sketches on
     both sides, what share are mutual top-10 neighbours in the sketch index?
   - **Precision at our scale:** how many candidate originals per embedded page does review have
     to judge?
   - **New findings:** how many of the near-miss pages reach the review band?

**Exit criterion.** Adopt only if, on that set, the embedding brings **≥ 50 % of the cross-site
pairs PDQ loses into a mutual top 10** while adding **≤ 5 candidate originals per page** to review,
and at least **5 of the 31 near-miss pages** reach the review band. If recall clears the bar but
precision does not, the fallback is narrower and still useful: run the embedding **only within a
reference's own shortlist** (a page already linked to an original by duration, creator or cluster),
where the candidate set is tiny and the false-neighbour cost is bounded. If recall misses the bar,
drop the signal and record the numbers — a second signal that does not reach the pages PDQ misses is
not worth 160 MB of resident set per hashing process. `Measurement.verdict()` prints exactly that.

**Status of the pilot, 2026-09-17.** The code is built, green and off. The numbers are **not in**,
and they cannot be from this side alone: the reference half needs the operator to re-fingerprint the
originals on his Mac (the server has never seen them), and the page half needs a day of bounded
re-fetching on the development host after this branch is merged and migrated. What *was* measured
with the shipped code, on the test corpus of `second-signal.md`, is in
`docs/matching/embedding-pilot.md`: through the measured site pipeline (a 3 % asymmetric re-frame,
unsharp mask and regrade at 403 px / JPEG 72), PDQ's median distance to the right moment is 57 bits
with **0 of 160** pairs inside the threshold, while the sketch puts the right moment in a **mutual
top 10 for 160 of 160** — the mechanism of criterion 1, reproduced by the production code rather
than by a research harness. Criterion 2 is **not measurable** on a single-video corpus (all ten
neighbours belong to the one original there, so "candidate originals per page" is 1 by
construction), and criterion 3 needs our own near-miss pages. Both need the real index.

## Consequences

- **Not retroactive, and that is accepted.** Page pixels are discarded by design, so the second
  signal exists only for media fetched after the switch. A full backfill is impossible (≈ 4 days of
  the entire media budget, and a large share of stored media URLs have already expired). The index
  will hold two populations for a long time; the matcher treats a missing sketch as "no
  information", never as a negative — that is why `FrameHash.embedding` is nullable and why every
  query filters on the tag rather than on presence.
- **Rollout, in this order.** (1) `fetch_embedding_model` on the development host; (2) the operator
  re-fingerprints his originals with `--embeddings` and imports them, which is free and touches no
  page; (3) the pilot's bounded re-fetch, over a day, per site; (4) `embedding_pilot --stage
  measure`; (5) only then `MATCHING_EMBEDDINGS_ENABLED=true` for the crawl runtime's media lane, so
  newly fetched media carries sketches from then on. Rollback is the setting plus, optionally,
  clearing the two columns; nothing about PDQ changes at any step.
- **A second place the pipeline must stay in step** — the client CLI. ADR 0022 exists because the
  server and the client hashed different moments; a second signal doubles that risk, which is why
  §6 gives it its own tag, refuses an unknown one, and refuses a file that carries a tag but not a
  sketch for every position.
- **The candidate table is rebuilt, not updated.** `refresh()` deletes and re-derives every
  candidate row of the recipe, because a sketch never changes once stored and the set of embedded
  frames only grows. During the pilot that is a few thousand frames. At full rollout it is up to
  `k ×` the embedded frames — bounded, but the run would be worth making incremental the way
  `matching.incremental` made the PDQ scan (ADR 0015). Do that when the numbers justify it, not
  before: an incremental cache that is subtly wrong is worse than a slow correct one.
- **More candidates means more review.** The whole design is a funnel into human work, so the
  pilot's precision number is as binding as its recall number, and the confidence bands
  (ADR 0015) are where the pressure will show first. The review API says which signal proposed a
  row, because confirming an embedding candidate is a judgement about pictures, not agreement with
  a score.
- **Memory is the scarce resource**, not CPU: +121 MB measured per hashing process on a box that has
  1–2 GB spare. The model is loaded once per process, the hash pool's RSS ceiling moves with it (or
  every child would be recycled as a leak on its first job), and the number of children is decided
  by `hash_processes`, not by the setting alone.
- **A preview clip pays for frames it then drops.** `hash_video` embeds each sampled frame and only
  afterwards drops consecutive near-duplicates, so a 32-frame clip can spend ~1.7 s of CPU on
  frames that are discarded. Preview clips are 2.3 % of active-page media on 2 of 20 sites, so this
  is accepted rather than fixed; fixing it means keeping the decoded images alive longer, which is
  the opposite of what the media path is built for.
- **Rejected alternatives, with their numbers**, are in `docs/matching/second-signal.md` §6: other
  perceptual hashes (no bit budget, or correlated with PDQ), audio fingerprinting (we hold no audio
  for 97.7 % of media), shot-boundary signatures
  (no material), OCR and watermark template matching (3–5 px of mark on a 403 px frame), and PDQ
  multi-scale (fixes a symmetric crop, not the re-frame that happens).
- **Independent of this ADR, and cheaper:** metadata scoring — duration, creator link, tags, title
  trigrams — is fully retroactive over all 80,788 pages today and needs no dependency at all
  (`second-signal.md` §4.7). It should ship whether or not this ADR is adopted.
