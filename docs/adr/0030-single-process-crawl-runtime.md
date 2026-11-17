# 0030 — One crawl runtime with database-backed host slots

Status: accepted (2026-09-17, operator decisions at the end). Issue #75. Replaces the "one crawler and one media worker per site"
rule (CLAUDE.md, `infra/compose.yaml`) and the in-process per-host delay of ADR 0007. Builds on the
request accounting of issue #74, which lands first. Plan: `docs/plans/single-process-crawler.md`.

Operator decision, 2026-09-17: *"yes, definitely — replace the 34 per-site worker containers; it
saves a lot of resources"*.

## Context

`make workers` runs `crawl_worker --site D` and `media_worker --site D` for each of the 17 enabled
sites: 34 worker containers plus the scheduler. Each process is a full Django process with its own
`PoliteClient`, whose per-host delay lives in memory. That is why "one worker per site" is a rule,
and since 2026-09-16 an enforced one (session advisory locks per site, `core/locks.py`).

Measured on the live host, 2026-09-17 around 10:30 UTC (read-only: `docker stats`, `pg_stat_activity`,
task and media timestamps):

| | Today |
|---|---|
| Worker containers | 34 (17 `crawler-*`, 17 `media-*`), each one Python process |
| Worker memory (`docker stats`) | crawlers 1,344 MiB, media 1,315 MiB: **2,659 MiB** (69–101 MiB each); `ps` RSS in a crawler container is 126 MiB, part of it shared or swapped |
| Host memory | 7.9 GiB; 5.0 GiB used, 1.24 GiB in swap; Postgres 452 MiB; scheduler 133 MiB; web 118 MiB |
| Postgres client connections | 35 idle (one per worker, plus the scheduler) of `max_connections` 100 |
| CPU | every worker 0.00–0.89 % at the time of measurement |
| Page tasks fetched, last 24 h | 44,365. 15 of 17 sites used their whole daily budget |
| Minutes with at least one page fetch, per site | 63–332 of 1,440 (4–23 %) |
| Media fetched, last 24 h | 71,283. Minutes with a fetch per site: 4–841 (0.3–58 %) |
| Claimable media backlog | 37,558 rows (site02 22,808, site08 6,784, site22 6,025) |
| Distinct media hosts, last 30 days | 32, **none shared between two sites** today |
| PDQ hash of a 1080p JPEG poster (dev box) | 75 ms; a hash-only Python process (numpy, PIL, pdqhash) is 45 MiB RSS |
| Django process after `setup()` plus workers, adapters and 8 idle `PoliteClient`s | 118 MiB RSS |

So the fleet is 34 copies of a ~100 MiB interpreter that each spend most of the day asleep: the
budgets are spent in bursts (14,417 page tasks in the 14:00 UTC hour alone), and politeness, not
CPU, bounds throughput. Three further problems come from the topology, not from the code:

- **Shared hosts are not safe.** The delay is per process, so two sites whose media sits on one
  CDN host (site10 already uses a shared third-party CDN, which any other mirror of the same
  network would share) would double that host's request rate. CLAUDE.md can only
  warn about it.
- **A site's own host is asked twice.** The crawler and the media worker of a site both hit
  `site08.example`; `SITE_HOST_DELAY_FACTOR` papers over it with a doubled media delay (≤ 1.5 requests
  per delay window, not 1).
- **Every new site is a compose change** (two services), and every migration needs 34 container
  restarts — the stale-worker failure of 2026-09-15 (997 lost detail pages) was a missed restart.

## Decision

### 1. Process model: threads in two lane processes, hashing in child processes

One command, `manage.py crawl_runtime --lane pages|media`, in a new project-package module
`marceline/runtime/` (it drives `crawler.worker` and `matching.media_worker`, a combination only the
project package may import — like `marceline.scheduler` and `marceline.capture`). No import-linter
contract changes.

