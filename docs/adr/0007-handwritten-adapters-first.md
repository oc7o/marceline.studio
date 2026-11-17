# 0007 — Hand-written site adapters first, skip instead of bypass, own robots.txt parser

Status: accepted (2026-09-14). Amends the rollout order of ADR 0006.

## Context
Reconnaissance (`docs/sources/`) showed the first sites are server-rendered with stable markup,
JSON-LD/og tags and predictable URL patterns. For three sites, small parsers are cheaper,
deterministic and easier to test than per-page LLM calls. Some sites sit behind Cloudflare; one
(site15.example) returns 403 for every request. site01.example's robots.txt uses `*` wildcards, which
Python's `urllib.robotparser` does not support.

## Decision
- **Adapters first.** Each site gets a pure adapter in `crawler/sites/<site>.py` implementing
  `SiteAdapter` (`seed_tasks`, `parse`) with fixture tests. The LLM (ADR 0006) comes later for
  onboarding new sites and repairing adapters whose extraction breaks.
- **Skip, don't bypass.** Cloudflare challenges, "access denied" pages, login walls and CAPTCHAs
  mark the task `skipped` with the reason. No challenge solving, cookie forging, headless-browser
  evasion or proxy rotation to get around blocks.
- **Own robots.txt parser** (`crawler/robots.py`, since 2026-09-17 `core/robots.py`): `*` and `$` wildcards, longest-match wins,
  allow on ties, a `MarcelineBot` group replaces `*`, crawl-delay honoured as
  max(`Site.crawl_delay_s`, crawl-delay). robots.txt 4xx → allow all; 5xx/429/network error →
  task back to pending.
- **User-Agent** `MarcelineBot/0.1` without contact or personal data during development.

## Consequences
- Deterministic, cheap crawling with fixture tests; `LLMCall` stays unused for now.
- Every site costs an adapter; markup changes break parsers until fixed (future LLM repair path).
- Blocked sites stay blocked — coverage gaps are accepted rather than evasion.

## Amendment (2026-09-15): age self-declaration vs age verification

Status: accepted. Decision by the operator.

### Context
The source probe labelled every page with age-confirmation wording "age gate" and treated it like a
block. The operator asked why: a click-through only asks the visitor to declare their age,
and the operator can answer it truthfully. Real age *verification* is different:
third-party providers and ID, selfie, credit-card or login checks exist to prove age, and getting
around them would be bypassing an access control.

### Decision
- **Allowed with operator approval per site:** self-declaration age gates (click-throughs, the age cookie or flag the site itself sets when "yes" is clicked). The approval
  lives in `catalog.Site.age_confirmation`
  (`{"cookies": {"age_verified": "1"}, "approved_by": "operator", "approved_at": "…", "note": "…"}`,
  blank = no approval). The crawl worker sends exactly those cookies through `PoliteClient`, scoped
  to the site's domain and its subdomains. This is an honest answer by the operator, not
  identity faking. Session, login, token and Cloudflare cookies are rejected by validation. Only
  cookies are supported (query flags would change URLs and task dedupe).
- **Never allowed:** real age verification — known third-party providers (list in `crawler/blocking.py`), ID/selfie/
  credit-card checks and login-based checks — and CAPTCHAs, logins, paywalls. These pages are
  always skipped and logged, also for approved sites; provider flows are never automated. Sites
  behind verification stay disabled (site25.example).
- **Not a gate:** client-side overlays on pages whose server already sends the full HTML
  (site19.example, site22.example). Nothing is sent or needed.
- **Classification** (`crawler/blocking.classify_age_gate`, used by the worker and the probe):
  provider domain markers → `age_verification_provider: <name>`; age wording, markup or known age
  cookie names on a page with at most 20 links → `age_self_declaration` (or
  `age_verification_provider: …` when it has a password field or ID/selfie/card wording); more links
  → overlay, not a gate. The worker skips both gate kinds with the reason; a self-declaration gate
  that is still shown with the approved cookie is skipped too. The probe records the same labels in
  `SourceCandidate.blocked_reason`, so the operator sees which candidates are onboardable.

### Consequences
- Candidates behind click-through gates can be onboarded after the operator has looked at the
  gate and recorded an approval; approval stays a manual, per-site decision.
- Classification is heuristic: an unknown verification provider on a thin page reads as
  `age_self_declaration` until it is added to the list. The operator review before approving is the
  safety net, and a cookie never passes a real verification — such pages keep being skipped.
- Overlay pages with a site-wide notice keep being crawled as before.

