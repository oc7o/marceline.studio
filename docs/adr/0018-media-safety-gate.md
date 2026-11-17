# 0018 — Media safety gate: score the page before fingerprinting it

Status: accepted (2026-09-16)

## Context

ADR 0011 gives every page a text verdict: `block`, `review` or `clear`. That verdict decides
whether media may be fetched. That left one gap:
the crawler **automatically fetching preview images from pages whose own text has never
been assessed**. The intended order is the opposite — fetch only the HTML and text
needed for classification, and run the safety rules before any media request.

The hole was structural, not accidental. Every adapter attaches the card thumbnail it sees on a
listing page to the detail page's `TaskSpec` (`_card_media` in `crawler/sites/*.py`). Those media
rows were created — and therefore fetchable — before the detail page's own title, tags,
description and uploader had ever been parsed or checked. The text gate was real, but a listing
card could walk straight past it. At the time of writing the index held 15,869 posters, 29,808
frames and 3,693 previews across 12,513 pages.

One possible control is to disable media retrieval for a site after repeated `block`
incidents. The operator decided against switching sources off (2026-09-16), verbatim:

> "we don't stop complete media retrieval for those sites flagged but we enforce guard rails.
> First we fetch text and if it's safe enough (maybe score) we fingerprint. No bulk fingerprinting
> for lists or overview pages. We only fingerprint what is highly secure."

## Decision

### 1. Eligibility lives on the media row

`index.Media.eligible_at` (nullable) carries the permission. `NULL` means *never request this*.
A row only becomes eligible in `crawler.services.store_parse_result`, at the moment the detail page
itself has been fetched, parsed, scored and found clearly safe. Card media carried in the crawl
task's context becomes eligible there too — never on the listing that discovered it.

The alternative was to stop materialising card media until the detail parse lands. It was rejected
because the URL is worth keeping even when it may not be fetched: expiring site03 links are
recovered from exactly these rows, and an explicit inert row is auditable in the admin, where
"this media exists and is deliberately not fetchable" is visible. Discovery stays, permission goes.

A listing may still attach *fresh* links to a page that was already parsed and scored clearly safe
(replacing expired ones). That page's own text was assessed, so this is not the hole above.

### 2. A per-page score, not only the binary term hit

`crawler/safety_score.py` is pure: `score_page(texts, verdict, site_risk)`. It starts at 1.0 and
only ever subtracts, so a page nobody knows much about never scores high.

| Signal | Default | What it is |
|---|---:|---|
| `block_term` / `review_term` | → 0.0 | An ADR 0011 term hit. Hard deny, unchanged. |
| `age_pattern` | −0.60 | An under-18 age in a unit the seeded block regex misses (`15 jahre`, `15 лет`). |
| `minor_context` | −0.15 per cluster, max −0.30 | School, family and "young" wording clusters. |
| `bare_low_age` | −0.10 | A bare 11–17 token. Noisy (resolutions, dates, part numbers), so only a nudge. |
| `uncovered_language` | −0.15 | ≥ 30 % Cyrillic letters: the term list barely covers the text. |
| `thin_text` | −0.10 | Fewer than 4 words were parsed, so the term check had almost nothing to judge. |
| `site_risk` | −0.10 per recent `block`, max −0.30 | Recent `block` incidents on the same site. |

`SAFETY_GATE_MIN_SCORE` (default **0.80**) is the "clearly safe" threshold;
`SAFETY_GATE_BLOCK_WINDOW_DAYS` (default 90) is how far back a block counts. At 0.80 a page passes
only when nothing fired or a single mild signal did: any two independent concerns, or one strong
one, hold its media back. The score is stored on `index.Page` with the deciding reason and every
signal that fired (`safety_score`, `safety_score_reason`, `safety_score_signals`).

Near misses are deliberately **not** `catalog.SafetyTerm`s: they never block a page and never queue
a review, they only hold media back. Generic leak-site vocabulary (such as "leaked")
was measured and rejected as a signal — it fires on 66 % of site22 and 26 % of site19 pages
and means nothing there. Uploader/performer signals were
measured and dropped: site01, site06 and site22 extract no performers at all, so "no performer"
would have gated three entire sites.

### 3. Only clearly safe pages are fingerprinted

Media is fetched and hashed only when the page is `clear` **and** scored at or above the threshold.
A page that was never scored is not allowed: nothing has judged its own text yet. Everything else
stays text-only — indexed, searchable, matchable by title and duration, with no hashes.

### 4. The deny is enforced at the fetch layer (issue #65)

`index/media_fetch.py` is the only place media bytes may be requested. `fetch_media` re-reads the
page's safety status, its score and the row's eligibility from the database *immediately before*
the request and raises `MediaFetchDenied` instead of opening a socket. Both fetch paths — the media
worker and the moderation UI's image passthrough — go through it, so a page blocked, put under
review or gated *after* its media was claimed still cannot cause an HTTP request. The queue query
and the worker's in-memory pre-check remain as defence in depth, not as the guarantee.

### 5. Recomputation

Scores are recomputed on every re-crawl and by `safety_scan` (terms change, site risk changes),
which also re-applies the gate: pages that are no longer clearly safe lose eligibility, pages that
became clearly safe get their held-back media released. `safety_scan` is therefore also the
backfill for pages indexed before the gate existed. Clearing a page in the review queue rescores it,
so a reviewer's decision is followed by a fresh gate decision rather than an unconditional release.

### What is deliberately not gated

- **Text indexing.** Title, tags, duration, uploader, creator links and availability rechecks are
  unaffected; a gated page is a full index entry without hashes.
- **Hashes already stored.** Gating is not blocking: a page that drops below the threshold keeps
  the hashes it already had. Only `block` deletes hashes (ADR 0011).
