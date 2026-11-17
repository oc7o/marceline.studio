# Crawler and content safety: the full reference

`backend/crawler/CLAUDE.md` keeps the invariants an agent must not break; this file is the detail it
used to carry — counter schemas, delay tables, crawl-runtime internals, the exact wording of each
safety mechanism and the narrative each rule grew out of. Moved here verbatim on 2026-09-18 (issue
#79); nothing was removed. The decisions live in the ADRs, and `docs/safety.md` is the operator's
guide to the safety side.

---

# Crawler and content safety

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Crawler
- HTTP goes through `core/http.py` (UA from settings, timeouts, per-host delay, size cap,
  retries on 429/5xx). Never call httpx directly from crawler code.
- **Request accounting (issue #74, ADR 0007 amendment 2026-09-17).** Build every `PoliteClient`
  with `observer=RequestAccounting()` (`catalog/request_accounting.py`) and label every request
  (`RequestLabel(kind, site_id=…)`; kinds `page`, `robots`, `media`, `probe`, `rdap`,
  `search_api`, `review_image`; `enforce=False` counts without refusing).
  `marceline/tests/test_request_accounting_coverage.py` fails on an unobserved client or a new
  direct httpx client. The client derives `retry` / `redirect_hop` request types itself.
  - Counters: `catalog.RequestCount` (hour, site, host, role, kind, request_type, outcome →
    requests, bytes), buffered in-process and upserted in sorted batches; `RequestCountDaily` after
    30 days. Names are stable (ADR 0030 reads them).
  - Roles: `SiteHost` row, else own domain/subdomains = `origin`, other hosts = `cdn`; no site =
    `third_party`. Budgets (rolling 24 hour buckets, refusals excluded): origin `page` (kinds page +
    robots) = `Site.daily_request_budget`, origin `media` = `Site.daily_media_request_budget`, `cdn`
    per host across sites = lowest `SiteHost.daily_request_budget` or
    `CDN_HOST_DAILY_REQUEST_BUDGET`.
  - `RequestObserver.admit` runs after `before_request` and the `public_only` check: a refusal there
    raises `BudgetExhaustedError` (`scope` `origin:page` / `origin:media` / `cdn`, `host`,
    `resume_at` = next hour boundary), flushes the counters, and sends nothing. The crawler releases
    the task (attempt refunded, `next_attempt_at=resume_at`); the media worker calls
    `matching.media.defer_media`, which also holds that host's other pending media. Checks that must
    run immediately before a request (safety deny, robots.txt for media) go in `before_request`, so
    what they refuse never touches a budget.
  - Workers pass `sleep=idle_sleep(client)` so idle processes flush; `client.close()` flushes too.
- Site adapters (`crawler/sites/<site>.py`) implement `SiteAdapter` from `crawler/sites/base.py`:
  `seed_tasks(site, creators) -> list[TaskSpec]`, `search_tasks(site, query) -> list[TaskSpec]`
  (free-text SEARCH seeds, same slug rules as creator seeds), `listing_tasks(site)` (start URLs
  about no creator — newest uploads, whole catalogues; only `crawl_seed --listings` enqueues them,
  ADR 0029) and `parse(kind, url, html) -> ParseResult`. Never put a broad listing in `seed_tasks`.
- **Breadth-first listing walks (`crawler/listing_walk.py`, ADR 0029 amendment 2026-09-18).** An
  adapter whose listing cards show their video's title puts that text into `TaskSpec.text` and
  declares `card_text = True` (asked through `crawler.sites.base.has_card_text`) — the protocol's
  one optional capability; adapters stay pure and know no creator. `crawl_seed --listings` then
  walks that site's broad catalogue instead of queueing its whole pagination chain: one frontier
  task at a time (the nearest page ahead only), and a detail task only for cards `TitleGate.admits`
  — `names_person` against every `cached_creators` name, i.e. ADR 0029's `linked` rule applied to
  the card, deliberately a little wider (a card with no text is admitted, a false negative is a
  leak we never see). The walk's state is the queue: `CrawlTask.context` names its start URL,
  `store_parse_result` hands it to the successor, and `services.enqueue_listing_walks` starts one
  or re-queues a frontier that was skipped or failed — at the page it died on, never page 1.
- **Adapters are pure:** no I/O, no DB, no clock. They return `PageData`, `TaskSpec` links and
  `MediaSpec`s; the worker handles robots.txt, delays, block/404/5xx outcomes, URL dedupe, the
  on-site link filter, creator linking and upserts.
- **A site whose video pages carry no media of their own** (ADR 0042 amendment 2026-09-19) declares
  `card_media_only = True` — the protocol's third optional capability, asked through
  `crawler.sites.base.has_card_media_only`. On such a site (`site04`, `site02`, `site07`) the
  player is a script and the cards below belong to other videos, so every poster and frame travels
  with the task in `TaskSpec.media` from the card that linked the page, and the detail parse
  returns none *when it works*. Breakage detection then reads `no_media` as context, exactly like
  duration and performers, and blind means no page or no title
  (`crawler/adapter_health.py`). Declare it only with a fixture test that shows the detail parse
  yields no media, and never to quieten an adapter that used to yield some.
- Adapter helpers: `crawler/sites/common.py`. Register adapters in `crawler/sites/registry.py`.
- **Drafting a new adapter (`crawler/drafting.py`, CLI `draft_adapter`, ADR 0043, issue #21).**
  Record the fixtures first, then read the draft: engine fingerprint (a KVS theme is a `KvsSite`
  entry, not an adapter), link families as path templates with a `video_path` regex, the performer
  index, `card_text`, the search form, candidate selectors for title/duration/uploaded/credits
  grouped by which of them agree, and the removal marker. It is **pure**: files in, text out — no
  network, no database, no model, and nothing it produces is ever loaded by the crawler. Two rules
  it must keep: it prints **structure only** (selectors, templates, counts, lengths — never a
  title, a name, a tag or a page's URL; the values stay in the fixture), and a path template is a
  hypothesis a human commits into a registered adapter, never something that reaches the queue.
  The fixture test stays hand-written: a test whose assertions came from the analyser asserts
  nothing. ADR 0043 also records why no LLM drafting call is built and what would have to be true
  to add one.
- **Credit flags (issue #3).** An adapter may put `COMPILATION_KEY` (a compilation or multi-scene
  post) and `DROPPED_PERFORMERS_KEY` (credits it judged noise, with a reason) into
  `PageData.extra` (`crawler/sites/base.py`). `crawler.services.creator_links` (pure) turns text
  and flags into links: a compilation links no creator, a dropped name alone links nobody.
  `store_parse_result` removes links the flags rule out (breadcrumbs recounted). Backfill for
  stored pages: `crawler/relink.py`, CLI `relink_flagged_pages` (per-site classifier in
  `STORED_CREDITS`; site03 only so far).
- **One worker per site is enforced** (`core/locks.py`): `crawl_worker` and `media_worker` take a
  session-level Postgres advisory lock per site (separate key spaces, all sites without `--site`)
  and a second worker for a locked site exits with a `CommandError`. The scheduler uses its own
  single-key lock. Never add a worker path that skips `advisory_locks`.
- **Crawl runtime (ADR 0030, issue #75)** — `marceline/runtime/`, CLI `crawl_runtime --lane
  pages|media`. It lives in the project package because it drives `crawler.worker` and
  `matching.media_worker` together.
  - **Host gate.** `PoliteClient(gate=…)` waits for a host's turn through a `core.http.HostGate`:
    `acquire` → hooks and checks (safety deny, robots, `public_only`, budget) → `start` or
    `cancel` → `done`; retry backoffs go through `penalize`. `InMemoryHostGate` (default) is the
    old per-client delay; `core.host_slots.DbHostGate` keeps one `core.HostSlot` row per
    `host_key` (`www.` stripped) with a lease, start-to-start delays on `clock_timestamp()`, the
    stricter of two callers' delays, an admin floor (`min_delay_s`), host-wide penalties and lane
    alternation. A refused check never touches `next_allowed_at`. Every runtime client uses
    `DbHostGate`; never build a runtime path on the in-memory gate. `core/host_slots.py` and
    `core/tests/test_host_slots.py` hold the guarantees (threads with their own connections).
  - **Delays.** Pages and robots.txt: the site's effective delay, as before. Media:
    `matching.media_worker.slot_media_delay_s` — origin hosts (`HostRoles`, #74) the site's delay
    (sharing the pages' slot, no doubling), CDN hosts `CDN_HOST_DELAY_S`; the media host's robots
    `Crawl-delay` raises either. Legacy `media_delay_s` (×2 on the site's own host) stays for the
    per-site `media_worker`.
  - **Loop** (`runtime.py`): `SiteRotation` hands a site to one thread per lane at a time, round
    robin across sites with work; a site's claim or task failure is isolated to that site. The
    dispatcher (main thread) holds the lane lock (`RUNTIME_LOCKS`) and the per-site
    `CRAWL_WORKER_LOCKS`/`MEDIA_WORKER_LOCKS` of every served site on its own connection, checks
    them against `pg_locks` every minute, runs maintenance, logs a summary every 5 minutes and
    restarts the lane (graceful stop, exit 0) after migrations, 20,000 tasks, 24 h, a hung task or
    `RUNTIME_MAX_RSS_MB`. Shutdown cancels slot waits (`HostBusyError`, attempt refunded), drains
    for 80 s and hands still-RUNNING rows back with their attempt refunded (`work.release`).
    **The task threads are daemon threads and must stay so**: past the drain their rows are back
    in the queue and every lock is gone, so the process has to be free to exit — a lane restarts
    by *ending*, and a non-daemon thread that hangs (an uninterruptible `threading.Lock`, a
    socket without a timeout) would leave a live container that crawls nothing and is never
    restarted. A dispatcher database error drops every lock with the connection, so the lane
    stops serving at once (`_locks_lost`) and re-takes what is free on the next tick.
  - **Workers are reused, not forked:** `CrawlWorker(robots=shared RobotsCache)` and
    `MediaWorker(robots=shared MediaRobots, hasher=HashPool.hash, delay_for=…)`, one
    `PoliteClient` per thread (cookies and connections never cross threads). `HostBusyError` →
    `release_task` / `defer_media` with `retry_at`. `RobotsCache` is thread-safe per origin;
    `fetch_media` runs the media robots check in `on_redirect` too, so a redirect target's
    robots.txt is fetched before that host's turn is taken.
  - **Hash children** (`hash_pool.py`): `spawn`ed, importing only `matching.media_hash`
    (Django-free kind dispatch, `hash_media_bytes(kind, data)`; asserted by a subprocess test).
    Deadline `FFMPEG_TIMEOUT_S + 30` s → kill, respawn; recycled after 500 jobs or 300 MiB. Bytes
    travel over a pipe, never a file. `HashPoolError` is a
    `matching.media_hash.HasherUnavailableError`: the child broke, not the media, so the worker
    **retries** the row (`hash unavailable: …`) instead of failing it for good — MAX_ATTEMPTS
    still ends one that keeps killing children. Only `MediaDecodeError` is final.
  - Settings: `RUNTIME_SITES` (required: domains or `all`), `CDN_HOST_DELAY_S`, `RUNTIME_*_THREADS`,
    `RUNTIME_HASH_PROCESSES`, `RUNTIME_SLOT_MAX_WAIT_S`, `RUNTIME_*_DEADLINE_S` (whole-response
    deadline, `RequestDeadlineError`, not retried), `RUNTIME_MAX_*`. `RUNTIME_SITES` and
    `CDN_HOST_DELAY_S` are pinned in `OPERATIONAL_DEFAULTS`.
  - Tests: `marceline/tests/test_crawl_runtime.py` (rotation, fake-lane loop, coexistence with a
    legacy worker, shutdown, both lanes end to end on host slots, hash pool).
- **Site outage pause** (`catalog.Site.paused_until` / `pause_reason` / `consecutive_failures`,
  `crawler.services`): an unreadable robots.txt pauses a site at once, `SITE_PAUSE_AFTER_FAILURES`
  (3) connection failures in a row pause it too; pauses back off like task retries
  (`RETRY_BASE_DELAY` 1 min up to `RETRY_MAX_DELAY` 6 h). The task that hit the pause goes back
  with its attempt refunded, `claim_task` skips paused sites, and any response resets the streak.

## Before page text leaves the server (ADR 0042 amendment 2026-09-18, issue #120)
- **One refusal, two callers: `crawler/text_gate.py`.** `discover_sources` has refused a planned
  search query since ADR 0013's amendment; a review of PR #112 found that ADR 0042's adapter
  repair had no equivalent although it sends far more — up to `llm.MAX_INPUT_CHARS` (12,000)
  characters of an indexed page's own text, on the one parse shape (blind: no page, no title or no
  media) where the terms, the score and the media gate have judged nothing yet, because they run
  afterwards in `store_parse_result`.
- `text_gate.judge` is **pure**: text and compiled terms in, rule names out. It runs the text and
  its `ascii_fold` variant through `crawler.safety.check` and `crawler.safety_score.score_page`, and
  reports `block_term`, `review_term`, `age_pattern`, `minor_context:<cluster>`, `forbidden_word`
  (the German non-consent list, moved here from `crawler/discovery/services.py` — `FORBIDDEN_WORDS`,
  and `FORBIDDEN_QUERY_WORDS` is now an alias of it) and, for a caller that passes `min_score`,
  `low_score`. The matched *word* is a separate field: a search query is **our** text and
  `unsafe_query_reason` may print it, a page's text is content and the repair path may not.
- `text_gate.transmission_refusal(task, page_text)` is the database-aware gate: `Site.enabled`
  first, then an `index.Page` for this URL whose `safety_status` is not `clear`, then the text
  itself with terms from `load_safety_terms()` — **fresh, never `cached_safety_terms`** — the site's
  own `site_risk`, and `min_score=SAFETY_GATE_MIN_SCORE`, the media gate's own bar (ADR 0018): text
  we would not fetch a picture for is text we do not hand to a third party.
- **Enforcement is in `llm.extract_page_fields`, as its first statement** — the one function in the
  crawler that opens a socket to the provider — so the gate runs before the prompt is assembled,
  before the `LLMCall` row exists and before any client is constructed, whoever calls it. What is
  judged is exactly what would leave: the cleaned, truncated excerpt that goes inside the
  `<page_text>` fence, never the HTML it came from.
- **Fail closed.** Any exception from the gate is a refusal (`refused_check_failed`); there is no
  branch in which a failing check becomes permission to send.
- **What a refusal records**: one `crawler.LLMCall` row, `error` = `refused_safety` /
  `refused_page_flagged` / `refused_site_disabled` / `refused_check_failed` plus the rule names and
  the matched terms' categories, **never a word of the text**; `input_sha256` empty (no prompt was
  built), `output` null, no tokens, no cost. `crawler.repair.calls_today` excludes rows whose
  `error` starts with `text_gate.REFUSED`, so a refusal never consumes the per-site daily cap. The
  page continues through the ordinary path and is blocked by the ordinary safety pass as usual.
- **Residual risk, recorded rather than hidden**: the excerpt cannot exclude performer names,
  because the field the repair exists to recover is the title and on these sites the title is where
  a performer is named. The gate bounds *what kind* of text may be sent, not *whether a name* may be
  in it. The feature's switch stays an operator decision.
- Tests: `backend/crawler/tests/test_adapter_repair.py` (the transport raises if it is ever reached)
  and `backend/crawler/discovery/tests/test_markets.py` (the query wording is unchanged).

## Content safety and the media gate
- Terms and statuses: ADR 0011, `crawler/safety.py` (pure), applied in `crawler.services`. The crawl
  hot path (`store_parse_result`) reads terms and creators through per-process caches
  (`cached_safety_terms` / `cached_creators`, `REFERENCE_CACHE_TTL_S` = 120 s, cleared by saves and
  deletes in the same process): an edited term reaches every crawler within two minutes. Explicit
  rescoring (`rescore_page`, `scan_pages`) always loads the terms fresh.
- **Active safety notification (issue #40) and the daily operator report.** `crawler/alerts.py` can
  mail the operator a short, rate-limited summary (page id, site domain, category, matched term,
  admin link — never the page URL/title/text) when `crawler.services` stores a page as `blocked`
  **by a rule that opens a safety incident** (`safety_rules.opens_incident`,
  `alerts.mails_immediately`) — but since 2026-09-19 it only actually mails while `safety_incident`
  is in `OPERATOR_MAIL_IMMEDIATE` (`core/operator_mail.py`), empty by default. Every block — 440 of
  the 459 blocks measured on 2026-09-18 were never immediate at all, and the incident class joined
  them — is counted (and, for the incident class, listed by name) in `crawler/safety_digest.py`
  instead: one mail a day from the scheduler job `safety_digest` at `SAFETY_DIGEST_HOUR_UTC`,
  covering the window since its last successful run (safety section: blocks by site and by rule,
  incident-rule blocks by name, what waits for a human, the change since the window before; crawler
  section: adapter alarms, TDM/AI reservations), sent every day, quiet or not (2026-09-19 — its own
  absence is now the "something broke" signal), a failed send never advancing the window. Same
  content rule as the alert; the by-site/by-rule tallies name no page id, and the page query reads
  three columns. Off unless `SAFETY_ALERT_TO` and `SAFETY_ALERT_FROM` are both set; transport is
  Django 6.1's `MAILERS` setting, not the deprecated single `EMAIL_BACKEND`/... settings
  (docs/safety.md). Every network mail backend gets
  a timeout (`EMAIL_TIMEOUT`, default 10 s), so a hung SMTP server cannot stall a worker.
- **Media gate (ADR 0018).** `crawler/safety_score.py` is pure: `score_page(texts, verdict,
  site_risk=)` returns a `PageScore` (score, deciding reason, signals). It starts at 1.0 and only
  subtracts — term hits short-circuit to 0.0; near misses (an age in an uncovered unit,
  school/family/"young" wording, a bare 11-17 token), an uncovered language, thin text and a
  per-site risk factor from recent `block` incidents each cost a documented amount.
  `crawler.services.site_risk(site)` supplies that factor. Stored on `index.Page` as
  `safety_score`, `safety_score_reason`, `safety_score_signals`, `safety_scored_at`.
- Media is fetched only when the page is `clear` **and** scored >= `SAFETY_GATE_MIN_SCORE`
  (default 0.80). A page that was never scored is never allowed. `index.services.page_media_allowed`
  is the single rule; `media_denial_reason` names the skip reason.
- **`index.Media.eligible_at` is the permission** (NULL = never request this). Only
  `store_parse_result` grants it, when the detail page's own text was parsed and scored. Listing
  cards never grant it — that was the hole issue #67 closed.
- **`index/media_fetch.py` is the only place media bytes may be requested.** `fetch_media` re-reads
  status, score and eligibility from the database immediately before the request and raises
  `MediaFetchDenied`. Both the media worker and `marceline/review/media.py` go through it. Never
  call `client.get` on a media URL directly — the invariant is asserted in
  `matching/tests/test_media_gate.py` (zero requests, every kind, every denied state).
- **The media worker's client is `public_only`** like the review passthrough: the media URL and
  every redirect hop must be http(s) to public addresses only, and the connection goes to the
  checked address (DNS rebinding). A refused hop sends nothing: the row is skipped
  (`address not allowed: …`), or retried when the name does not resolve at all.
- **Media hosts' robots.txt (ADR 0007 amendment 2026-09-17).** `core/robots.py` is the one parser
  and cache (`crawler.robots` re-exports it). `matching/media_robots.py` `MediaRobots.check_for`
  is passed as `fetch_media(check=…)`: after the deny, before every attempt and redirect hop.
  Disallowed → `Media.Status.ROBOTS_BLOCKED` (final, never requested; existing hashes untouched);
  robots.txt 4xx → allowed; 5xx/429/network → `defer_media` for the whole host with a per-host
  backoff (`retry_delay`, 1 min → 6 h), attempt refunded. Its robots.txt requests are `kind=robots`
  for the media's site, counted but `enforce=False`. A media host's `Crawl-delay` raises its delay.
  The review image passthrough uses the same check (module-level `robots`, client per request).
- `crawler.services.rescore_page(page)` recomputes one page and re-applies the gate (used after a
  reviewer clears a page). `scan_pages` (CLI `safety_scan`) rescores everything and is the backfill.
- **Safety writes race live workers; a block always wins.** Never write a verdict or apply the
  media gate from a page read earlier without re-checking it: `scan_pages` writes conditionally on
  the stamps it read (else re-judges under a row lock) and applies the gate from locked current
  rows; `matching.media._page_gate` locks the page until the hashes are written. Safety mail is
  sent after commit (`transaction.on_commit`), never inside a transaction holding a page lock.
- **Safety incidents (issue #66).** `index.SafetyIncident` + `SafetyIncidentPage` are the reporting
  record: status, category, term, timestamps, channel, hotline reference, decider, note, cluster id
  and one URL row per page — **never page content**. `index/incidents.py` owns the record, the
  four deduplication cases (`same_url`, `mirror`/`sha256`/`cluster`, `pdq`, `term_change`) and the
  retention policy (open → closed → deleted twelve months after closure). `SafetyIncidentHash`
  holds the PDQ hashes `safety_scan` moves off a page it blocks by an incident rule (ADR 0011
  amendment 2026-09-17): **for deduplication only** — never in `index.FrameHash`, no matching
  module may read the table (a test walks `matching/`), nothing is ever fetched for it, and the
  rows cascade with the case's retention. It may not import
  `matching`, so the "same content" evidence is assembled in `marceline/safety_incidents.py`
  (`mirror_key`, media sha256, `ClusterMember`). CLI: `safety_incidents`,
  `backfill_safety_incidents`, `safety_retention` (all `--dry-run`-first, all idempotent).
  Workflow for staff: admin *Index → Safety incidents* (mark reported / closed / false positive).
- **Rule actions (operator decisions 2026-09-17, ADR 0018 amendment).** `SafetyTerm.action` is
  `review | auto_block | auto_block_and_incident | customer_release_queue`; meaning, strictness and
  helpers (`blocks`, `incident_due`, `opens_incident`, `release_queue_only`, `rule_version`) live in
  `catalog/safety_rules.py` (pure). Every non-review action blocks; unknown/legacy `block` = strictest.
  `crawler.safety.resolve` keeps a human decision only for (term, action no stricter than decided).
  Rule set versions are content digests (`Page.safety_rule_version`, snapshot `catalog.SafetyRuleSet`);
  `index.SafetyRuleDecision` is the append-only audit log (auto-blocks, queue steps, staff changes —
  `set_safety_status(by=)` logs). Consequences that need the project layer go through
  `index.signals.safety_rules_applied` (sent on commit by `store_parse_result`) →
  `marceline/safety_rules.py`: incidents for `auto_block_and_incident` pages, and the **customer
  release queue** (`Page.safety_release`: queued only when every match is a
  `customer_release_queue` term and a rights holder of an owned original is credited; release
  re-checks text + rules under a row lock and makes the page clear/reviewed; any later hit revokes).
  Staff API `marceline/review/safety_release.py` (`/review/safety/customer-release`, **text only: no
  media ids, no URLs**). `site_risk` counts only incident-worthy blocks. `safety_scan` lives in the
  project package now (it needs incidents and targets).
- **Hotline report draft (issue #66).** `index/incident_report.py`: `incident_facts` reads the
  record, `render(facts, "de"|"en")` is pure and follows a fixed template
  (`TEMPLATE_VERSION`). Staff API `marceline/review/incidents.py` under
  `/review/safety/incidents` (list, `/{id}/report-draft`, `/{id}/reported`). The draft carries
  the case's URLs, dates, **category (never the term)**, dedup evidence, a truthful media-fetch
  statement and `MARCELINE-SI-<id>` — never titles, tags, text or media. Open/reported cases
  only; settled → 409. Change wording in both languages at once and bump `TEMPLATE_VERSION`.