## Amendment (2026-09-17): request accounting and budgets on real requests (issue #74)

Status: accepted. Decision by the operator.

### Context
`Site.daily_request_budget` counted crawl *tasks* (`CrawlTask.fetched_at` in the last 24 h). It
never saw robots.txt fetches, retries after 429/5xx, redirect hops (each one a request since the
hop-by-hop redirect fix) or anything the media worker fetched. Measured on 2026-09-16, the last
whole UTC day, read-only against the live database: every enabled site but site03 and site20.example
used its whole page budget (2,500, or 5,000 for site02/site04, 4,000 for site03), and the
media worker added, per site:

| site | media on the site's own hosts | media on other hosts / reviewed CDN subdomains |
|---|---|---|
| site01.example | 4,010 | — |
| site02.example | — | 19,445 (fastdelivery.site02.example) |
| site22.example | 8,530 (www.) | — |
| site19.example | — | 8,995 (imgcdn.site19.example) |
| site08.example | 5,010 | — |
| site17.example | 4,668 (www.) | — |
| site21.example | 4,432 | — |
| site04.example | — | 4,033 (a third-party CDN host) |
| site05.example | — | 2,765 (static-eu-cdn.site05.example) |
| site11.example | 2,386 (apex + n1.) | 50 (cdn.site11.example) |
| site07.example | 2,362 | — |
| site18.example | 2,274 (www.) | — |
| site20.example | 2,232 | — |
| site10.example | 174 | 2,403 on nine CDN hosts |
| site09.example | — | 1,609 (image-cdn.site09.example) |
| site03.example | — | 2,726 on three hosts of one third-party CDN |
| site06.example | — | 84 (a third-party CDN host) |

So about 44,500 page tasks and 36,000 origin media requests reached the sites' own servers that
day, plus about 42,000 media requests to CDN hosts — roughly 123,000 real requests against budgets
that saw 44,500. (Lower bounds: a re-fetched task or media row keeps only its last `fetched_at`,
and robots.txt, retries and redirect hops were never recorded.) The operator decided: *"the daily budget should count real requests — but then
increase the max requests accordingly. We should have metrics for request type and destination (we
can request CDNs more than their servers if we can separate them)."* And, clarifying: a site's own
server is expensive and gets strict budgets; CDN requests are nearly free and get their own, much
higher, per-host budgets; budgets are keyed by (site, host role, request kind), and media served
from the origin counts against the origin.

### Decision
- **Every outgoing request is counted at one choke point**, `core.http.PoliteClient`: a client
  built with an `observer` reports each attempt — every retry, every redirect hop, and refusals by
  a hook — with a `RequestLabel` saying what it is for (`kind`, `site_id`, optionally `role`,
  `enforce`). The observer is `catalog.request_accounting.RequestAccounting`; a test
  (`marceline/tests/test_request_accounting_coverage.py`) fails if a `PoliteClient` is built
  without one or new code opens its own httpx client.
- **Counters, not a request log.** `catalog.RequestCount` holds one row per (UTC hour, site, host,
  host role, kind, request type, outcome) with `requests` and `bytes`, upserted in batches from an
  in-memory buffer (at least every 30 s while sending, on idle and on close; rows sorted so
  concurrent flushes cannot deadlock). About 100k requests a day become a few hundred rows a day.
  The daily `request_accounting` scheduler job rolls hourly rows of days older than 30 days into
  `RequestCountDaily`, kept forever. Dimensions:
  - `role`: `origin` (the site's domain and subdomains), `cdn` (any other host serving the site's
    media), `third_party` (traffic of no site: RDAP registries, DataForSEO);
  - `kind`: what the request is for — `page`, `robots`, `media`, `probe`, `rdap`, `search_api`,
    `review_image`; `request_type`: the kind, or `retry` / `redirect_hop` for a repeated attempt or
    a followed redirect;
  - `outcome`: `2xx` … `5xx`, `error` (no response), `refused` (refused before sending — by the
    media deny, `public_only`, a redirect policy or a budget; not a request, never budget use).
- **Host roles per site.** Without a row the rule applies (own domain and subdomains = origin,
  anything else = CDN). `catalog.SiteHost` overrides it: the daily job records every host media
  URLs point to (`source=observed`, rule role), `seed_sources` marks reviewed CDN subdomains from
  `SitePolicy.cdn_hosts` (`source=policy`: subdomains that resolved to another network than the site
  on 2026-09-17 — fastdelivery.site02.example, imgcdn.site19.example, static-eu-cdn.site05.example,
  image-cdn.site09.example, cdn.site11.example), and the operator edits roles in the admin
  (`source=operator`, never overwritten).