- **Hash reuse from already-hashed media.** Copying existing hashes causes no request.
- **Pages under review.** Their media keeps waiting as `pending` exactly as before; it simply is
  not eligible. Only a `clear` page below the threshold is skipped with reason `safety gate`.
- **The operator's own reference videos.** They are local files, never crawled.

## Consequences

Measured on the live index (2026-09-16, 12,656 pages, `m2_report` distance 31):

- **183 of 12,656 clear pages (1.4 %) become text-only** at the default threshold: 181 of 939
  site06 pages (19.3 %, Russian text on the only site with recent `block` incidents) and 2 of
  2,147 site02 pages (0.1 %). Every other site: zero.
- **Recall is unchanged.** 10 confirmed (reference, page) pairs; before the gate strong 4, weak 5,
  linked 1, found 10 — **100.0 %**. After the gate at 0.80: identical, **100.0 %**. None of the 183
  gated pages currently has a match within distance 31.
- **0.90 would cost recall**: 1,073 pages gated, site06 entirely, and confirmed leak pages 11476
  and 11484 lose their hashes — found drops to 8, recall **80.0 %**. That is the reason the default
  is 0.80 and not higher.
- The site06 concentration is the intended behaviour, and it is the per-site control
  applied proportionately: the site keeps being crawled and indexed, but the pages whose language
  the term list barely covers are no longer fingerprinted automatically.
- Media rows of gated pages stay in the database as skipped, inert URL records. They cost storage
  and show up in queue counts; `safety_scan --gate-report` reports them per site.
- The score is not a probability and carries no statistical claim. It is an ordering with
  documented cut-offs, and it will need revisiting as terms, sites and languages change.

Hash-list matching against known CSAM (ADR 0011, "Later") remains the proper long-term control.
This gate narrows the window in which an unassessed page can cause a fetch.

## Amendment (2026-09-17): rule actions, auto-blocks and the customer release queue

Operator decisions of 2026-09-17, implemented exactly (docs/safety.md, "Rule actions"):

1. **Non-consent auto-block.** "rape" (rape, raped, rapes, raping) and "forced" block a page at
   once as `non_consent`, with no human review step, recorded with the matched rule and the rule
   set version, reversible by staff with a note. No incident is opened, because only a
   title is held; docs/safety.md, "Why these open no case".
2. **CSAM suspect.** "lolita" (lolita, lolitas) blocks at once in the most severe category that
   exists, `minor`, and opens a safety incident automatically (the existing record and its
   deduplication) for the operator to report by hand. Nothing is sent externally.
3. **Flagged term.** "flagged term" stays blocked by default. Only a page
   that credits a customer's rights holder and matches nothing else goes to a text-only staff
   queue, where a logged human release lets the ordinary gate decide. Any other hit keeps it
   blocked, and a later hit revokes the release.

**Decision.** `catalog.SafetyTerm.action` is one of `review | auto_block | auto_block_and_incident
| customer_release_queue` (`catalog/safety_rules.py`); every action but `review` blocks, and an
unknown value blocks and counts as incident-worthy. The former `block` terms became
`auto_block_and_incident`: `safety_incidents` already opened a case for each such page, so only
the timing changed (at crawl time instead of at the next command run).

- **Rule set versions are content-addressed** (`sr-` + 12 hex of a digest over every enabled
  term), so admin edits version themselves; `catalog.SafetyRuleSet` stores the snapshot the first
  time a decision uses a version. `index.SafetyRuleDecision` is the append-only log (rule terms,
  version, creator, staff user, note — never page content).
- **A human decision covers a term only at the action it was made under.**
  `crawler.safety.resolve` compares term and action strictness: a page cleared while "rape" was a
  review term is blocked by the new rule. Legacy `block` equals `auto_block_and_incident`, so the
  rename re-judges nothing.
- **The crawler cannot import the project layer**, yet incidents need mirror/cluster evidence and
  the queue needs ownership. `crawler.services.store_parse_result` sends
  `index.signals.safety_rules_applied` after commit; `marceline.apps` connects
  `marceline.safety_rules.after_store`. A receiver failure is logged and never breaks the crawl;
  `safety_scan`, `safety_incidents` and the scheduler job `safety_release_queue` catch up. No
  dependency direction changes: `index` defines the signal, the project layer listens.
- **A release is a status change, not a gate exception.** `page_media_allowed(status, score)` keeps
  its signature and its single rule. The release makes the page `clear` and reviewed for exactly
  its `customer_release_queue` terms, after re-checking the current text and rules under a row
  lock; the score still has to reach `SAFETY_GATE_MIN_SCORE`. The "no other category" condition is
  enforced wherever a verdict can change: at release (refused) and at every re-crawl or scan
  (`resolve` blocks on any new term, `_safety_fields` marks the release `revoked`). The admin
  refuses to unblock such a page by hand.
- **Site risk counts hotline cases only.** Pages blocked only by `auto_block` or
  `customer_release_queue` terms do not add to a site's risk factor: they were review pages before
  and never counted. Counting the several hundred of them on the live index would have capped the
  penalty at −0.30 on the affected sites and gated every page there.
- **Customer creators are rights holders only** (`TargetKind.RIGHTS_HOLDER`); co-performers,
  watchlist entries and prospects never queue a page. Credit uses the crawler's own linking rule
  (whole words in title, tags or performer credits; compilations credit nobody), because several
  sites extract no performer credits at all.

**Consequences.** Pages under these terms leave the review queue for good. The operator's safety
mail (issue #40) still reports every newly blocked page, so the first `safety_scan` after the
migration mails one batch naming them. Released pages remain invisible to customers
(`marceline/app/safety.py` excludes any page that ever matched a term).