| OS process | Container | What runs in it | Expected RSS |
|---|---|---|---:|
| `crawl_runtime --lane pages` | `runtime` | dispatcher thread + `RUNTIME_PAGE_THREADS` (6) task threads: robots.txt, fetch, adapter parse, store | ~150 MiB |
| `crawl_runtime --lane media` | `media-pool` | dispatcher + `RUNTIME_MEDIA_THREADS` (8) fetch threads; stores hashes | ~150 MiB (bodies ≤ 5 MiB, previews ≤ 20 MiB, in flight) |
| hash children ×`RUNTIME_HASH_PROCESSES` (2) | `media-pool` | PDQ / ffmpeg only, no Django, no DB, no network | ~50–70 MiB each |
| `scheduler` | `scheduler` | unchanged (ADR 0012) | 133 MiB |
| `ffmpeg` | `media-pool` | transient, per preview, as today | — |

**Workers: 4 long-running processes, ~0.4 GiB instead of 2.6 GiB (≈ 2.2 GiB saved); with the
scheduler ~0.55 GiB.** Postgres connections drop from 35 to about 17 (one per thread, one per
dispatcher, the scheduler), which also shrinks the database container.

**Threads, not asyncio.** Everything the workers call is synchronous Django: `claim_task`,
`store_parse_result`, the safety gate's row locks, `fetch_media`'s database re-read inside
`PoliteClient`'s `before_request` hook. Under asyncio each of those becomes `sync_to_async`, which
by default funnels every ORM call through one thread, and a CPU-bound parse or hash would stall
every host's event loop. The concurrency we need is small and fixed by politeness: 17 site hosts at
3 s is ≤ 6 page requests per second, the media hosts at most ~10, so fewer than 20 requests are ever
in flight. At that size a thread pool costs a few MiB, keeps `crawler/worker.py`,
`matching/media_worker.py` and their tests nearly unchanged, and gives each thread its own Django
connection (Django connections are thread-local). httpx's async client buys nothing here.

**Hashing in processes.** PDQ decoding holds the GIL for ~75 ms per 1080p image and a decode bomb
or a native crash in PIL/pdqhash must not take the fetchers with it. `marceline/runtime/hash_pool.py`
is a tiny pool of `spawn`ed children that import only `matching.pdq` / `matching.frames` (neither
imports Django): the parent sends `(kind, bytes)` over an anonymous pipe and receives
`HashedMedia`. A job over its deadline (`FFMPEG_TIMEOUT_S` + 30 s) kills that child and respawns
it; the media row is failed as today (`hash error`), never left RUNNING. Children are recycled after
500 jobs or 300 MiB RSS. The pool size is set by RAM, not cores: at peak (10,717 media in one hour,
~3/s × 75 ms) the whole load is a quarter of a core, so two children are ample on the 8 GB box.
Bytes stay in memory as today; the pipe is not a file.

**Parsing stays in the task thread.** Adapters import Django models for their types, are pure and
fixture-tested, and parse in milliseconds. A parser exception already fails only its task
(`_process_safely`). The one failure a thread cannot survive — a regex that backtracks for minutes
while holding the GIL — is caught by the process watchdog (§ 5), which restarts the process.

**Decided (operator, 2026-09-17): two containers**, `runtime` (`--lane pages`) and `media-pool`
(`--lane media`). They fail differently (media: 20 MiB bodies, ffmpeg, native decoders; pages: mail
alerts, the safety scorer), and the database slots below make the split free for politeness. `--lane all` runs both in one process for development and
for a box where even 150 MiB matters.

### 2. Database-backed host slots

`core.HostSlot`, one row per host key:

