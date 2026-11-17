# Crawler and content safety

What must not break here, and the single place each rule is answered. The full detail — counter
schemas, delay tables, runtime internals and the narrative behind each rule — moved verbatim to
`docs/conventions/crawler.md` on 2026-09-18; the reasoning is in the ADR each line names, and
`docs/safety.md` is the operator's guide to the safety side. The root `CLAUDE.md` hard rules and
`backend/CLAUDE.md` apply here too.

## Requests
- **HTTP goes through `core/http.py`** (UA from settings, timeouts, per-host delay, size cap,
  retries on 429/5xx). **Never call httpx directly from crawler code.**
- **Request accounting is not optional** (issue #74, ADR 0007 amendment 2026-09-17): build every
  `PoliteClient` with `observer=RequestAccounting()` (`catalog/request_accounting.py`) and label
  every request (`RequestLabel(kind, site_id=…)`).
  `marceline/tests/test_request_accounting_coverage.py` fails on an unobserved client or a new
  direct httpx client. Counters land in `catalog.RequestCount`; their names are stable (ADR 0030
  reads them).
- Budgets are rolling 24 h buckets, refusals excluded: origin `page` (page + robots) =
  `Site.daily_request_budget`, origin `media` = `Site.daily_media_request_budget`, `cdn` per host
  across sites = `SiteHost.daily_request_budget` or `catalog.sources.CDN_HOST_DAILY_REQUEST_BUDGET`.
  A refusal raises `BudgetExhaustedError`, flushes the counters and **sends nothing**; the crawler
  releases the task with its attempt refunded and the media worker calls
  `matching.media.defer_media`.
- **Checks that must run immediately before a request go in `before_request`** (safety deny,
  robots.txt for media), so what they refuse never touches a budget. `RequestObserver.admit` runs
  after `before_request` and the `public_only` check.

## Adapters
- Implement `SiteAdapter` from `crawler/sites/base.py`: `seed_tasks`, `search_tasks`,
  `listing_tasks` and `parse`. **Never put a broad listing in `seed_tasks`** — a start URL about no
  creator belongs in `listing_tasks()`, which only `crawl_seed --listings` enqueues (ADR 0029).
  Helpers in `crawler/sites/common.py`; register in `crawler/sites/registry.py` (re-read it right
  before editing — it is shared).
- **Adapters are pure:** no I/O, no DB, no clock. They return `PageData`, `TaskSpec` links and
  `MediaSpec`s; the worker handles robots.txt, delays, outcomes, URL dedupe, the on-site link
  filter, creator linking and upserts.
- **Breadth-first listing walks** (`crawler/listing_walk.py`, ADR 0029 amendment 2026-09-18): an
  adapter that can show a card's title puts it in `TaskSpec.text` and declares `card_text = True`
  (`crawler.sites.base.has_card_text`) — the protocol's one optional capability; adapters stay pure
  and know no creator. The walk queues one frontier task at a time and a detail task only for cards
  `TitleGate.admits` (deliberately a little wider than the `linked` rule: a card with no text is
  admitted, because a false negative is a leak we never see). **The walk's state is the queue**, so
  a walk that was skipped or failed resumes at the page it died on, never at page 1.
- **Catalogue totals** (issue #107, ADR 0049): the protocol's *second* optional capability, the
  method `catalogue_total(kind, url, html) -> CatalogueTotal | None`
  (`crawler.sites.base.catalogue_total` asks for it, absent on every adapter that does not declare
  it). It may read **only the HTML it was handed** — an adapter never fetches a sitemap, never
  walks to a last page, never requests anything. `crawler/catalogue_totals.py` records what it
  returns (`MIN_PLAUSIBLE_TOTAL` keeps a per-tag count out of the denominator), and the worker's
  call is best effort: a figure is never worth losing a page over. `source_path` is a path shape,
  never a URL.
- **An empty media tuple is not a break on every site** (ADR 0042 amendment 2026-09-19). Three
  adapters (`site04`, `site02`, `site07`) parse video pages that carry no image of their own,
  so every poster and frame travels with the task from the card that linked the page and the detail
  parse returns none *when it works*. They declare `card_media_only = True` — the protocol's third
  optional capability, asked through `crawler.sites.base.has_card_media_only` — and for them
  `crawler/adapter_health.py` counts `no_media` as context only, like duration and performers. The
  flag is read from the registry when the verdict is judged, **never stored beside the counters**.
  Declare it only with a fixture test showing the detail parse yields no media, and never to quieten
  an adapter that used to yield some: it is the alarm for that site being cut in half.
- **A picture-set post is its own page kind** (issue #118): `crawler.drafting.classify` returns
  `gallery` for a JSON-LD `ImageGallery` or enough same-shaped pictures on a page with no video
  marker, and the draft names `MediaSpec(kind=Media.Kind.PHOTO, …)`. A link family of *image files*
  is not evidence of a listing — a listing links pages, a picture set links the pictures.
- **Drafting a new adapter** (`crawler/drafting.py`, CLI `draft_adapter`, ADR 0043, issue #21):
  record the fixtures first, then read the draft. It is **pure** — files in, text out: no network,
  no database, no model, and **nothing it produces is ever loaded by the crawler**. Two rules it
  must keep: it prints **structure only** (selectors, templates, counts, lengths — never a title, a
  name, a tag or a page's URL; the values stay in the fixture), and a path template is a hypothesis
  a human commits into a registered adapter, **never something that reaches the queue**. The
  fixture test stays hand-written: a test whose assertions came from the analyser asserts nothing.
- **An adapter repair sends page text to a third party, so the safety terms judge it first**
  (ADR 0042 amendment 2026-09-18, issue #120, PR #112's review blocker). `crawler/text_gate.py`
  is the one refusal — safety terms loaded **fresh**, minor near misses, German non-consent wording,
  `SAFETY_GATE_MIN_SCORE`, plus a disabled site and a page already flagged — and
  `llm.extract_page_fields` calls it as its **first statement**, before the prompt, the `LLMCall`
  row and any client. It judges *exactly* the string that would leave (the cleaned, truncated
  excerpt, never the HTML). **Any exception from the gate is a refusal** (fail closed), and a
  refusal is an `LLMCall` row with an empty `input_sha256` and an `error` naming the **rule only**,
  which `repair.calls_today` excludes from the daily cap. `discover_sources` reads the same module —
  **never fork that list**, and never record the matched word on the repair path: a query is our
  text, a page's is not.
- **Credit flags** (issue #3): an adapter may put `COMPILATION_KEY` and `DROPPED_PERFORMERS_KEY`
  into `PageData.extra`. `crawler.services.creator_links` (pure) turns text and flags into links —
  **a compilation links no creator**, and a dropped name alone links nobody; `store_parse_result`
  removes links the flags rule out and recounts breadcrumbs. Backfill: `crawler/relink.py`, CLI
  `relink_flagged_pages`.

## Source discovery: kinds and coverage (ADR 0051, issue #118)
- **A candidate carries a coverage verdict, not only a kind** (`crawler/discovery/kinds.py`, pure):
  `coverable` / `link_intelligence` / `not_coverable` / `parked` / `unknown`. It restates ADR 0007
  at the candidate stage — a login, a CAPTCHA, a paywall, an age *verification* gate or a robots.txt
  disallow is `not_coverable`, because we may never work around any of them.
- **The verdict is computed from what the ≤ 3-request probe already recorded, and nothing else.**
  `crawler/discovery/structure.py` adds three counters off the *same* homepage — profile links,
  forum links, the biggest same-shaped picture family — **never an extra request**, and integers
  only: no URL, name or title is stored.
- **A `not_coverable` or `parked` candidate can never be approved**: a `CheckConstraint` on
  `SourceCandidate` refuses the row and the admin action skips it with the reason. **Never relax
  that constraint to onboard "just this one"** — the answer is a human decision recorded somewhere
  else, not an approved row.
- **Coverage is written only while the row is unreviewed.** An approved candidate keeps the
  operator's decision; `--reapply-rules` re-decides the rest and prints every change.
- **Creator-source templates run for target creators only.** The two `targets_only` markets refuse
  to plan without the caller's verified list of target names, and `crawler` may not import
  `marceline.targets` — so the entry point is `manage.py discover_creator_sources` in the project
  package, which **plans and prints and requests nothing**. Prospects are excluded by name. The
  existing query-safety refusal applies to every template and a test asserts each one.

## Discovery retention (ADR 0048, issue #103)
- **`crawler.SearchCall` is two records in one row**: ADR 0013's **spend ledger** (the rolling 24 h
  guard and the lifetime total are summed from it) and a record that this named person was searched
  for. So the row is never deleted — the admin's refusal of add/change/delete stays — and
  `crawler/retention.py` blanks `query` after `SEARCH_QUERY_RETENTION_DAYS`, stamping
  `query_scrubbed_at`. **Scrubbing the text is the deletion**; what is left is an accounting line.
- The same pass scrubs `SourceCandidate.evidence["queries"][*]["query"]` for candidates untouched
  for the same period. The sample's `rank`, `url` and `title` stay: they are the evidence that the
  domain carries copies, and a result title is the site's own words. `CandidateCreator` stays too —
  two integers, `CASCADE` on `Creator`, taken by the name pass.
- **A suppression scrubs, it does not delete** (`marceline/suppression.py` calls the same
  `scrub_search_calls`): deleting the rows removed the name *and* silently moved the accounting.

## Workers and the crawl runtime
- **One worker per site is enforced** (`core/locks.py`): `crawl_worker` and `media_worker` take a
  session-level Postgres advisory lock per site and a second worker for a locked site exits with a
  `CommandError`. **Never add a worker path that skips `advisory_locks`.**
- **Crawl runtime** (ADR 0030, issue #75, `marceline/runtime/`, CLI `crawl_runtime --lane
  pages|media`; runbook `docs/runbooks/crawl-runtime.md`). It lives in the project package because
  it drives `crawler.worker` and `matching.media_worker` together.
  - **Politeness lives in Postgres**: `PoliteClient(gate=…)` waits through a `core.http.HostGate`,
    and every runtime client uses `core.host_slots.DbHostGate` (one `core.HostSlot` row per host,
    leases, start-to-start delays on `clock_timestamp()`, the stricter of two callers' delays).
    **Never build a runtime path on the in-memory gate.** A refused check never touches
    `next_allowed_at`. The guarantees are in `core/host_slots.py` and `core/tests/test_host_slots.py`.
  - **The task threads are daemon threads and must stay so**: past the drain their rows are back in
    the queue and every lock is gone, so the process has to be free to exit — a lane restarts by
    *ending*, and a non-daemon thread that hangs would leave a live container that crawls nothing
    and is never restarted.
  - Workers are reused, not forked; one `PoliteClient` per thread (cookies and connections never
    cross threads). A `HostBusyError` hands the task back with its attempt refunded.
  - Hash children (`hash_pool.py`) import only `matching.media_hash` (Django-free; a subprocess
    test asserts it) and bytes travel over a pipe, never a file. `HashPoolError` means the child
    broke, not the media, so the worker **retries** the row; only `MediaDecodeError` is final.
  - Tests: `marceline/tests/test_crawl_runtime.py`.
- **Site outage pause** (`catalog.Site.paused_until` / `pause_reason` / `consecutive_failures`): an
  unreadable robots.txt pauses a site at once, `SITE_PAUSE_AFTER_FAILURES` connection failures in a
  row pause it too, pauses back off like task retries, the task that hit the pause goes back with
  its attempt refunded, `claim_task` skips paused sites, and any response resets the streak.
- **A machine-readable TDM/AI reservation is noticed, and only the clear case pauses** (tripwire T2
  of `docs/legal/crawl-position.md`, issue #105). The worker passes an observer to `RobotsCache`, so
  the robots.txt it fetches anyway is also described by `core.robots.describe_robots` (pure) and
  stored by `catalog.robots_policy.record` as `catalog.SiteRobotsPolicy` per (site, host) — agent
  tokens, booleans, a content hash; **never a path or a line of the file**, and never an extra
  request. Three outcomes, deliberately different: a group naming **our** token or a
  `TDM-Reservation` / `X-Robots-Tag: noai` header → pause with reason `tdm_reservation` (only while
  `ROBOTS_RESERVATION_PAUSE_ENABLED`, default off), task handed back, and `record_site_reachable`
  **never lifts that pause**; a group naming `*` → already obeyed by `parse_robots`, nothing new; a
  group naming only other companies' AI tokens → recorded, reported, alerted once, **never acted
  on** (the operator decides per site). `ROBOTS_RESERVATION_DETECTION_ENABLED` (on) and
  the pause flag both reach the per-site crawlers *and* the runtime lanes through
  `infra/compose.yaml`. Media hosts' robots.txt is **not** described yet: `matching/media_robots.py`
  passes no observer, and wiring one is a decision, not a tidy-up.

## Content safety and the media gate
- Terms and statuses: ADR 0011, `crawler/safety.py` (pure), applied in `crawler.services`. The
  crawl hot path reads terms and creators through per-process caches (`cached_safety_terms` /
  `cached_creators`, `REFERENCE_CACHE_TTL_S`), so an edited term reaches every crawler within two
  minutes; explicit rescoring (`rescore_page`, `scan_pages`) always loads the terms fresh.
- **Safety mail names identifiers, never content** (issue #40, `crawler/alerts.py`): page id, site
  domain, category, matched term, admin link — **never the page URL, title or text**. Only a block
  by a rule that opens an incident is even *eligible* for an immediate mail
  (`safety_rules.opens_incident`); every other block is counted into the one daily operator report
  (`crawler/safety_digest.py`, scheduler job `safety_digest`) by name (page id, domain, category,
  term, admin link), and one is sent every day, quiet or not (2026-09-19). **Since 2026-09-19 an
  incident-rule block itself no longer mails on its own by default either** — `core/operator_mail.py`
  gates it behind `OPERATOR_MAIL_IMMEDIATE` (comma list, kind `safety_incident`, empty by default);
  the daily report lists it either way, so nothing here can lose an event, only whether it also
  gets a mail of its own. Adapter alarms (`crawler/adapter_alerts.py`, kind `adapter_health`) and
  robots.txt TDM/AI reservations (`catalog/robots_alerts.py`, kind `tdm_reservation`) fold into the
  same daily report's Crawler section the same way. Off unless `SAFETY_ALERT_TO` and
  `SAFETY_ALERT_FROM` are set; transport is Django's `MAILERS` setting, and every network mail
  backend gets a timeout so a hung SMTP server cannot stall a worker.
- **Media gate (ADR 0018).** `crawler/safety_score.py` is pure and only ever subtracts: term hits
  short-circuit to 0.0, near misses and thin or uncovered text each cost a documented amount.
  Media is fetched only when the page is `clear` **and** scored >= `SAFETY_GATE_MIN_SCORE`, and **a
  page that was never scored is never allowed**. `index.services.page_media_allowed` is the single
  rule; `media_denial_reason` names the skip reason.
- **`index.Media.eligible_at` is the permission** (NULL = never request this). Only
  `store_parse_result` grants it, when the detail page's own text was parsed and scored. **Listing
  cards never grant it** — that was the hole issue #67 closed.
- **`index/media_fetch.py` is the only place media bytes may be requested.** `fetch_media` re-reads
  status, score and eligibility from the database immediately before the request and raises
  `MediaFetchDenied`. Both the media worker and `marceline/review/media.py` go through it. **Never
  call `client.get` on a media URL directly** — asserted in `matching/tests/test_media_gate.py`
  (zero requests, every kind, every denied state).
- The media worker's client is `public_only` like the review passthrough: the URL and every
  redirect hop must be http(s) to public addresses, and the connection goes to the checked address
  (DNS rebinding). A refused hop sends nothing.
- **Media hosts' own robots.txt** (ADR 0007 amendment 2026-09-17): `core/robots.py` is the one
  parser and cache; `matching/media_robots.py` `MediaRobots.check_for` runs after the deny, before
  every attempt and every redirect hop. Disallowed → `Media.Status.ROBOTS_BLOCKED`, final, never
  requested, existing hashes untouched. The review image passthrough uses the same check.
- **Safety writes race live workers; a block always wins.** Never write a verdict or apply the
  media gate from a page read earlier without re-checking it: `scan_pages` writes conditionally on
  the stamps it read and applies the gate from locked current rows, and `matching.media._page_gate`
  locks the page until the hashes are written. Safety mail is sent after commit
  (`transaction.on_commit`), never inside a transaction holding a page lock.

## Safety incidents and reports
- **Safety incidents** (issue #66): `index.SafetyIncident` + `SafetyIncidentPage` are the reporting
  record — status, category, term, timestamps, channel, hotline reference, decider, note, cluster id
  and one URL row per page, **never page content**. `index/incidents.py` owns the record, the four
  deduplication cases (`same_url`, `mirror`/`sha256`/`cluster`, `pdq`, `term_change`) and the
  retention policy, so one case produces one hotline report.
- `SafetyIncidentHash` holds the PDQ hashes `safety_scan` **moves** off a page it blocks by an
  incident rule (ADR 0011 amendment 2026-09-17): **for deduplication only** — never in
  `index.FrameHash`, no matching module may read the table (a test walks `matching/`), nothing is
  ever fetched for it, and the rows cascade with the case's retention. `index` may not import
  `matching`, so the "same content" evidence is assembled in `marceline/safety_incidents.py`.
- **Rule actions** (operator decisions 2026-09-17, ADR 0018 amendment): `SafetyTerm.action` is
  `review | auto_block | auto_block_and_incident | customer_release_queue`; meaning, strictness and
  helpers live in `catalog/safety_rules.py` (pure). **Every non-review action blocks; an
  unknown/legacy action is the strictest.** `crawler.safety.resolve` keeps a human decision only
  for (term, action no stricter than decided). `index.SafetyRuleDecision` is the append-only audit
  log. Consequences that need the project layer go through `index.signals.safety_rules_applied` →
  `marceline/safety_rules.py`, including the **customer release queue** (`Page.safety_release`:
  queued only when every match is a `customer_release_queue` term and a rights holder of an owned
  original is credited; release re-checks text and rules under a row lock; any later hit revokes).
  The staff API `marceline/review/safety_release.py` is **text only: no media ids, no URLs**.
- **Hotline report draft** (issue #66, `index/incident_report.py`): `render(facts, "de"|"en")` is
  pure and follows a fixed template. The draft carries the case's URLs, dates, **category
  (never the term)**, dedup evidence, a truthful media-fetch statement and `MARCELINE-SI-<id>` —
  **never titles, tags, text or media**. Open/reported cases only; settled → 409. **Change the
  wording in both languages at once and bump `TEMPLATE_VERSION`.**
- CLI: `safety_scan`, `safety_incidents`, `backfill_safety_incidents`, `safety_retention` — all
  idempotent, all `--dry-run` first (`docs/commands.md` has the flags).
- **`safety_retention` is a scheduler job now** (issue #100), daily, right before `page_retention`
  — closing a stale reported case here lifts `page_retention`'s own exclusion of open/reported
  incidents within the same pass. Off by default (`SAFETY_RETENTION_ENABLED`, same shape as
  `PAGE_RETENTION_ENABLED`); while off it is recorded skipped, with the reason, in `JobRun`. Both
  periods it applies stay code constants in `index.incidents`, never new settings.
