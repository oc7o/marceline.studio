# 0040 — Metadata scoring as a third signal, retroactive and never evidence

Status: **accepted** (2026-09-18), **amended 2026-09-18** — the output is a fetch instruction, not
a review item; the amendment at the end supersedes Decisions 4, 5, 6, 8 and 10 below and is the
part to read first. Built and **off by default** (`MATCHING_METADATA_CANDIDATES_ENABLED`).
Extends ADR 0010 (creator references), ADR 0015 (frame
evidence) and ADR 0037 (the second signal), whose closing paragraph recommends exactly this and
never measured it. Measurement: `docs/matching/metadata-scoring.md`.

## Context

Two content signals exist and both compare pictures, so both share one hard limit: **a page whose
pixels we already discarded can never be reached again.**

- PDQ is our only evidence signal and needs a frame hash. **2,647 of 26,747 active pages (9.9 %)
  hold no frame hash at all** — the media URL expired, the host's robots.txt forbids it, or it was
  never eligible under the safety gate.
- The embedding of ADR 0037 is explicitly **not retroactive**: page pixels are discarded after
  hashing, so a sketch exists only for media fetched after the switch. Today that is **109 frames
  in the whole index**, and a full backfill was costed at about four days of the entire media
  budget against a large share of already-expired URLs.
- ADR 0039's reference-side crop grid recovers the cross-site loss that motivated ADR 0037 — a
  1–3 % asymmetric re-frame — and recovers it **as evidence**. So the gap left over is not
  "cross-site recall". It is the retroactive blind spot, plus the general want of cheap candidates.

Everything needed to score a page against an original is already in the database and cost nothing
to obtain: `Page.raw_extraction.duration_s`, `Page.creators`, `Page.title`, against
`Reference.duration_s`, `Reference.creators` and `Reference.title`. Coverage on active pages:
92.4 % have a parsed duration, 100 % a title, 45.2 % a creator link.

`second-signal.md` §4.7 measured something adjacent and drew the wrong-shaped conclusion from it:
it scored **page against page**, with positives selected by PDQ, so it could only establish that
two pages PDQ had already linked also share metadata. The operationally useful question — *does
this page's metadata suggest it carries this original?* — was measured for the first time in
`docs/matching/metadata-scoring.md`, over **1,176,868 (original, active page) pairs** and the
**188** pairs a human has actually judged.

The numbers that decided this ADR:

| | |
|---|---:|
| pairs the rule proposes, whole index | **148** of 1,176,868 |
| candidates per original | **3.4** (worst original 12) |
| precision on the 188 human-labelled pairs | **74.2 %** (95 % Wilson 57–86 %) |
| base rate of that labelled pool | 20.2 % |
| recall of the 38 human-confirmed leaks | **60.5 %** |
| pairs nothing had ever proposed | **102** |
| of those, on pages with no frame hash at all | **8** |
| cost of one full pass | 0.90 s, no request, no model, no dependency |

For comparison, ADR 0037's pre-registered precision budget was ≤ 5 candidate originals per page.

## Decision

**Add metadata scoring as a third signal: a candidate generator, fully retroactive, never
evidence.**

1. **The rule is two independent coincidences**, in `matching/metadata_score.py` (pure, no ORM, no
   clock, no I/O), each with the measured number behind it in a named constant:
   - `cast_and_duration` — the page credits one of the original's performers **and** the durations
     are within `CAST_DURATION_TOLERANCE_S` (3 s). The creator link is what makes this affordable:
     one original's cast matches ~581 of 26,747 active pages, a 98 % cut of the search space.
   - `duration_and_title` — the durations are within the ordinary `matching.duration` tolerance
     **and** the titles share `TITLE_MIN_JACCARD` (0.3) of their tokens. This clause requires **no
     creator link on purpose**: 54.8 % of active pages credit nobody and it is the only part of
     the rule that can ever reach them. Measured: it costs nothing today (every pair satisfying it
     already has a link), so it is capacity for a case we cannot yet see rather than volume we pay
     for now.

   Rejected variants and their numbers are in the measurement's §5. The single best affordable
   cell was `creator + ±3 s` (75.0 % precision, 110 pairs); widening to ±10 s bought 5 points of
   recall and lost 7 of precision; dropping the creator link from the first clause raised the
   volume from 148 to 1,742.