| Field | Meaning |
|---|---|
| `key` (pk) | lower-cased host, a leading `www.` stripped: `www.site22.example` and `site22.example` share one slot (stricter than today's exact-host key) |
| `role` | `origin` or `cdn`, copied from #74's host classification (`catalog.HostRole`, `SiteHost`, default rule: the site's domain and its subdomains are origin); decides the default delay, below |
| `next_allowed_at` | earliest start of the next request |
| `leased_until`, `lease_owner` | one request in flight per host; the lease expires by itself if its holder dies |
| `min_delay_s` | floor for every request to this host: the largest delay any caller has passed for it (only ever raised automatically; the admin may lower it — the per-host override for a CDN) |
| `last_lane`, `waiting_lane`, `waiting_until` | lane alternation, below |
| `penalty_until`, `penalty_reason` | host-wide backoff after 429 / `Retry-After` / connection failures |

`PoliteClient` gets a `gate: HostGate` argument (a protocol in `core/http.py`, which stays free of
Django). `InMemoryHostGate` is today's `_last_request_at` dict and stays the default for CLI tools,
discovery probes and tests; `core/host_slots.py` implements `DbHostGate`. Every attempt — each
retry, each redirect hop, each robots.txt fetch — goes through `gate.acquire(key, delay, lane)`,
exactly where `_wait_turn` is today:

```sql
-- inside one short transaction (a savepoint when nested)
UPDATE core_hostslot
   SET next_allowed_at = clock_timestamp() + make_interval(secs => greatest(%(delay)s, min_delay_s)),
       min_delay_s     = greatest(min_delay_s, %(delay)s),
       leased_until    = clock_timestamp() + make_interval(secs => %(lease)s),
       lease_owner     = %(owner)s,
       last_lane       = %(lane)s,
       waiting_lane    = CASE WHEN waiting_lane = %(lane)s THEN NULL ELSE waiting_lane END
 WHERE key = %(key)s
   AND next_allowed_at <= clock_timestamp()
   AND (leased_until IS NULL OR leased_until < clock_timestamp())
   AND (penalty_until IS NULL OR penalty_until <= clock_timestamp())
   -- yield to the other lane when it is waiting and this lane had the last turn
   AND NOT (last_lane = %(lane)s AND waiting_lane IS DISTINCT FROM %(lane)s
            AND waiting_lane IS NOT NULL AND waiting_until > clock_timestamp())
RETURNING next_allowed_at
```

- **`clock_timestamp()`, never `now()`**: `now()` is the transaction's start and would let a
  long transaction hand out a slot early. All processes use the database clock, so container clock
  skew cannot shorten a delay.
- No row returned: read the slot, sleep until the earliest moment it could be free (≤ 1 s steps),
  try again. A thread waits at most `RUNTIME_SLOT_MAX_WAIT_S` (60 s); then the gate raises
  `HostBusy` and the task is released with its attempt refunded (the path paused sites use today).
  A thread never sleeps minutes behind a penalised host.
- **The safety hook runs inside the reservation.** `before_request(url)` — the media deny — runs
  after the `UPDATE` and before `COMMIT`. A refusal rolls the reservation back, so ADR 0018's
  "immediately before every attempt" and `core/http.py`'s "a refused attempt does not take the
  host's slot" both hold literally. The request is sent right after the commit; the lease is
  released (`leased_until = NULL WHERE lease_owner = me`) when the body is read or the attempt
  fails.
- **Delays follow the destination's role** (operator, 2026-09-17: a site's own server is expensive,
  a CDN is "basically free"). Start to start, never below the slot's `min_delay_s`:
  - **origin hosts**: `max(Site.crawl_delay_s, robots Crawl-delay)` as today, for every request type
    — page, robots.txt, retry, redirect hop *and* media the site serves from its own server;
  - **CDN hosts**: their own slot with `CDN_HOST_DELAY_S` (proposed 1 s, a setting; a slower CDN
    gets a higher `min_delay_s` in the admin), no longer the site's delay as `media_delay_s` uses
    today. Two sites sharing a CDN host are limited **once**, at the stricter delay, with one
    request in flight.
- **Lanes share a slot fairly.** A lane that does not get a slot marks itself as waiting
  (`waiting_lane`, `waiting_until` = two delays ahead, refreshed while it waits). The last condition
  makes the lane that had the previous turn yield to a waiting other lane. Both busy: they
  alternate. Only one lane using a host (every CDN host today): it gets every turn.
  `SITE_HOST_DELAY_FACTOR` is removed: page and media requests to a site's own host together stay
  at one request per delay (today up to 1.5).
- **Backoff is host-wide.** 429 and 5xx with `Retry-After`, and connection failures, set
  `penalty_until` for the host (capped at `RETRY_MAX_DELAY`), so every lane and process backs off,
  not just the thread that saw it. `PoliteClient` retries in-thread only when the backoff is ≤ 10 s;
  longer ones become the task's `next_attempt_at` (`fail_task` / `fail_media` as today).
- Slot rows are upserted (`ON CONFLICT DO NOTHING`) when tasks and media are enqueued; the gate
  upserts too, so a redirect hop to an unseen host works. The table is tiny and hot:
  `fillfactor = 50` keeps its ~20 updates/s HOT.