- **Budgets on real requests**, rolling window of 24 hour buckets, refusals excluded, checked by
  the observer immediately before each request (after the per-host wait and every hook):
  - origin **page** budget per site, `Site.daily_request_budget`: kinds `page` and `robots` on
    origin hosts, their retries and redirect hops included. `claim_task` skips sites over it;
  - origin **media** budget per site, `Site.daily_media_request_budget`: kind `media` on origin
    hosts;
  - **CDN host** budget, one per host across every site using it: `SiteHost.daily_request_budget`
    (the lowest value set for that host) or `CDN_HOST_DAILY_REQUEST_BUDGET` (25,000). A CDN shared by
    several sites therefore has one shared limit, never one per site; related hostnames of one CDN
    (numbered hosts b1/b2/b3 of one CDN) are separate hosts with separate budgets.
  - Discovery probes, RDAP, DataForSEO, capture robots.txt and the review image passthrough are
    counted and never refused (they have their own caps: `MAX_PROBE_REQUESTS`, the discovery spend
    guard, capture visit budgets, the passthrough rate limit).
- **A refusal sends nothing and costs no attempt.** The crawler releases the task with its attempt
  refunded and `next_attempt_at` at the next hour boundary (when the oldest bucket leaves the
  window). The media worker does the same for the row and holds every other pending media row on
  that host until then — of the same site for an origin budget, of every site for a CDN host — so
  it does not claim them one by one. A refusal flushes the counters first, so database queue
  filters agree with it.
- **Numbers.** New policy values keep the measured origin load, in real requests: page budget =
  old page budget + 10 % for robots.txt, retries and redirect hops, rounded up to 500 (3,000;
  site02/site04 5,500; site03 4,500); media budget = origin media of 2026-09-16 + 10 %, rounded
  up to 500, at least 1,000 (site01 4,500, site22 9,500, site08 6,000, site17.example 5,500, site21.example
  5,000, site07/site18/site11 3,000, site20.example 2,500, the rest 1,000). Catalog migration
  0013 applies them to sites still on their old policy value, so a migration alone does not cut
  throughput; `seed_sources` re-applies them. `CDN_HOST_DAILY_REQUEST_BUDGET` is 25,000, above the
  largest measured host (19,445).
- **Visibility.** `manage.py request_report [--site DOMAIN] [--days N]` and the Site admin (list
  columns for page and media use, a per-site report on the change page, a Site hosts inline) show
  origin page and media budgets, CDN hosts, types and outcomes for 24 h and N days; read-only admin
  lists of the raw counters. The review overview's `requests_24h` is now origin page requests.

### What bypasses the choke point
- The capture worker's **headless browser** (ADR 0016): its page, player and segment requests go
  through Playwright; the per-capture request count and bytes stay in
  `BrowserCaptureCandidate.capture_run`. Only its robots.txt fetch is counted here.
- **DataForSEO** uses its own authenticated httpx client and is counted by hand
  (`record_request`, kind `search_api`).
- **DNS**: the system resolver and the Team Cymru TXT lookups of `catalog/hosting/dns.py` (UDP),
  and the `public_only` address checks — lookups, not HTTP requests to anyone's server.
- **Outgoing mail** (safety alerts, SMTP) and **Sentry** (when configured).
- Off the server: the operator's local `client/fingerprint.py` makes no requests to sites, and the
  `frontend/parity` harness only talks to a loopback server.

### Consequences
- Overshoot is bounded, not zero: a process reads the counters at most every 60 s (flushing its own
  buffer first) and adds its own requests since; what it cannot see is other processes' requests
  since that read that are not flushed yet. Per other process using a budget that is at most
  (60 s + 30 s) / per-host delay requests — about 30 at 3 s. A crashed process loses at most 30 s of
  counts. The counters and budget checks are database-backed, so they work unchanged when the
  per-site worker containers become one process (issue #75, ADR 0030 reads the same counters and
  names).
- A budget-exhausted site idles until an hour bucket leaves the window instead of until 24 h after
  its last fetch; the budget still caps what a rolling day can hold.
- Delays are unchanged: the per-host delay is still the rate a site's operator sees; budgets only
  say how long we keep going each day. Raise budgets before delays, as before.
- New checks that must hold immediately before a request (e.g. robots.txt for media) belong in the
  `before_request` hook; they run before the budget check, so a request they refuse is counted as
  `refused` and never uses budget.