2. **There is no score.** `score_metadata` returns the reason that admitted the pair, or why none
   did — never a number. A number here would invite the one mistake this signal must not make,
   being compared against the confidence of a real frame match.

3. **Duration is an indicator, never a hard filter** (standing operator rule: clips are leaks).
   The module only ever *admits* on a duration coincidence. It never rejects a pair, never writes a
   negative verdict, never changes a status and never touches another signal's row. A page shorter
   than its original earns nothing and loses nothing — measured, "the page is clearly shorter" is
   true of 18.4 % of confirmed leaks and 42.7 % of human-rejected pairs, so it is not a signal in
   either direction and there is no reason code for it.

4. **Role, and the invariant that holds it there.** The signal may only create a `ReferenceMatch`
   with `candidate_source='metadata'`, carrying **zero evidence frames, zero matched frames, no
   timeline, confidence 0 and band `review`** — the human review queue and nothing else. It is
   enforced three times independently, exactly as ADR 0037 does it:
   - a database `CheckConstraint` (`reference_match_metadata_is_never_evidence`) — its **own**
     constraint beside the embedding's, not a widened version of it, so each signal's wall fails
     with its own name and ADR 0037's guarantee is not collateral of an edit to this one;
   - `matching.confidence.band_for(metadata_only=True)`, honoured before anything else is looked
     at, so no combination of confidence and frame counts routes a row past it;
   - `matching.calibration.apply_bands`, which excludes the row from re-banding. This is the guard
     that matters over time: calibration runs again whenever labels arrive, so a candidate row
     left in would be silently re-banded into `high` the first time the thresholds fell below its
     confidence.

   `ReferenceMatch.CANDIDATE_ONLY_SOURCES` names the set once, so a fourth signal cannot be added
   to the enum and forgotten at the exclusions.

5. **Promotion uses the paths that already exist.** A later PDQ match takes the same
   `(reference, page)` row over and sets `candidate_source='pdq'`
   (`matching.references.match_references`), or a human confirms it in review. A pair PDQ — or the
   embedding — already has a row for is never touched: two candidate generators do not overwrite
   each other's questions.

6. **`best_distance` is 256.** Every other writer puts a real Hamming distance there. No picture
   was compared here, so the field gets the one value a 256-bit PDQ comparison cannot produce: it
   reads as "no information" rather than as a close match to anyone skimming the admin, and nothing
   compares it against a threshold (thresholds live in settings and are read by the PDQ scan, which
   never sees these rows).

7. **No candidate table of its own**, unlike ADR 0037. An embedding candidate is per *frame* — a
   page frame's nearest reference moments — so it needs somewhere to live before being collapsed to
   a pair. A metadata candidate *is* a pair: the whole verdict fits in the match row's
   `confidence_signals`, and one pass costs under a second. An intermediate table would save
   nothing and would have to be kept correct.

8. **Off by default**, like the second signal, and for the one cost it has. It needs no model, no
   dependency, no request and under a second of CPU — but it costs **review work**, and how much
   human attention a candidate generator may consume is an operator decision, not a code default.
   `manage.py metadata_candidates [--dry-run]`, plus the same opt-in flag on `match_references`
   that ADR 0037 has.

9. **Photo collections are out of scope.** A collection has no duration
   (`reference_shape_matches_kind`), so neither clause can fire on one. They are reached by PDQ on
   the images themselves (ADR 0020).

10. **Not scheduled yet.** The scheduler gains no job in this ADR. The operator looks at one run's
    148 candidates first; scheduling is a separate, reversible decision once review has said what
    the precision really is.