**Slots are politeness, budgets are cost — two separate things.** The operator's model
(2026-09-17): budgets are kept apart by request type and destination role; requests to a site's own
server cost the site and get strict budgets and delays, CDN requests get their own, much higher
budgets and their own host slots. **Interpretation recorded here:** the objection is to *sharing
budgets*, not to per-server politeness. A site's origin host therefore keeps **one slot for all
request types**, so its server never sees more than one request per delay whether the request is a
page, a robots.txt or a poster served from that server — while each request is still *counted and
budgeted* by its own type and role. If the operator ever wants origin media on a separate cadence
from origin pages, that is a change to this paragraph, not to the model.

**Budgets come from issue #74 (request accounting), not from this ADR.** As merged (re-checked
2026-09-17 against `catalog/request_accounting.py`; this paragraph first described one origin
budget per site, which is not what #74 built):

- `PoliteClient(observer=RequestAccounting)` counts every attempt with a `RequestLabel` (site,
  kind: `page`, `robots`, `media`, `probe`, `rdap`, `search_api`, `review_image`, plus the request
  types `retry` and `redirect_hop` the client derives) into `catalog.RequestCount` — one row per
  (hour, site, host, `HostRole`, kind, request type, outcome) — rolled up into `RequestCountDaily`.
- **Budgets are keyed by (site or host, role, kind)** over a rolling 24 h window, refusals
  excluded: origin **page** budget (kinds `page` + `robots` to the site's own hosts) =
  `Site.daily_request_budget`; origin **media** budget (kind `media` from the site's own hosts) =
  `Site.daily_media_request_budget`; **CDN** budget per CDN host across all sites = the lowest
  `SiteHost.daily_request_budget`, else `CDN_HOST_DAILY_REQUEST_BUDGET` (25,000). Other kinds are
  counted, never refused. `observer.admit` raises `BudgetExhaustedError` (`scope` `origin:page`,
  `origin:media` or `cdn`).
- A further per-kind split is a new budget field on #74's tables, not a new counter.

This ADR only fixes *where* the check runs: `observer.admit` is called inside the `gate.acquire`
transaction, after the slot `UPDATE` and the safety hook, so "slot granted", "not refused by
safety" and "within budget" are one decision, and a refused budget takes no slot (#74's client
already runs the budget check last). The claim queries keep the cheap pre-filters: the pages lane
skips sites over their origin budget; the media lane skips media on an over-budget site's origin
hosts and on CDN hosts over their own budget — so a site that spent its origin budget keeps getting
its CDN-hosted media hashed. With two lane processes instead of 34 workers, #74's documented
overshoot bound (unflushed counts of *other* processes) shrinks accordingly.

### 3. Claiming: host first, priority within a host

Both queues get a denormalised host key (`CrawlTask.host_key`, `Media.host_key`: set on insert,
backfilled by migration, indexed with `status` and priority). The claim becomes:

1. Up to `k` (= idle threads) **free slots**, oldest `next_allowed_at` first.
2. For each, the best claimable row *on that host*, with the existing filters (enabled, not paused,
   not over budget, due, attempts left; for media: `clear`, `eligible_at`, active page, not
   `capture`) and the existing orders (`-priority, created_at` for tasks; expiring, creator-linked,
   poster-before-hover for media), `FOR UPDATE SKIP LOCKED`.

Consequences for fairness:

- **Across hosts it is round robin.** A host that was just used moves to the back, so a site with
  a 22,808-row backlog gets one turn per delay like a site with 4 rows, and cannot starve anyone.
- **ADR 0029 priorities decide within a host**, which is the only place they compete for anything:
  two hosts never share a slot. Rights-holder seeds (30) still go before co-performer seeds
  (12–28), watchlist seeds (11) and details (≤ 20) on the same site.
- Priorities across hosts would matter only when threads are saturated. The dispatcher records
  **slot lag** (claim time − `next_allowed_at`); sustained lag above one delay means "add threads",
  and it is reported (§ 6). The measured peak is ~4 page tasks/s (14,417 in one hour) and ~3 media
  fetches/s (10,717), so 6 page and 8 media threads keep up while a task takes up to ~1.5 s
  (request plus parse and store); thread counts are settings, not code.
- A slow site holds one lease and one thread for at most the request deadline (§ 5); the other
  threads keep serving other hosts.
- **Per-site pause** (`Site.paused_until`, `consecutive_failures`) is unchanged and filtered in the
  claim; a paused site's hosts simply yield no row. A CDN host failing for media sets
  `penalty_until` on that host instead of pausing a site.

### 4. Safety guarantees (unchanged, and asserted again)

- `index/media_fetch.py` stays the only way to request media bytes; the deny runs inside the slot
  reservation (§ 2), after any wait, before every attempt. `store_hashes` still re-checks the gate
  under the page lock after hashing, so a page blocked while its bytes were in a hash child gets no
  hashes. Block wins everywhere it wins today.
- Adapters, `store_parse_result`, robots.txt (per-origin cache, now shared by the threads of a
  process behind a per-origin lock so concurrent threads fetch it once), the allowlist and
  hop-by-hop redirect checks, age-confirmation cookies (one `PoliteClient` per thread, so a site's
  cookies never leak into another thread's request) and the block detector are reused as they are.
- **Browser capture stays a separate, on-demand process** (ADR 0016), not in the runtime and not in
  `make workers`. It switches its private `_wait_turn` to `DbHostGate`, so a capture visit and the
  runtime can no longer double the rate on a site.
- The existing invariant tests (`matching/tests/test_media_gate.py`,
  `marceline/tests/test_capture_worker.py`) run against the runtime path, plus new ones: a page
  blocked while a thread waits for a database slot causes zero requests; a refused attempt leaves
  `next_allowed_at` untouched.

### 5. Failure isolation

- **Per task:** `_process_safely` and the media worker's catch-all stay; a task thread closes its
  database connection after any `InterfaceError`/`OperationalError` (`close_old_connections()`
  before every task — Django only does this per HTTP request) and carries on.
- **Per request:** a total deadline, not only httpx's per-read timeout (review L12): `_read` checks
  elapsed time on every chunk (`RUNTIME_REQUEST_DEADLINE_S`, 60 s pages / 120 s previews), so a
  server dripping one byte every 19 s cannot hold a thread. The slot lease equals that deadline.
- **Per thread:** each thread publishes a heartbeat (task id, started at). The dispatcher's watchdog
  treats a task running longer than deadline + 60 s as hung: it logs the stack
  (`sys._current_frames()`), stops claiming, and asks for a graceful restart.
- **Per process:** the dispatcher re-arms `faulthandler.dump_traceback_later(120, exit=True)` on
  every loop. That timer lives in C and needs no GIL, so a regex holding the GIL or a deadlock
  still dumps every thread's stack and exits; compose (`restart: unless-stopped`) starts a fresh
  process.
- **Crash recovery is immediate, not after 15 minutes:** tasks and media carry `locked_by` (the
  lane's name). A lane is single-instance (advisory lock in a new `RUNTIME_LOCKS` space, key =
  lane), so at start it returns its own RUNNING rows at once; `recover_stale_tasks` /
  `recover_stale_media` stay for everything else. The lock connection is checked every minute
  (`pg_locks` for the dispatcher's backend pid); a lost lock stops the process, closing the known
  gap of connection-bound advisory locks.
- **Leaks and code changes → graceful self-restart**, when any of: `RUNTIME_MAX_TASKS` (20,000)
  processed, RSS above `RUNTIME_MAX_RSS_MB` (400), `RUNTIME_MAX_UPTIME_H` (24) reached, or **the
  applied migrations changed** (the dispatcher compares `django_migrations`' newest id every
  minute). The last one removes CLAUDE.md's manual "restart workers after every migration" step:
  code is bind-mounted, so the restarted process runs the new models.
- **Graceful shutdown** (SIGTERM, or a restart trigger): stop claiming, let in-flight tasks finish
  within `stop_grace_period` (90 s, above the request deadline), release anything still RUNNING
  with its attempt refunded, release leases and locks, exit 0.
- **Maintenance** runs once per lane, not once per site: stale-row recovery and
  `recover_expired_media` every 10 minutes (review PERF), in the dispatcher.

### 6. Observability

- `core.RuntimeInstance` (one row per lane): pid, started at, heartbeat at, code revision,
  migration id, threads busy/idle, tasks done/failed since start, restart reason of the previous
  instance. Read-only admin.
- `HostSlot` in the admin (read-only except `min_delay_s` and clearing a penalty): next allowed,
  lease, penalty, last lane.
- `manage.py runtime_status [--site D]`: per site — pending/due/running per lane, fetched 24 h vs
  budget (from #74's counters), paused until; per host — slot lag p50/p95 over the last hour, wait
  time, penalty; per lane — heartbeat age, threads busy, hash pool queue. Plain text, no page
  content, like the other reports.
- Every 5 minutes each lane logs one INFO summary line (tasks/min per lane, busiest hosts, max slot
  lag, restarts); `docker compose logs runtime` replaces reading 34 logs.

### 7. Operations and migration

Compose shrinks to `runtime`, `media-pool` and `scheduler` under profile `workers`. **A new site
needs no compose change:** the lanes serve every enabled site with an adapter.

**Decided (operator, 2026-09-17):** side by side, then switch, with a rollback that is only a
compose command:

- During the transition the runtime takes the **existing per-site advisory locks**
  (`CRAWL_WORKER_LOCKS` / `MEDIA_WORKER_LOCKS`) for each site it serves, re-trying every minute. A
  site whose old container still runs is skipped and logged; one whose container stopped is picked
  up within a minute. Old and new can therefore never serve the same site, whatever the operator
  starts in which order.
- The old per-site services move to a `legacy-workers` profile for one week. `DbHostGate` is wired
  into the old commands first (plan step 3), so both generations respect the same slots; hosts are
  disjoint between sites today in any case.
- **Soak: 24 h on site20.example and site06.example** (low volume) — their legacy containers stopped,
  the runtime started with `--sites site20.example,site06.example`, every other site still on its old
  containers. Compare requests per host per hour (#74's `RequestCount`), errors and slot lag with
  the day before; then stop all legacy services and run the runtime unrestricted.
- The legacy services are **kept for one week** after the switch for rollback, then deleted.
- **Rollback:** `docker compose --profile workers stop runtime media-pool` and
  `docker compose --profile legacy-workers up -d`. The schema additions (slots, host keys,
  `locked_by`, instance rows) are additive and ignored by the old workers.
- `crawl_worker` / `media_worker` stay as manual `--once`/`--limit` debugging commands on the
  database gate; the "one worker per site" section of CLAUDE.md is rewritten to "one runtime per
  lane" once the legacy profile is deleted.

### 8. Stored media URLs: CDN re-requests without the page

Every media URL is already stored (`index.Media.url`, never the bytes), and the review image
passthrough already requests stored URLs without crawling the page first. The operator confirmed
(2026-09-17) that the runtime may do the same. The rules:

- **Allowed and cheap for stable CDN URLs.** Re-requesting a stored URL needs no page round trip
  when the host's role is `cdn`: one request against the CDN host's own slot and budget, none
  against the site's origin budget. For media served from the site's own server it saves nothing
  (it costs the origin budget like the page would) and is not done in bulk.
- **Never for tokenised or expiring URLs.** Shapes matching `matching.media.EXPIRING_URL_REGEX`
  (site03's `/vid|img/<token>/<10-digit expiry>/`) — and signed query strings, once an adapter
  produces them — are only valid shortly after the crawl. They need a fresh page crawl, as
  `crawler.services.recover_expired_media` does today.
- **The safety gate is unchanged.** A stored URL is not a permission: every re-request goes through
  `index/media_fetch.fetch_media`, which re-reads the page's status, score and `eligible_at`
  immediately before the request (§ 4), and automated paths skip inactive pages (ADR 0029). A
  blocked page's URLs are never requested again, however cheap.
- **A CDN asset is not evidence about the page.** Assets outlive pages (CDN caches, orphaned files
  after a takedown) and predecease them (rotated posters, CDN purges while the video stays up).
  Availability and removal tracking — `Page.availability`, `crawl_recheck`, the customer case file's
  "removed" — keep coming from the page alone. An asset answering 404/410 marks only its media row
  (`MEDIA_GONE`, as today), never the page removed; an asset answering 200 never marks a removed
  page playable.

**Where it pays off** (recommendation, in order):

1. **Re-hashing** after a hashing pipeline change or a decoder fix (ADR 0022 pipeline tags; rows
   failed with `hash error` or `MediaDecodeError`): the media lane re-fetches CDN media directly,
   one request per media and no origin budget, instead of re-crawling thousands of pages. This is
   the large one; a `media_rehash` command belongs to a later issue.
2. **Retries of media that failed transiently** (5xx, timeouts) — already page-free today; the
   runtime keeps it that way and now charges the CDN budget.
3. **Hoster follow-up** (ADR 0027): whether a CDN still serves an asset after a notice *to that
   CDN* is a fact about the CDN's compliance, recorded as such and separately from page removal.
4. **Browser capture ordering** (ADR 0016) at most as a hint: a poster that is gone makes a visit
   less promising, but the visit still loads and judges the page itself.

Not worth it: availability rechecks (not evidence, see above) and expiring URLs (need the page).

### Not chosen

- **Thread per site in one process** (the issue's interim): keeps in-process delays and per-site
  locks, so it still cannot handle a shared CDN host, still needs code per site in the process
  topology and wastes 34 idle threads' DB connections. It is roughly half of this work anyway.
- **asyncio** (§ 1).
- **One container** for both lanes: saves one ~100 MiB interpreter, but ties a native decoder
  crash or a 20 MiB preview spike to the page crawl. Available as `--lane all`.
- **Redis / Celery** for slots and queues: a new service for a few updates per second that Postgres
  already handles (ADR 0002).

## Consequences

- ~2.2 GiB of RAM back on the 8 GB box (the swap in use today is the symptom), ~18 fewer idle
  Postgres connections, 3 worker-side containers instead of 35.
- Politeness becomes a property of the database, not of the process topology: shared CDN hosts,
  apex/www pairs, page+media on one host and browser capture are all limited once.
- Combined page+media rate on a site's own server drops from ≤ 1.5 to 1 request per delay; the
  origin budget (#74) binds long before that (2,500–5,000 requests/day vs 28,800 slot turns at 3 s).
- CDN-hosted media speeds up: its own slot at ~1 s instead of the site's 3 s, and its own budget
  instead of competing with the site's pages. The largest backlog (site02, 22,808 media, served
  from `fastdelivery.site02.example`, a CDN host in #74's site policy) is then bounded by that
  host's CDN budget (25,000/day by default) instead of a 3 s delay; 15,025 were fetched in the last
  24 h measured.
- One database round trip per request more (~20/s at peak) on a small HOT table.
- A process-level fault (GIL-holding regex, native crash in a parser) now pauses all sites of that
  lane for the restart time (seconds) instead of one site; the watchdog and immediate `locked_by`
  recovery bound that.
- The runtime is new code on the hottest path; the rollout keeps the old workers one command away
  for a week.

## Effort

About **7 developer days in 10 small mergeable steps**, plus a 24 h soak and one week with the
legacy profile before deleting it (`docs/plans/single-process-crawler.md`). The issue's 3–5 days
covered slots, claims and the loop; the hash pool, watchdog, self-restart and status report are the
difference. Steps 1–3 (slots in the existing workers) are useful on their own: they make shared
CDN hosts safe before the runtime exists.

## Operator decisions (2026-09-17)

1. **Two containers**: `crawl_runtime --lane pages` (`runtime`) and `--lane media` (`media-pool`).
2. **Rollout**: 24 h soak on site20.example and site06.example next to the old containers, then switch;
   old containers kept one week for rollback; the runtime restarts itself after migrations.
3. **Budgets by request type and destination role**: origin requests cost the site (strict budget
   and delay), CDN requests have their own much higher budgets and their own host slots (§ 2, names
   from #74). *Interpretation, not a quote:* a site's origin host keeps one slot for every request
   type — per-server politeness — while budgets stay separate.
4. **Stored media URLs may be re-requested from a CDN without the page**, within the limits of § 8.

Further operator decisions for the implementation (2026-09-17, issue #75): two lanes (pages and
media); a site's origin server gets **one** request slot shared by every request type (pages,
robots.txt, media from the origin) while budgets stay separate per (site, role, kind); CDN hosts get
their own slot with a **1 s minimum delay** per host (or the host's robots `Crawl-delay`, whichever
is larger); slots live in the database (`core.HostSlot`); the soak runs site20.example and
site06.example with a `RUNTIME_SITES` allowlist while every other site keeps its old containers.

## Implementation (2026-09-17, issue #75)

What was built, and where it differs from the design above. Runbook:
`docs/runbooks/crawl-runtime.md`.

**Built as designed:** `core.HostSlot` + `core.host_slots.DbHostGate` behind a `HostGate` protocol in
`core/http.py` (`InMemoryHostGate` stays the default); `clock_timestamp()` everywhere; one lease
per host that expires by itself; host-wide penalties for 429/5xx/network backoffs; lane alternation
(`last_lane`, `waiting_lane`, `waiting_until`); `HostBusyError` after the maximum wait (the task or
media row goes back with its attempt refunded, media via `defer_media`); a whole-response deadline
in `PoliteClient` (`RequestDeadlineError`, not retried); `crawl_runtime --lane pages|media` in
`marceline/runtime/` with task threads, `spawn`ed hash children (`HashPool`, Django-free
`matching.media_hash`, deadline `FFMPEG_TIMEOUT_S + 30` s, recycling after 500 jobs / 300 MiB), a
faulthandler watchdog, graceful stop with refunds, and self-restart after applied migrations,
20,000 tasks, 24 h, a hung task or an RSS limit; single instance per lane (`RUNTIME_LOCKS`, checked
against `pg_locks`); the legacy per-site locks taken for every served site and retried every
minute; a 5-minute summary line. Origin media waits the site's delay (no `SITE_HOST_DELAY_FACTOR`
doubling), CDN media `CDN_HOST_DELAY_S` (`slot_media_delay_s`, roles from #74's `HostRoles`).

**Differences:**

- **Names and profile.** Containers `crawler-pages` and `crawler-media` under a new compose profile
  `runtime` (`make runtime` / `make runtime-stop`); the per-site services stay in profile
  `workers` for now (`make workers WORKERS_SKIP=…` leaves the runtime's sites out) instead of
  moving to `legacy-workers`. `--lane all` is not built.
- **`RUNTIME_SITES` is required** (domains or `all`; empty refuses to start), so a missing value
  can never take over every site. Compose defaults it to the soak pair.
- **Fairness by site, not by host.** No `host_key` / `locked_by` columns on the queues (plan steps 2
  and 4 deferred): each lane claims with the existing `claim_task(site_domain=)` /
  `claim_media(site_domain=)` for a site handed out by a round-robin `SiteRotation`, one task per
  site per lane at a time. Consequence: a site's media lane sends one request at a time even when
  its media sits on several hosts, like the legacy media worker; ADR 0029 priorities decide within
  a site as before. Crash recovery is the existing stale-row recovery (15 min) plus the release of
  unfinished rows at a graceful stop.
- **Two-phase slot turns instead of checks inside the reservation transaction.** `acquire` takes
  the lease in one short statement; the checks (safety deny, robots, `public_only`, budget) run
  while the lease is held; `start` stamps the start, `cancel` only drops the lease. No row lock is
  held while checks run (they may themselves read and write), and a refused attempt still leaves
  `next_allowed_at` untouched and lets no other request in meanwhile.
- **`min_delay_s` is an operator floor only**, never raised automatically (a lowered site delay or
  a removed robots `Crawl-delay` would otherwise stick for ever). "The stricter delay wins"
  holds per request: the next start waits for the previous start plus the larger of both callers'
  delays. The slot has no `role` column; callers pass the delay their role implies.
- **Maximum slot wait 150 s** (`RUNTIME_SLOT_MAX_WAIT_S`), not 60 s: `PoliteClient` keeps retrying
  in-thread for backoffs up to its 120 s cap, now as host-wide penalties, and such a retry must not
  turn into `HostBusyError`. A penalty that ends after the maximum wait raises at once.
- **Nested turns.** A thread asking for a host it already holds (a media host's robots.txt fetched
  inside that host's check) gets a turn that neither waits nor touches the row; `fetch_media` runs
  the robots check in `on_redirect` as well, so this only happens when the 24 h robots cache
  expires in between.
- **Legacy workers keep the in-memory gate** and the ×2 origin media delay: they are unchanged, so
  a rollback is exactly the old behaviour. Old and new never serve one site together (per-site
  locks), and no host is shared across sites today; the capture worker keeps its private delay.
- **Not built yet:** `core.RuntimeInstance` and `runtime_status` (the summary line, `HostSlot` in the
  admin and `request_report` cover the soak), slot rows upserted at enqueue time (the gate inserts
  them), `media_rehash` (§ 8, later issue).
