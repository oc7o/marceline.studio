# 0016 — Browser capture: triage queue, scoring, budgets, and the capture worker

Status: accepted (2026-09-15); capture enabled by the operator and implemented 2026-09-16
(issue #31); amended 2026-09-17 (every browser request follows the crawler's policy) and
2026-09-18 (one click on the page's own play control, per site, off by default — issue #78)

## Context

Single-poster sites can't produce a strong match by construction: `strong` and band `high` need
at least 2 evidence frames (ADR 0010/0015). A headless browser could sample frames from the full
video (`docs/research/headless-video-fingerprinting.md`), but one visit costs 2–5 minutes, about
1 CPU core, 1.5 GB RAM and 50–300 requests.
The operator (2026-09-15): *"Because the headless browser is really resource
consuming and slow we need very good triage for this. Scheduling for uncertain cases, not just
every random site."* The browser must never run on safety-flagged pages, and a video is scanned
only once.

This ADR covers **only the queue**: which pages would be worth a visit, in what order, and within
which budgets. Capturing is a separate, later spike. It needs an explicit operator decision.

## Decision

### Queue table

`matching.BrowserCaptureCandidate` has these fields:
- `page`, `reference` (nullable: the leading source's reference), `site`
- `reason` (leading source), `priority`, `signals` (every source with its component, frame
  counts, URL/media basis, `gate_checked`)
- `status` (`queued` / `skipped` / `captured` / `failed` / `expired`)
- `queued_at`, `decided_at`, `attempts`, `last_error`
- `capture_run`: an empty JSON placeholder for the future worker

A partial unique constraint allows one **open** row (queued or failed) per page. Skipped,
expired and captured rows are history.

### Sources and weights (`matching.triage`, pure)

Each source gives a reason code and a priority component for one (page, reference) pair.

| Reason | When | Component |
|---|---|---|
| `review_band_match` | a `ReferenceMatch` in band `review`, or `weak` (< 2 evidence frames, any band) | `REVIEW_MATCH_WEIGHT` 1.0 × (`REVIEW_MATCH_FLOOR` 0.6 + 0.4 × min(confidence / high threshold, 1)) + `REVIEW_EXACT_DURATION_BONUS` 0.1 if the page duration is within 1.5 s of the reference |
| `confirmed_leak_without_frames` | a confirmed `ReferenceLeak` or confirmed `ReferenceMatch` whose page has < `FEW_FRAME_HASHES` (10) quality-eligible frame hashes | `CONFIRMED_LEAK_WEIGHT` 0.7 |
| `linked_cluster_member` | an active cluster member or mirror-group page sharing a group with a current strong, non-rejected match for the reference, with no own `ReferenceMatch` for it (`leaks.related_pages`) | `LINKED_CLUSTER_WEIGHT` 0.6 |
| `exact_duration_single_thumb` | creator-linked page, duration within 1.5 s of a reference of that creator, ≤ `SINGLE_THUMB_MAX_FRAMES` (1) eligible frame hash, no `ReferenceMatch` for the pair | `EXACT_DURATION_WEIGHT` 0.4 |

The weights put an undecided near-high match first, because more frames decide it. A confirmed
leak without frames comes next: it gains evidence (weak → strong, calibration data) but no
decision. Cluster/mirror evidence and duration-only hints follow.

`combine_priority` computes one number per page: the largest component, plus
`SECONDARY_SOURCE_SHARE` (0.25) × every other component, plus `FRAME_POVERTY_WEIGHT` (0.15)
× (1 − eligible frames / 10). The result is rounded to 4 digits. Ties go to the lower page id.
"10 frames" is where the research table reaches ~85 % for 2 evidence frames at a per-frame hit
rate of 0.3. The weights are hand-set, like the confidence weights, until captures give data.

### Hard exclusions (never queued; counted per code in the command output)

Page-level exclusions drop the whole page. The first one that applies is counted.
- `safety_not_clear`: `Page.safety_status` is not `clear` (review or blocked; ADR 0011).
- `safety_matches`: any stored safety term match, even on a clear page.
- `removed`: availability `removed`.
- `site_disabled`: `Site.enabled` is false.
- `age_verification_provider`: the crawler recorded a verification provider on this page or
  anywhere on its site.
- `age_gate_unapproved`: a self-declaration gate was recorded on the page or site, and the site
  has no valid `Site.age_confirmation`.
- `private_or_login_page`: a login/account/members path, or a `private` / `login_required` /
  `members_only` / `premium` flag in the extraction. None are indexed today; this is a guard.
- `media_pending`: the page still has pending/running HTTP media. The cheap path goes first.
- `already_captured`: see "Scan once".

Pair-level exclusions drop one source:
- `rejected_pair`: a human rejected the pair in either table.
- `confirmed_high`: the pair is confirmed, band `high`, with ≥ `CONFIRMED_HIGH_MIN_EVIDENCE`
  (3) evidence frames. More frames would change nothing.

**Gate signals and module boundaries.** Gate outcomes exist only in crawler tables:
`CrawlTask.decision_reason` of skipped tasks (`age_self_declaration …`,
`age_verification_provider: …`). `matching` may not import `crawler`, so the service takes
injected `GateSignals`. The scheduler (project package, `marceline.scheduler.crawler_gate_signals`)
reads them and passes them to the command as a stealth option. A manual command run has no gate
signals: its rows get `signals.gate_checked = false`, and `next_capture_batch` never returns such
rows until the scheduler refreshes them. This fails closed without a new dependency direction.

### Scan once

A page with a `captured` row is not queued again. The same holds for a `skipped` row with a
non-empty `capture_run`, i.e. a skip by the future worker (DRM, block, no video, ad wall). The
exception is a page whose basis changed substantially. `signals.basis` holds the normalized URL
(host without `www`, path, no query), the duration and the media keys (`media_content_key`, else
host + path). The worker must keep the basis it captured. `changed_substantially` returns:
- `url_changed`: the normalized URL differs;
- `duration_changed`: both durations are known and differ by more than the cluster tolerance
  `max(30 s, 3 %)`, i.e. another video under the same page;
- `media_replaced`: both key sets are known and disjoint (every poster/frame/clip replaced).

Rotating tokens, query strings, small duration drift and a changed poster alongside kept media
don't count. Triage skips (a safety flag, for example) don't block a later queueing.

### Refresh (`triage_browser_capture [--dry-run] [--limit N]`)

Idempotent, in one transaction:
- Create rows for new candidates.
- Update priority, reason, reference and signals of open rows. A second run on the same data
  reports only `unchanged`.
- Re-queue failed rows below `MAX_CAPTURE_ATTEMPTS` (3).
- Mark open rows of pages that are now excluded `skipped` (`last_error = "triage: <code>"`).
- Expire open rows whose basis disappeared (`no capture source left`) or that fall outside
  `--limit` (the N best stay queued).
- Never touch captured, skipped or expired rows.

The output lists:
- counts (created/updated/unchanged/requeued/expired/skipped);
- per-reason counts, both as leading reason and as sources;
- skip counts per exclusion;
- the top 10 as indented `page, site, reason, priority` lines;
- the budget view for the current or next window.

`--dry-run` writes nothing.

### Budgets and window (wired in `settings.py` since 2026-09-16)

| Setting / env | Default | Meaning |
|---|---|---|
| `BROWSER_CAPTURE_ENABLED` | `False` | `next_capture_batch` returns nothing while false |
| `BROWSER_CAPTURE_PER_SITE_PER_DAY` | 5 | visits per site per window |
| `BROWSER_CAPTURE_TOTAL_PER_DAY` | 20 | visits in total per window |
| `BROWSER_CAPTURE_WINDOW_UTC` | `01:00-05:00` | daily window, UTC; may wrap midnight |
| `BROWSER_CAPTURE_MAX_CONCURRENCY` | 1 | batch size (browsers at once) |

`marceline/settings.py` reads them from exactly these env names, and `.env.example` documents
them. There is one window per day, so the daily budgets count visits (rows with `attempts > 0` and
`decided_at` since the window start) in the window containing `now`. A rolling 24 h budget
would let yesterday's window block today's.

`capture_triage.next_capture_batch(now)` is for the future worker only. It returns an empty list
when capture is disabled or `now` is outside the window. Otherwise it returns the best queued,
gate-checked rows within the concurrency, total and per-site budgets, re-checking safety status,
safety matches, removal and `Site.enabled` in the query. It is read-only: the worker must claim
rows with `SKIP LOCKED` and re-check robots.txt and gates live, as ADR 0007 requires.

### Scheduler

The `browser_capture_triage` job runs daily (`SCHEDULER_BROWSER_CAPTURE_TRIAGE_HOURS`) and is
declared after `match_references`. Its guard: a `ReferenceMatch` or `ReferenceLeak` created or
updated, or a page safety-flagged, since the last successful start. It only refreshes the queue.
**It runs even when `BROWSER_CAPTURE_ENABLED` is off.** Triage reads the database and writes
queue rows. It makes no request and opens no browser, and the table shows the operator what a
capture would cost before any decision.

### Admin

*Matching → Browser capture candidates*: view only, text only (page id, title, URL as plain text,
no links or images), filters for status, reason and site, sorted by priority. Deleting a row is
the operator's way to allow re-queueing.

## Capture needed an operator decision

Until 2026-09-16 nothing in this ADR captured, and no browser dependency existed. Capture waited
on: an operator decision; decisions on
capture method, allowlist and sandbox; and a worker design. The spike plan, success criteria and
the RAM-only rule for bytes are in
[`docs/research/headless-video-fingerprinting.md`](../research/headless-video-fingerprinting.md).

## Amendment 2026-09-16: the operator enabled capture (issue #31)

The decision this ADR was waiting for. The operator's framing is also the safety design:

> Capture whole videos only from pages where we have **evidence the content could be pirated** —
> a customer's original plausibly appears there — and only from sites that are **safe to touch**.

Capture is expensive, so it must be **aimed, never blanket**. Nothing changes about *what* is
queued: the worker takes the candidates the triage above already produces, in priority order,
inside the window and under the budgets. What is new is the visit itself.

The motivating gap is unchanged and concrete: site09.example serves exactly one WebP thumbnail per
video and site10 one AVIF poster. One image per page is where matching goes blind — about 40
crawled Sasha Meyer pages sat creator-linked and unmatched. Real frames from the player are the
recall unlock.

### What runs

`marceline/capture/`, in the project package because one capture needs `crawler` (robots.txt and
the block/challenge detector), `matching` (the queue and PDQ) and `index` (the page) together — a
combination no app may import, exactly like `marceline.scheduler`. CLI
`capture_worker [--site DOMAIN] [--limit N]`, holding a Postgres advisory lock so only one runs at
a time. It is deliberately **not** in `make workers`.

**The browser is driven through Node, not a Python package.** `capture.mjs` runs as a subprocess
speaking one JSON line at a time on stdin/stdout, and resolves Playwright from the harness install
`frontend/parity` already vendors for browser parity (issue #28), Chromium and the root-free NSS
fallback included. Adding `playwright` to `backend/pyproject.toml` would have meant a second
driver and a second browser download in the backend image for a feature that is off by default;
this way nothing `uv sync` installs is able to open a browser at all. Frames come back as base64
PNG and live in memory only.

### Capture method

Element screenshots of the `<video>` at seeked positions, as the research doc recommends: it is
compositor output, so a cross-origin stream taints no canvas and no tokenised player URL is ever
replayed outside the player. Playback starts muted, then `currentTime` is set and the frame is
taken once `seeked` plus one rendered frame have fired. Sampling is ~1 frame per minute, spread
evenly, skipping the first and last 15 s (intros, studio logos, black). That rule lives in
`marceline/capture/plan.py`, which is pure and tested, so the policy this ADR documents is the
policy that runs rather than something buried in browser code.

The worker waits (up to 15 s) for the player to report a finite `duration` before it samples
anything. The `<video>` element sits in the markup long before its metadata loads, so treating
"element present" as "video ready" made visits abort as `no_video` under load — a wasted visit,
and the one bug the synthetic end-to-end test caught that the unit tests could not.

### Safety: the deny is a property of the visit, not of the queue

`marceline/capture/guard.py` is the only place a browser may be pointed at a page. `capture_page`
re-reads the page's safety status, its score, its availability and its site's `enabled` flag from
the database **immediately before** the browser starts, and raises `CaptureDenied` instead of
starting one. That is deliberately the same ordering, and the same rule (`page_media_allowed`,
ADR 0018), that `index/media_fetch.py` enforces for image fetches: a browser pulls far more of a
page than a poster fetch does, so it can never be allowed where a poster fetch is not. A page
blocked, gated, put under review or removed *after* its candidate row was claimed therefore still
causes no visit. The queue filter in `next_capture_batch` remains defence in depth, never the
guarantee.

- **Nothing is worked around.** The loaded page is judged by `crawler.blocking.detect_block` — the
  crawler's own detector — so a login page, CAPTCHA, Cloudflare challenge, paywall, age
  self-declaration or age *verification* gate is abandoned with a recorded reason (ADR 0007). The
  browser sends no age-confirmation cookie at all, which is stricter than the HTTP crawler. DRM
  (an `encrypted` event or a non-null `video.mediaKeys`) is detected and abandoned, never
  decrypted.
- **Crawler policy still binds.** robots.txt is checked for the page URL and the per-site delay is
  waited out before the browser opens. A browser must not become a way around the crawler's own
  policy.
- **Hashes only.** Frames are hashed with the existing PDQ pipeline and dropped. The output is a
  `Media(kind=capture)` row — its URL is the page URL plus `#browser-capture` and its
  `eligible_at` is NULL forever, so the fetch layer refuses it and the media worker never claims
  it — plus `FrameHash` rows written through `store_hashes`, which re-checks the gate one last
  time and deletes everything if the page stopped being clearly safe mid-capture.
  `matching.timeline` orders capture frames like a clip, so `verify_timeline` can score them and
  loose 32–40 bit frames can count.
- Ads, trackers and decoration are refused by resource type (`image`, `font`, `stylesheet`,
  `websocket`, …), which is cost control and malware hygiene. Refusing to load something is not
  anti-bot evasion: the UA stays `CRAWLER_USER_AGENT` and nothing about the browser is spoofed.
- Asserted in tests, not just described: `marceline/tests/test_capture_worker.py` proves zero
  browser visits (and not even a robots.txt request) for blocked, review, gated, never-scored,
  removed and disabled-site pages, including a page denied *while robots.txt is being read* — the
  case only a check inside the call that starts the browser can catch.
  `test_capture_browser.py` proves the same end to end against a real Chromium: the local test
  server records that it was never contacted.

### Caps and cost

Every cap is a field of `CapturePlan`, so no code path can sample more than the plan allows:

| Setting | Default | What it bounds |
|---|---:|---|
| `BROWSER_CAPTURE_MAX_FRAMES` | 24 | frames sampled per video |
| `BROWSER_CAPTURE_MAX_SECONDS` | 120 | seconds in the seek-and-sample loop |
| `BROWSER_CAPTURE_MAX_BYTES` | 200 MiB | bytes pulled before further requests are aborted |
| `BROWSER_CAPTURE_TIMEOUT_S` | 300 | watchdog for the whole visit |
| `BROWSER_CAPTURE_MAX_CONCURRENCY` | 1 | browsers at once, plus an advisory lock: one worker |

**Measured on a synthetic local video, never on a real site.** `test_capture_browser.py` generates
an ffmpeg `testsrc2` clip (60 s, 320×240, VP8) and serves it from a temporary loopback HTTP
server; Chromium 153 on the dev machine:

| | One capture |
|---|---|
| Wall time | **4.8 s**, of which 4.2 s seeking and screenshotting |
| Bytes pulled | **3.6 MiB** — the whole clip, which a local player buffers entirely |
| HTTP requests | 2 (the page and the clip) |
| Frames | 20 sampled, 20 quality-eligible hashes stored |

So roughly **0.2 s per frame**, dominated by seek-and-render rather than by hashing. A real site
will cost more on both axes, and the research doc's estimate stands until a real run is measured:
50–300 requests and 30–180 MB per visit, minutes rather than seconds, because segments arrive over
the network and the player is rarely a `<video>` with a direct `src`. The caps exist so that the
worst case is bounded rather than surprising, and every visit records its own wall time, byte
count and request count in `capture_run` — the placeholder this ADR reserved is now that record.

The byte meter counts a response's declared `Content-Length` when the response arrives and
replaces it with the transferred size once the request finishes. Without the first half, a video
still streaming when sampling stops is never counted: the first version of the meter reported a
whole 3.6 MiB clip as 0.01 MiB.

### What is still off, and what is still open

- `BROWSER_CAPTURE_ENABLED` is **`False` by default** and is the only switch that matters. Triage
  keeps running either way. The window (`01:00-05:00` UTC) and budgets (5 per site, 20 per day)
  are unchanged.
- The engineering rule the research doc demands — bytes in RAM only, nothing on disk — is
  implemented and test-covered.
- No separate process type, image or sandbox yet: capture runs from the operator's shell against
  the Playwright install on the host. The sandbox and per-site-process plan stays in the research
  doc for when capture outgrows aimed, budgeted use.
- `next_capture_batch` gained a `site_domain` filter so a run can be aimed at one site (the
  research doc's 5-visit spike on site04). Budgets still count every site's visits.
- A worker skip caused by the *page* (DRM, no video, a block) records a `capture_run` and so
  counts as "scan once". A skip caused by *our own policy* (a safety deny, robots.txt) records
  none, so triage may queue the page again once the reason disappears — a page a reviewer later
  clears must not be locked out forever.

## Amendment 2026-09-17: every browser request follows the crawler's policy (issue #78)

The first real trial (45 visits) found no playable video on 40 pages. The diagnosis
(`docs/research/browser-capture-no-video.md`, text-only, 12 visits plus synthetic engine tests)
also found a policy gap: robots.txt was checked for the page URL only, not for the
requests a page triggers (player iframe, video file). What changes:

- **Every request is asked for.** `capture.mjs` holds each request until the worker's
  `RequestPolicy` (`marceline/capture/requests.py`) answers. Refused outright: image, font,
  websocket, eventsource, manifest, texttrack, other. A subframe document must come from the
  page's site, an enabled `catalog.Site` or a `SiteHost` of the site (ad frames are refused).
  Every other request needs its host's robots.txt to allow it — 4xx allows, unreadable refuses,
  exactly the media-host rule of ADR 0007's amendment of the same day. robots.txt requests are
  counted for the candidate's site (`kind=robots`, not enforced), as before.
- **Stylesheets load.** Refusing them broke players' layout (a 1264×15 px player was hashed; a KVS
  player never became visible). Refusing decoration is still cost control, not evasion.
- **Sampling:** the largest drawn `<video>` with a duration across all frames, not the first
  element; a player drawn under 160×90 px is skipped; playback starts muted and is **paused**
  before seeking, so the screenshot is the picture that was sought and not the one a playing video
  has moved on to.
- **A capture position is metadata, never evidence** (operator, 2026-09-17). A player reports one
  position and can deliver another, and a leak is often an excerpt, so `capture_run.positions_s`
  is kept for cost and debugging only. Evidence stays what it already was: the sample *ordinal*
  (`matching.timeline`, `Ordering.CLIP`), scored as the longest in-order run, so the offset to the
  original falls out of the data and a single frame proves nothing. Measured that way on a
  synthetic clip, a Chromium screenshot is 1 bit (median) from the nearest frame of the video with
  offsets inside 0.04 s; Firefox 15 bits inside 0.11 s.
- **Skip reasons say why** and `capture_run.diagnostics` keeps text-only evidence (element states,
  player libraries, refusal counts by reason and request type — never a URL or pixel):
  `no_video: embedded player refused (…)`, `no_video: player requests refused (robots.txt)`,
  `no_video: click_to_play`, `no_video: media error N`, `no_video: player drawn at WxH px` count as
  scanned once; `unsupported_media: …` (the engine's codec gap) stays re-queueable.
- **Engine is a setting:** `BROWSER_CAPTURE_ENGINE` = `chromium` (default: Playwright's headless
  shell, H.264/AAC capable, closest frames, ~510 MiB) | `firefox` | `chrome`, and
  `BROWSER_CAPTURE_EXECUTABLE` for a binary Playwright did not install.

Still not allowed, and so still no video on those pages: **no click of any kind** (a click on a
site's own play control is an open operator decision; on site09.example it would also not be enough —
a 30 s ad, then the stream answers 403), no path robots.txt disallows (site21.example `/embed/*` and
`/get_file/`, site10.example `/get/`), no challenge or age gate worked around.
*(The click was decided the next day — see the amendment below. The robots.txt paths and the
challenge stay refused.)*

## Amendment 2026-09-18: one click on the page's own play control, per site, off by default (issue #78)

Until today the browser performed **no interaction at all**. The re-run of the trial set
(`docs/research/browser-capture-no-video.md`) ended 0 captured / 0 hashes, and its largest single
cause was `no_video: click_to_play` on 37 of 43 pages: the site builds its player only when its own
play control is pressed. The operator decided (2026-09-18) that **a single click on a page's own play
control is normal use** — it is exactly what the page invites every visitor to do — and separately
approved the **age self-declaration** gate of one site for the browser. The 403 challenge of another
site stays skipped: a challenge is never worked around.

This amendment says exactly what that buys, because everything else stays forbidden.

### The rule

**At most one click per visit**, and only on an element that the policy module
`marceline/capture/click.py` admits. That module is **pure**: `capture.mjs` describes elements, Python
decides, the browser clicks the index it is handed back — so the rule is testable without a browser
(`marceline/tests/test_capture_click.py`) and cannot drift into browser code. Every clause exists
because of a way a click could stop being "press play":

1. **Tag and ARIA role.** One of `CLICKABLE_TAGS` (`video`, `button`, `div`, `span`, `i`, `b`, `img`,
   `svg`, `canvas`) — never `a`, `form`, `input`, `iframe`, `label`. An explicit `role`, if the page
   set one, must be in `ALLOWED_ROLES`; `role="link"`, `role="checkbox"`, `role="menuitem"` are the
   page saying the element is something else, and something else is never clicked.
2. **It does not navigate.** Not an anchor with an href, not inside one, not a submit control. A
   click that leaves the page is a fetch instruction written by the page.
3. **Its frame is one we were allowed to load** — the main document, or a subframe the embed-host
   allowlist of the 2026-09-17 amendment already permitted. An ad or pop-under iframe is refused as a
   *document*, so it can never contribute a candidate at all.
4. **It is inside the page's own player.** Either the browser saw a `<video>` at it or within an
   ancestor a few levels up (structural, no token involved), or its own identifiers or an ancestor's
   name a player container. An element outside the player is never the play control, whatever it is
   called.
5. **It is drawn, and at least 24x24 px.** A one-pixel "play" element is an ad trick.
6. **Nothing about it, or about any ancestor, reads as another action.** `FORBIDDEN_TOKENS` covers
   consent, cookies, age and disclaimers, login/membership/subscription/payment, download, sharing,
   reporting, and advertising with its dismissals (`close`, `dismiss`, `skip`, `continue`, `next`).
   The check runs over the element's identifiers **and** its visible text, ARIA label, title and
   role, after splitting on separators *and* camelCase, so `closeAd` cannot hide. One forbidden token
   anywhere refuses the element outright, even if it also says "play". **This is what makes ad
   skipping structurally impossible**: a "Skip ad" control can never be named, so a pre-roll ends the
   visit exactly as it does today, with nothing.
7. **It reads as a play control**: the `<video>` itself, an identifier token naming a play control,
   or the player container itself — the click-to-play case, where the page draws a poster inside its
   player box and builds the player on the click.

Ties go to the largest drawn element, then the earliest: the same page decides the same way twice.
**Anything that does not match is no click**, and the visit is recorded with a reason
(`site not opted in`, `no candidate element`, `no element passes the play-control rule`).

### Where the click sits in the order of operations

After `crawler.blocking.detect_block` has judged the loaded page, and after the HTTP status and the
DRM probe. So a login page, CAPTCHA, challenge, paywall or age gate is abandoned **before any
interaction**, exactly as before — with the opt-in on and an admissible play control sitting right
there. Nothing else changes: the safety deny is still re-read from the database immediately before
the browser opens (`guard.capture_page`), robots.txt still answers for the page and for every
request's host, the embed-host allowlist still stands, and the frame, second, byte, per-site and
daily budgets are untouched.

Exactly one click, enforced three times over: `ClickPolicy` spends a budget of
`MAX_CLICKS_PER_VISIT` = 1, `runner._talk` has no loop (click, look again, decide — that is all the
protocol can express), and `capture.mjs` counts too. After the click the page is looked at once more
(`AFTER_CLICK_WAIT_MS`); if there is still no player the visit ends as
`no_video: no player after the play click`, which counts as scanned once.

**Never**: a second click, keyboard input, scrolling tricks, an ad skip, a consent or age click, a
click in a frame the allowlist did not permit, or a click on a cross-origin iframe's chrome.

### Per-site opt-in, default off

`catalog.Site.capture_play_click`, default `False`, admin-editable (*Sites*, list display and
filter). Like `capture_enabled` and `age_confirmation` it is deliberately **not** part of
`catalog.sources.SitePolicy` and is never re-applied by `seed_sources` — a reseed must not switch it
on behind the operator's back, and a test pins that. No site has it until the operator decides one
does.

### The age self-declaration, for the browser too

The browser used to send no cookie at all, which made it stricter than the HTTP crawler. It now sends
the approved self-declaration cookies of `Site.age_confirmation` — the same field, parsed by the same
`catalog.sources.parse_age_confirmation`, which refuses session, login and anti-bot cookie names
outright — read fresh from the database for each visit, scoped to the site's own domain. Those are
the only cookies the browser ever holds. An age **verification** provider is unchanged: triage
excludes the whole site, and `detect_block` abandons the visit. Approving a site is therefore a data
change (`Site.age_confirmation`), not a code change, and it is the operator's alone.

### What a visit records

`capture_run.diagnostics.click`: whether the click was `allowed` for the site, whether an element was
`chosen`, whether it was `performed`, how many candidates the page offered, the `kind`
(`video_element` / `play_control` / `player_container`), the **selector shape**, the refusal
`reason`, and whether a player existed afterwards. The shape is the tag, the ARIA role and up to
three identifier tokens that still look like markup — **never the element's text, never its ARIA
label or title, never a URL**. Text and label are read to *refuse* an element; they are never stored.
`capture_worker --refusal-report` sums it up per run of rows: how often the click was allowed, chosen
and performed, which shapes were clicked, and which refusal reasons came up. That is the number the
operator reads to decide whether the click earns its place on a site.

### Consequences

- The click can only ever *aim* the existing capture; it changes no threshold and no evidence rule. A
  frame captured after a click is an ordinary capture frame.
- If a site answers a click with a pre-roll ad, we get nothing and the page is marked scanned once.
  That is the intended outcome, not a gap to close.
- The denylist is conservative and will occasionally refuse a legitimate play control (a themed
  button that happens to say "start", for instance). That is the right direction to be wrong in; the
  refusal shows up in the report as `no element passes the play-control rule` with the site named.

## Evaluation (dev DB, READ ONLY transaction, 2026-09-15)

The table isn't migrated on the dev DB, so captured rows were taken as none and budget use as
zero. Gate signals came from crawler tasks: none recorded.
- **6 candidates.** Leading reason: `review_band_match` 3, `exact_duration_single_thumb` 3.
  Sources: `review_band_match` 3, `confirmed_leak_without_frames` 3,
  `exact_duration_single_thumb` 3, `linked_cluster_member` 0.
- **Exclusions:** `rejected_pair` 1. No safety, removed, gate or pending-media exclusion among
  sourced pages.
- **Top:**

  | Page | Site | Reason | Priority | Ref |
  |---|---|---|---|---|
  | 2616 | site04 | review match + confirmed leak, exact duration, 1 frame | 1.3273 | 1 |
  | 8686 | site18 | review match + confirmed match, exact duration, 1 frame | 1.2633 | 2 |
  | 6650 | site04 | review match + confirmed match, 1 frame | 1.1633 | 6 |
  | 6434 | site04 | exact duration | 0.5350 | 5 |
  | 6782 | site04 | exact duration | 0.5350 | 7 |
  | 8683 | site18 | exact duration | 0.5350 | 3 |

- **Page 7406** (site03, ref 5, confirmed, band high, 6 evidence frames, 22 frame hashes) has no
  source. It would also be excluded as `confirmed_high`.
- **Page 2616** has its own match, so it is not a `linked_cluster_member` of cluster 306.

A 4-site test run fills the 20-visit window with the 3 known uncertain pages plus 3 duration hints.

## Consequences

- The queue stays small and explainable: every row carries its sources and components.
- Triage costs a few queries per day.
- Weights are guesses until captures give hit rates; revisit with the spike results.
- The guard sees updates, not deletions. A match deleted without any other change expires its
  row only at the next triggered run. `next_capture_batch` re-checks safety and removal anyway.
- Visit counting uses `decided_at` per row. A re-queued failed row that is visited again in the
  same window counts once. Exact per-visit accounting belongs to the capture-run table the worker
  ADR will add (`capture_run` is its placeholder).
- A verification provider seen on any page of a site excludes the whole site: conservative, and
  the site is usually disabled anyway (site25.example).