### What this decision explicitly is not

**No biometrics, no face detection, no face recognition, no person identification, no age
estimation** — not as an output and not as an intermediate step. This is structural rather than
promised: the module's inputs are two floats, two sets of integers and two sets of strings, and it
opens no picture and imports nothing that could. The only person-shaped input is `Page.creators`,
a set of primary keys of staff-reviewed `catalog.Creator` rows that the crawler linked from a
site's own published performer credit — the same links ADR 0029's targeting and ADR 0038's
suppression already read.

**It is not a content signal, and it does not claim to be.** It says "this page's description is
consistent with that original", never "these are the same pictures". Only PDQ says the second
thing. That is the entire reason for § Decision 4.

## Consequences

- **Fully retroactive, which is the point.** Switching it on reaches every page already in the
  index, including the 9.9 % that hold no frame hash and that neither PDQ nor ADR 0037's sketch can
  ever see. 8 of the 102 new pairs it proposes today sit on exactly those pages; for them metadata
  is not a cheaper signal, it is the only one that exists.
- **The evidence in a notice is unchanged.** A notice needs `high` plus a human confirmation
  (ADR 0035), a metadata row can never be `high`, and `marceline/notices/gate.py` excludes the
  whole candidate set — so a rejected candidate cannot veto a notice the real match rows allow
  either.
- **Nothing customer-visible changes.** `marceline/app/originals.counts_as_match` excludes the
  candidate set, so such a row is not a customer's finding until a human confirms it, and a
  confirmed one reads `evidence: no_fingerprint_match`, which is exactly the case. No confidence,
  band, distance or candidate count reaches a customer schema.
- **The fields are the pirate site's own text.** A re-titled upload defeats the title clause, a
  stripped credit defeats the first, and a **forged duration is cheap to produce** and this signal
  has no defence against it. That is not a caveat on top of the design; it is why § Decision 4
  exists. If the signal could reach `high`, a spam farm could manufacture a notice.
- **A per-original cap, because the input is other people's data.**
  `MAX_CANDIDATES_PER_REFERENCE` (200) is more than an order of magnitude above today's worst
  original (12), so it changes nothing now. It turns a future data accident — a site that starts
  publishing one duration for its whole catalogue, a creator linked to thousands of pages — into a
  logged number instead of a flooded review queue, keeping the tightest coincidences.
- **The precision number is provisional.** 74.2 % rests on 31 labelled hits (95 % CI 57–86 %) and
  the positives were surfaced by paths that already require a creator link, so the recall figure is
  recall over *leaks we already find another way*. Nothing outward-facing — a published number, a
  coverage claim — may be built on either figure. Review's verdicts on the 102 new pairs are what
  will replace them.
- **PDQ distance cannot referee this signal**, and the measurement records the trap: 81.7 % of an
  earlier variant's new candidates sat 65+ bits from the original, which looks damning and means
  nothing — `second-signal.md` §4.2 measured two pictures 0.5 s apart in the same video at a median
  60 bits. A page whose only poster shows a different moment is 65+ by construction. Only a human
  can judge these rows, which is why their one destination is the review queue.
- **Tags are unusable here and were measured out, not designed out.** They are the best-covered
  page field (91.8 %) and the strongest page-against-page signal in §4.7 — but **an original has no
  tags**, so there is nothing to compare against. A *learned* tag profile per original would be
  circular with 38 labels over 44 originals and would make the signal's precision a function of its
  own past output. Written down in the measurement's §4 so it is not rediscovered as new.
- **The run is a full rebuild, not incremental**, like ADR 0037's candidate refresh and for the
  same reason: 0.90 s over the whole index today. Make it incremental when a measurement asks, not
  before — an incremental cache that is subtly wrong is worse than a slow correct one.