## Amendment (2026-09-17): robots.txt for media hosts

Status: accepted. Decision by the operator.

### Context
robots.txt was obeyed for every page the crawler fetches, but the media worker requested posters,
hover frames and preview clips from whatever host served them — the site's own server or a CDN —
without asking that host's robots.txt. Measured on 2026-09-17 (read-only, one robots.txt request
per host): the 42,125 media rows of active pages point to 29 hosts. On 28 of them no stored media
URL is disallowed for us (some answer 404 or 403, which allows everything); two hosts of site03's CDN
ask for `Crawl-delay: 10` (site03 already crawls at 10 s). **One host disallows everything**:
`k.site10.example` (`User-agent: *` / `Disallow: /`), 81 media rows (0.19 %), all already hashed.

### Decision
- **The robots.txt policy lives in `core/robots.py`** (moved from `crawler/robots.py`, which
  re-exports it), so the media worker in `matching` can use it without importing `crawler`.
- **Every media request obeys its host's own robots.txt** for our User-Agent
  (`matching/media_robots.py`). The check runs through `index.media_fetch.fetch_media(check=…)`:
  once after the media deny and before the host wait, then in the client's `before_request` hook
  before every attempt and every redirect hop — after the deny, before `public_only` and the budget
  check, so a refused request is never sent and never uses budget. Rules are cached per origin for
  24 h in the worker process, as the crawler caches a site's.
- **Outcomes.** Disallowed (the URL or a redirect target): the row gets the final status
  `robots_blocked` (`index.Media.Status.ROBOTS_BLOCKED`, migration `index/0015`) and is never
  requested or retried. robots.txt 4xx (404 included): everything allowed (RFC 9309, as for sites).
  5xx, 429 or a network error: nothing is requested from that host; the row goes back with its
  attempt refunded and every pending media row on that host waits with it, backing off per host
  like a paused site (1 min doubling up to 6 h, reset by the next readable robots.txt).
- **Crawl-delay.** A media host's robots.txt `Crawl-delay` raises the delay between its media
  requests (on top of `media_delay_s`), once its robots.txt is cached.
- **Accounting (issue #74).** A media host's robots.txt request is labelled `kind=robots` for the
  media's site and counted like any other, but never refused by a budget (`enforce=False`, as the
  capture worker's): it is one request per host per worker a day, and refusing it would hold a
  site's origin media behind the origin page budget, which the crawler uses up daily.
- **Existing hashes stay.** Rows hashed before this rule keep their frame hashes: a finished row is
  never claimed again, so its URL is never requested again. Nothing is deleted.

### Consequences
- `k.site10.example` media found from now on is recorded (URL only) and never fetched; site10
  pages whose only preview is there stay text-only.
- A media host whose robots.txt is down stops its media, not the site's pages; the crawler's site
  pause is unchanged.
- The review image passthrough (`marceline/review/media.py`) applies the same check (rules cached
  per web process, robots.txt fetched and counted when missing): a disallowed image is a 403, an
  unreadable robots.txt a 502. The review overview counts `robots_blocked` media as skipped.
- A changed robots.txt reaches a running worker within 24 h. A row blocked by a rule that was later
  lifted stays `robots_blocked` until someone sets it back to `pending` in the admin.

## Amendment (2026-09-17): site19.example and site22.example age overlays confirmed not gated

Status: accepted. Decision by the operator.

The 2026-09-15 amendment above already classified both sites' client-side age overlays
as not a gate (the full page is served, nothing is clicked or submitted). The operator has now
explicitly confirmed this for both sites: `docs/sources/site19.example.md` and
`docs/sources/site22.example.md`. No behaviour changes — both sites keep crawling exactly as before.

## Note (2026-09-17): delays in the crawl runtime (ADR 0030, issue #75)

The per-host delay described above is kept in each worker process. In the crawl runtime
(`crawl_runtime`, `infra/compose.yaml` profile `runtime`) it is enforced by `core.HostSlot` rows
instead (`core/host_slots.py`), shared by every thread, lane and process: a site's own server gets
one slot for pages, robots.txt and media from that server (one request per effective delay in total,
where the per-site workers allowed up to 1.5), and each CDN host its own slot at `CDN_HOST_DELAY_S`
(1 s) or its robots.txt `Crawl-delay`, whichever is larger. Budgets, robots.txt, the allowlist and
the hooks' order are unchanged. The per-site workers keep the in-memory delay until the cut-over.