- **A third signal means a third thing review must tell apart.** `candidate_source` is now a
  column, a filter and a read-only field in the match admin; it was invisible there since
  ADR 0037, so a reviewer could not distinguish a candidate from real frame evidence at all. The
  review API already reports it, because confirming a candidate is a judgement about pictures, not
  agreement with a score.

## Amendment 2026-09-18: the output is a fetch instruction, not a review item

Decided by the operator on the day the signal was built, before it was ever switched on:

> "I'm not sure about metadata candidates. I think enabling them for human review opens the gate
> for too much human in the loop."

He is right, and the reason is already written down above in three places — Decision 4, the
"fields are the pirate site's own text" consequence, and §7 of the measurement. This amendment
takes that reasoning to its conclusion. **Nothing about "never evidence" is relaxed. The
destination changes: from a person to a fetch.**

### Why a review item was the wrong output

Every field this signal reads — the duration, the performer credit, the title — is **the pirate
site's own text about its own upload**. A re-title defeats the title clause, a stripped credit
defeats the cast clause, and a forged duration is cheap to produce. So a human shown a metadata
candidate is not judging our evidence; there is no evidence in the row. They are judging *the
site's claims*, from a page id, a duration and two titles, with no picture in front of them and
nothing to compare — 148 times per run, at a measured 74.2 % precision whose 95 % interval runs
from 57 % to 86 %. A "confirm" there would be an opinion about a description, recorded in the same
field where confirmations about pictures live, and read by the notice gate and the customer case
file as the same kind of fact. The volume is also the wrong shape: the operator is one person, and
a generator that is cheap to *produce* candidates is not cheap when its output is minutes of
someone's attention per row. Asking a human to referee the strongest hypothesis a pirate site's
own text supports is the most expensive possible way to use the cheapest signal we have.

### What the new output is

**A metadata candidate is an aiming signal: it says "this page is worth fingerprinting next", and
then PDQ decides, automatically.** It is one more source in the browser-capture triage of
ADR 0016 (`matching.triage.METADATA_CANDIDATE`), which is the machinery that already exists for
"we suspect this page and we do not have enough frames of it". If the capture confirms, an
ordinary `ReferenceMatch` with real evidence frames appears through the normal path
(`match_references`), banded by the ordinary thresholds, with pictures behind it. If it does not,
the candidate dies silently on the next triage refresh and nobody ever saw it.

Concretely:

1. **`matching.metadata_candidates` writes nothing at all.** `write_candidates` is gone; the
   module scores and reports (`aim()`), and `manage.py metadata_candidates` is a read-only
   report. No `ReferenceMatch` row with `candidate_source='metadata'` is created by anything, so
   Decision 4's "the human review queue and nothing else" becomes "no queue at all".
2. **`match_references --metadata-candidates` is gone**, with the call it made. Matching is the
   evidence path; a signal that produces no evidence has no business writing there.
3. **The queue it feeds is the browser-capture queue**, refreshed by the existing
   `triage_browser_capture` (scheduled daily) under every exclusion, budget and window ADR 0016
   already enforces. `MATCHING_METADATA_CANDIDATES_ENABLED` still decides whether the source
   counts at all, and is still **off**. That job's guard is unchanged and still asks for new or
   changed matches, leaks or safety flags since its last success — so in the rare case where a new
   original arrives and nothing else in the index moves at all, its aim waits for the next cycle.
   A day late costs nothing here, and a guard that fires on everything would cost a full pass of
   every source daily for no reason.
4. **The three guards stay, untouched**: the database constraint
   `reference_match_metadata_is_never_evidence`, `confidence.band_for(metadata_only=True)` and
   `calibration.apply_bands`' exclusion of `ReferenceMatch.CANDIDATE_ONLY_SOURCES`. They are not
   redundant now that nothing writes such a row — they are what makes "nothing writes such a row"
   an invariant rather than a fact about today's code. A row that exists on a machine where the
   signal ran before this amendment keeps its guards and its human decision; no migration deletes
   anything.

### Which fetch path, and why

Two paths could take a page: the cheap HTTP **media lane** (fetch the poster or preview URL and
PDQ-hash it) and the **browser capture** of ADR 0016 (open the page, sample frames from the
player, hash them, keep nothing). The code already divides them, and the division decides this:

- **Browser capture takes every page this signal aims at.** A metadata candidate is only
  interesting where PDQ has said nothing, and the reason PDQ has said nothing is almost always
  that the page's *only* picture is a poster of a different moment. Measured over today's 171
  candidates on 156 pages: **no page has a single pending or running media row** — 409 media rows
  are `done`, 44 `skipped`, 1 `failed`. There is nothing left for the media lane to fetch on any
  of them. Re-fetching a `done` row means requesting the same URL for the same bytes and getting
  the same hash; a `skipped` row was skipped by the safety gate or a host's robots.txt, and this
  signal may not overturn either. Only a browser can produce a frame of the video these pages
  actually carry, which is exactly what ADR 0016 exists for.
- **The media lane gets no change, deliberately.** It already prioritises the right rows: its
  claim order is expiring URLs, then *pages linked to a tracked creator*, and every candidate of
  the `cast_and_duration` clause is creator-linked by construction (122 of today's 171), so those
  pages already ride in the media queue's second-highest class before any original is even
  imported. And triage's existing `media_pending` exclusion says the rest: while a page's media is
  still queued, the cheap path runs first and capture waits for the next refresh. Adding a
  metadata term to `claim_media`'s ordering would mean a new persistent store for candidates
  (Decision 7 says no) and another `Exists` subquery in the hottest query in the system, one with
  a measured performance history, to reorder rows that are already ordered correctly and of which
  there are currently zero.

### What a metadata candidate must satisfy before it aims a browser

A page this signal names has been judged by **nothing of ours** — no picture, no human, no cluster.
The other four capture sources all originate in our own evidence (a PDQ match, a human decision, a
cluster link to a strong match), so they carry their own justification onto the page. This one does
not, and therefore carries two conditions the others do not:

1. **The ordinary media gate must already allow this page** (`index.services.page_media_allowed`,
   ADR 0018). If the page's own text is not scored clearly safe, the ordinary path would refuse to
   fetch a *poster* from it, so a browser — which pulls far more — may certainly not go there.
   This is checked when the source is built, on top of the deny the capture guard re-reads from
   the database immediately before the browser opens (`marceline.capture.guard.capture_denial`,
   which uses the same rule). **A metadata candidate may not become a way to reach a page the
   ordinary path would refuse**, and this is the amendment's load-bearing rule. Measured: **41 of
   today's 156 candidate pages (26 %) are refused by it** — 45 of the 171 pairs — and every page
   among them that holds no frame hash at all is refused for exactly that reason.
2. **The page must be frame-poor** — fewer than `triage.FEW_FRAME_HASHES` (10) quality-eligible
   frame hashes, the same threshold `confirmed_leak_without_frames` uses. Where PDQ has already
   had a proper look and said nothing, the site's own text is not a reason to look again with a
   browser. That drops another 29 pairs, and 11 more are already owned by another signal.

Everything else is ADR 0016's, unchanged and not re-implemented here: safety status and safety
matches, removed pages, disabled sites, inactive pages, age-verification and unapproved
self-declaration gates, login/paywall pages, pages with media still pending, pages already
captured, rejected and confirmed-high pairs, each host's robots.txt for every request the browser
makes, the embed-host allowlist, the per-site delay, ≤ 5 visits per site per day, ≤ 20 per day in
total, and the nightly window. The signal contributes a *reason to look*, never a permission.

### The cluster corroboration, built

A metadata candidate whose page shares a cluster or a mirror group with a page **confirmed** for
the same original is corroborated by a signal it did not come from — PDQ's own grouping, or a
human's. That is cheap here: `matching.capture_triage` already loads the confirmed pairs and both
membership structures for its `linked_cluster_member` source, so the corroboration is one more
`related_pages` call over data already in memory. A corroborated candidate keeps every condition
above and only ranks higher (`METADATA_CORROBORATED_WEIGHT` 0.55 instead of
`METADATA_CANDIDATE_WEIGHT` 0.3, still below a review-band match), and the seed page and the kind
of link are recorded in the source's details. It is a better *aim*, never a stronger claim — the
capture still has to produce frames and PDQ still has to match them. Today it fires on **none** of
the 171 candidates — the confirmed pairs and the candidate pages do not yet share a group — so it
is capacity for the case rather than a number now, which is also why it is worth having cost
nothing more than one call over structures already in memory.

### Consequences of the amendment

- **Nothing this signal does can be seen by a human as a finding any more.** The review queue,
  the match admin, the notice gate and the customer case file see exactly what they saw before it
  was switched on. What they may see *later* is an ordinary PDQ match with evidence frames, which
  is indistinguishable from one found any other way — because it is one.
- **The blind spot claim shrinks, and this is the honest part.** The original ADR's best argument
  was the 9.9 % of active pages that hold no frame hash and that no fingerprint can reach. Under
  this amendment those pages get nothing: measured today, **every** candidate page with no frame
  hash is one the media gate refuses, so it is not reachable by a browser either. Metadata is
  still the only signal that *sees* them; it is now a signal with no output there. The operator
  chose that knowingly — the alternative was a review item, and he declined it.
- **The cost of one run moves from review minutes to capture visits**, and the visits are capped
  by ADR 0016 rather than by this signal. Measured through the shipped path on 2026-09-18: the
  queue holds 49 candidate pages without this signal and 119 with it — **70 pages whose leading
  reason is `metadata_candidate`**, plus 6 that gain it as a secondary source. Their priorities
  are 0.36-0.51 against the 44 rows already open, so they are drained only by capacity the older
  sources leave over. At ≤ 5 visits per site per day and ≤ 20 per day in total, 70 pages are at
  least four nights of a capture budget that is currently entirely unused — and zero nights while
  `BROWSER_CAPTURE_ENABLED` stays off, which it is. Turning this signal on alone changes no
  request at all; it changes what the capture worker would do when the operator turns *that* on.
- **Against the request budgets that bind the crawl, one run costs nothing measurable.** A
  browser visit sends no `media`-kind request through `PoliteClient`, so it spends **none** of any
  site's `daily_media_request_budget` — 0 of the 1,000 a day of the site that contributes most of
  these candidates, and 0 of every other site's. The only counted requests a capture makes are
  robots.txt (`kind=robots`, `enforce=False`, cached per host per worker run), which land in the
  page budget of 3,000-9,000 a day and are never refused: a handful per night. The browser's own traffic is real bandwidth but is not in the ledger at all —
  that is a property of ADR 0016 (issue #74 records it), not something this amendment introduces,
  and it stays capped per visit at 24 frames, 120 s of sampling and 200 MiB.
- **A silent death is a real loss of information, accepted.** When a capture produces frames and
  PDQ does not match them, the fact that metadata once pointed there is not recorded anywhere. The
  capture row remembers that the page was visited and why; the pair does not become a "rejected"
  anything, so a later, better fingerprint may propose the same page again. That is the right
  default for a signal built on someone else's text: it should not be able to poison a pair.
- **The measurement in `docs/matching/metadata-scoring.md` still stands as measured** — the rule,
  its precision, its volume and its traps are unchanged. Only §8's "the only real cost is review
  work" is superseded by this amendment; the cost is now capture visits, and the precision figure
  has become an estimate of how often an aimed visit is worth its bandwidth rather than a claim
  a human has to act on.
- **The guard against a future revival is the constraint, not this text.** If someone later wants
  a metadata row in front of a human again, the database will still only accept it with zero
  evidence frames and band `review` — and this amendment is what they have to argue with.
