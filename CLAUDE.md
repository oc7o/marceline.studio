# Marceline

> **Sunset 2026-09-22.** Nothing runs any more and nothing here is to be started, deployed or
> promoted; see `README.md`. The text below is the record of how it was built and operated.

Leak detection for content creators: crawl piracy sites, index what is found, match it against
creators' media with PDQ fingerprints. Built entirely by coding agents — keep things conventional.

Architecture: `docs/architecture.md`. Decisions: `docs/adr/`. What to build next: `docs/backlog.md`.
Source list (sites, creators): `sources.md`. Site reconnaissance notes: `docs/sources/`.
Every command and flag: `docs/commands.md`. Why each feature rule exists: `docs/feature-notes.md`.
Releasing to production: `docs/runbooks/release.md` (a promotion PR needs a green check, a security
review, a legal review and the operator's approval; the two review checklists are `docs/reviews/`).

## Prerequisites
docker + docker compose, `uv`, `make`, `ffmpeg`/`ffprobe` (video frame sampling), `g++` and
`python3-dev` (`pdqhash` has no Linux wheels and is compiled from source).

## Layout
```
backend/     Django + Django Ninja (Python 3.13, uv)
  marceline/   project: settings, urls, api (Ninja, /api/v1); review/ = staff moderation API
  core/        shared helpers (env config, polite HTTP client, robots.txt policy) — depends on nothing
  accounts/    users/auth
  billing/     plans, subscriptions, GoBD invoices + PDF/GiroCode (ADR 0032)
  catalog/     Site, Creator, sources.md seeding
  index/       Page, Media, FrameHash
  crawler/     CrawlTask queue, site adapters, worker, LLMCall log; adapter breakage
               detection and the capped LLM repair behind it (ADR 0042, off by default)
  matching/    PDQ hashing, frame sampling, Hamming index, duplicates, PairLabel, evaluation,
               frame embeddings as a second signal (ADR 0037, off by default), metadata scoring
               as a third, retroactive one (ADR 0040, off by default)
  delivery/    production read-through cache (ADR 0028): signed envelopes, export tokens, cached
               bundles, purges, the customer write outbox — depends on core only
infra/       compose.yaml (development VPS: postgres, web, crawler + media workers, scheduler);
             prod/ = public customer cache (Caddy, gunicorn, Postgres; ADR 0028); backup/
             prod/site/ = the standalone Caddy that serves marceline.studio today (own compose
             project `marceline-site`, no upstream). One Caddy serves both names from the cut-over
             on (operator 2026-09-19, `docs/runbooks/production.md` §14): `PUBLIC_SITE=on` in the
             customer stack and this project retired — until then it holds the host's public
             80/443 and `PUBLIC_SITE` stays off
docs/        architecture, ADRs, backlog, site reconnaissance
frontend/    internal moderation SPA: Vite + React + TS, TanStack, Tailwind (ADR 0004, 0014;
             conventions in frontend/CLAUDE.md,
             subsystem rules in the CLAUDE.md next to the code — also under backend/)
  customer/    the customer app (/app): Next.js 16 App Router (webpack, not Turbopack — see
               next.config.ts), server-rendered, its own npm project (ADR 0036); shares the words
               and the pure logic with the SPA via @shared/*
client/      local reference fingerprinting CLI for the operator's Mac (uv script, no Django;
             videos, and photo collections with `--photos`; hashes-only JSON for
             `import_fingerprints`; client/README.md)
website/     the public marketing site at marceline.studio: static HTML + CSS, no build step, no
             JavaScript, no image, no third-party request — `public/` is what a server serves,
             `check.py` is the whole build (`make website-check`). Impressum and
             Datenschutzerklärung are written and real (§ 5 DDG, Art. 13 DSGVO). **Live since
             2026-09-18**, served by the standalone `marceline-site` project (`infra/prod/site/`),
             not by the customer stack's `PUBLIC_SITE` block, which stays off. Creator-first,
             „du", short on purpose — website/README.md lists the constructions that must not come
             back and how an edit is shipped. "Dein Video bleibt bei dir" is a published claim:
             it must change **before** `CLOUD_FINGERPRINTING_ENABLED` ever does (issue #124)
```

## Commands (run from repo root)
Full reference, one row per target and per management command with every flag: `docs/commands.md`.

| Command | What |
|---|---|
| `make status` | **where the project actually stands, derived — run it at the start of a session and after every compaction.** Read-only. Branches, running containers with the flags they really hold, settings no container receives, scheduler jobs, budgets, issues, operator decisions. Main checkout only (`ARGS=--branches`, `--gaps`, `--no-github`) |
| `make sync` | install backend deps (`backend/.venv`) |
| `make up` / `make down` | start db + web (http://127.0.0.1:8000, admin at `/admin/`) / stop everything; `up` never starts crawling |
| `make workers` / `make workers-stop` | start / stop the per-site crawler + media workers and the scheduler; `WORKERS_SKIP="crawler-x media-x"` leaves out sites the crawl runtime serves |
| `make runtime` / `make runtime-stop` | start / stop the crawl runtime lanes for `RUNTIME_SITES` (ADR 0030, `docs/runbooks/crawl-runtime.md`) |
| `make db` | start only Postgres (127.0.0.1:5433) |
| `make migrate` / `make makemigrations` | apply / generate migrations |
| `make test` / `make fmt` | pytest against compose Postgres / ruff fix + format |
| `make openapi` → `make frontend-api` | regenerate `backend/openapi.json`, then `frontend/src/api/schema.d.ts`, after API changes |
| `make frontend-install` / `-dev` / `-build` | moderation SPA: `npm ci` / dev server on :5173 (`/api` proxied to :8000) / build |
| `make customer-install` / `-dev` / `-build` / `-check` / `-smoke` | the Next.js customer app (ADR 0036): dev server on :3000 |
| `make check` | everything: backend lint, format, mypy strict, import-linter, migration graph, tests, OpenAPI drift; both frontends; the website |
| `make check-posted` | `make check` on this server under the shared check lock (`/tmp/marceline-check.lock`), one at a time; stop the workers and the runtime first — the server is RAM-bound. `POST_STATUS=1` also posts the `check` commit status for a promotion PR. **The check-runner host was deleted on 2026-09-19** (downscale): `make remote-check` now refuses with a pointer here unless `CHECK_HOST=` names a rebuilt runner (`docs/runbooks/check-runner.md`) |
| `make prod-config` / `make prod-surface-check` / `make website-check` | production compose invariants / real Caddy against a stub / the marketing site |
| `make hooks` | install pre-commit hook |

Single test: `cd backend && uv run pytest matching/tests/test_models.py -k self_pair`.
Management commands: `cd backend && uv run python manage.py <command>` — table in `docs/commands.md`.
**Anything that deletes, deactivates, suppresses or moves ownership defaults to a dry run; run the
dry run first and read it.**

**Moderation UI** (ADR 0014): `make up` + `make frontend-dev`, then `http://dev.example.internal:5173`
(Tailscale; locally `127.0.0.1:5173`), staff user (`manage.py createsuperuser`). Images: privacy
mode by default; load mode only through `/api/v1/media/{id}/image`, never for pages in safety
review/blocked.

**Customer app** (ADR 0036): `make up` + `make customer-dev`, then `http://dev.example.internal:3000/app`,
customer from `manage.py seed_test_customer`. Server-rendered; the browser's writes go to
`/app/api/*`; the language comes from the request (`curl -H "Accept-Language: de-DE"` is a real test). The Vite
SPA still serves the same screens at `:5173/app` until the cut-over to Next has settled.

## Operating rules
The reasoning and the measurements behind each line: `docs/feature-notes.md` and the ADR named.
- **Restart workers after every migration** (`make workers`). A worker keeps the code it started
  with and fails every write after a model change (2026-09-15: 997 detail pages lost). The runtime
  lanes restart themselves within a minute.
- **`make up`/`workers`/`runtime` restart but only rebuild when `backend/pyproject.toml`, `uv.lock`
  or `Dockerfile` changed.** Force with `REBUILD=1` or `make workers-build` / `up-build` /
  `runtime-build` — required after a dependency change, never after a code change or migration.
- **One crawler and one media worker per site, enforced by a per-site advisory lock**
  (`core/locks.py`); a second one exits. A site is served by the legacy per-site workers *or* the
  crawl runtime (ADR 0030), never both: start legacy workers with `WORKERS_SKIP` for the runtime's
  sites or they restart-loop on the taken lock.
- **Request budgets count real requests** (issue #74). Every `PoliteClient` gets
  `observer=RequestAccounting()` and every request a `RequestLabel`; a test enforces it. Raise
  budgets in `catalog/sources.py` `SitePolicy`, never by bypassing the client.

- **Onboarding by invite** (issue #115, ADR 0052, `ONBOARDING_INVITES_ENABLED`, off): staff issue a
  single-use link, the creator sets a password, signs the versioned declarations (issue #123), accepts
  the documents and answers the EU-only country question. **Still no signup** (ADR 0023): the
  token is staff-issued, stored only as a hash, printed once, and nothing mails it.
  `/api/v1/app/invite/…` is the one customer surface reachable **without a session**, so every
  failure answers **one identical 404** — never add a second sentence, status or faster path
  there. `ONBOARDING_DOCUMENTS` is empty by default and empty **refuses the accept step**: never
  fill it with a draft. **It runs on the public host since ADR 0052 §9 slice 2** (2026-09-19):
  production still creates **nothing** — the last step hands the redemption to the outbox and
  answers 202, development applies it with the same `complete()`, and only the password's
  **hash** ever travels; `handed_over_at` is what makes single use hold there. An invite that is **never redeemed**
  (expired, revoked, abandoned — `redeemed_at IS NULL`, never "`organisation IS NULL`": a join
  invite names its organisation from the start) is deleted `ONBOARDING_INVITE_RETENTION_DAYS` (30)
  after it stopped being usable, by the scheduler's `invite_retention` job
  (`INVITE_RETENTION_ENABLED`, off; dry run by default); a handed-over redemption and a redeemed
  invite are never touched by it. The signed accounts
  document carries the open invites' token digests only while the flag is on, so the deploy order
  is main → the promotion → the flag (`docs/runbooks/production.md` §15).

- **Password reset and address verification** (issue #149, ADR 0054, `ACCOUNT_EMAIL_FLOWS_ENABLED`,
  off): **development mails, the public host never does** and is refused a transport by `make
  prod-config`. A request there becomes an outbox item, development issues the token (stored as a
  digest only), sends the mail and applies the write; only the new password's **hash** travels. A
  request answers **one 202 for every address** and a dead link **one 404** — never add a second
  sentence, status or faster path. A **GET confirms nothing** (scanners fetch links) and **nobody
  is signed in by a reset**. An unverified address **blocks nothing**: the app asks in a banner.
  Deploy order is main → the promotion → the flag (`docs/runbooks/production.md` §16); the flag
  waits for a mailbox with SPF/DKIM/DMARC and a processor named in the privacy notice.

- **Identity by proof of control, never a document** (ADR 0055, `ACCOUNT_CONTROL_VERIFICATION_ENABLED`,
  off): we show the creator a one-time code, they place it in the bio of the platform account whose
  content we protect (or send it from there), and a staff member opens that **public** profile **by
  hand** and records what they saw. **Identity documents are never
  stored** — the fallback for doubt, for studios, and the only route for "own website" / "another
  platform" is a video call that records only *that*
  the check happened. It clears `identity_not_verified` through `RightsClaimant.identity_status`
  and adds **no second notion of "verified"**. **Nothing fetches the claimed profile URL** — a test
  fails on an HTTP import in that flow. Reads reach production from the signed bundle, the two
  writes through the outbox, so the switch is set on **both** machines.

## Signals and breakers — all off by default
- **Frame embeddings** (ADR 0037, `MATCHING_EMBEDDINGS_ENABLED`): a **candidate generator, never
  evidence** — band `review`, never `high`, never a notice; three guards enforce it (database
  constraint, `confidence.band_for`, `calibration.apply_bands`). No face detection, no biometrics,
  no age estimation. Not retroactive. The model file is fetched per machine, never committed.
- **Reference-side crop grid** (ADR 0039, `MATCHING_REFERENCE_CROP_GRID`): a grid hit **is**
  ordinary PDQ evidence. One packed blob per moment; **never add a radius query over a blob**;
  adoption requires re-running `calibrate_confidence`. The strict threshold is 36
  (`matching/references.py`, ADR 0015 amendment), set independently of the grid.
- **Metadata scoring** (ADR 0040, `MATCHING_METADATA_CANDIDATES_ENABLED`): **an aiming signal,
  never a finding.** It writes nothing and asks nobody; an admitted pair is only a browser-capture
  candidate and PDQ decides. A reason to look, never a permission: every ADR 0016 exclusion and the
  media gate still apply. **Duration only ever admits, never rejects — clips are leaks.**
- **Media URL breaker** (ADR 0041, `MEDIA_URL_BREAKER_ENABLED`): only a settled 404/410 counts —
  never a 5xx, 429, 403 or a tokenized link. Suppression **postpones** rows, never fails them.
- **Cloud fingerprinting** (issue #59, ADR 0044, `CLOUD_FINGERPRINTING_ENABLED`): the customer
  uploads a video, the server hashes it and deletes the bytes. **Uploaded media never touches
  Postgres, a log, a backup or a Sentry event, and is never stored on the public production
  host** — without `CLOUD_UPLOAD_RELAY_MODE` the routes are development-only
  (`ROLE_ONLY_PREFIXES`), like the rights proof vault; with it (operator 2026-09-19, ADR 0044 §9
  amendment) the public host only **relays** each 64 KiB read to development and writes nothing:
  no spool, no row, no outbox item, no temporary file. **Never touch `request.body` on that
  path**, never add `request_buffers` to the Caddyfile, and the relay's credential is an
  `upload_relay`-scoped service token, never the export token. Development **verifies** that
  attestation rather than believing it (ADR 0044 §9 amendment 2026-09-20,
  `marceline/uploads/attestation.py`): an active account, a membership for exactly that
  organisation and the same gate a browser gets, **one identical 404 for every failure**, at most
  25 organisations per rolling day, every refusal counted in the append-only
  `marceline.RelayRefusal`, and the relay only ever touches rows **it created**
  (`MediaUpload.origin`), reads included. A signed upload grant would be theatre — the browser
  only talks to production, so `web` could ask for any customer's grant. The bytes live only in the spool volume, are deleted in a `finally` on
  every exit and by a TTL reaper after a crash, and `bytes_deleted_at` is written only from a
  verified deletion. The decoder is a child process with **no database credentials**,
  `RLIMIT_FSIZE = 0` and an ffprobe allowlist, in a container with **no route to the internet**;
  **never remove `-protocol_whitelist file`** from an ffmpeg or ffprobe argument list, and never
  put a customer-supplied string into one. The customer's **file name is never transmitted**. An
  upload needs the per-file **sole-creator declaration** with explicit consent to processing
  sensitive personal data, stored verbatim (`marceline/app/upload_declaration.py`). The switch must not be
  moved for a real customer before ADR 0044's amendment § C is met: the published website claim
  changes **first** (issue #124), and the declaration and issue #127's DSA artefacts are final.
- **Customer data deletion** (ADR 0045, `CUSTOMER_DATA_DELETION_ENABLED`): two scopes — an
  organisation whose pilot ended, and one original an organisation loses — each on a date **a human
  set**, never inferred; deleted `CUSTOMER_DATA_RETENTION_DAYS` (30) later. **Never delete pages,
  hashes or the index** (ours, not the customer's), a file another organisation owns, billing, a
  `legal_hold` row (the database refuses it) or a safety incident; what is kept is reported, never
  silently skipped. An owner switch resets the moved file's case file at once and marks the
  previous owner's rest: the new owner never inherits a case history.
- **Adapter health and LLM repair** (ADR 0042): alarm only on blind parses (no page, no title, no
  media), never on duration or credits. The repair (`CRAWLER_LLM_REPAIR_ENABLED`) costs money per
  call, one call per page, no retry, capped per site per day. **Never add a URL-shaped field to the
  repair schema**: an extracted URL is a fetch instruction written by the page.

## Adding a site adapter
1. Record HTML fixtures in `backend/crawler/tests/fixtures/<domain>/` (HTML only, no media).
2. Read the draft: `manage.py draft_adapter --domain <domain>` (ADR 0043) — engine fingerprint,
   link families with a `video_path` regex, field candidates, search form, removal marker. Offline,
   no model. A KVS theme needs only a `KvsSite` entry, which the draft writes out.
3. Implement the `SiteAdapter` protocol from `crawler/sites/base.py` in `crawler/sites/<site>.py`
   (`domain`, `seed_tasks()`, `search_tasks()`, `listing_tasks()`, `parse()`); reuse helpers from
   `crawler/sites/common.py`. Seeds are per creator; a start URL about no creator (newest uploads,
   whole catalogue) belongs in `listing_tasks()`, which only `crawl_seed --listings` enqueues.
4. Register an instance in `ADAPTERS` in `crawler/sites/registry.py`.
5. Test `parse()` against the fixtures (model: `crawler/tests/test_site01.py`) — hand-written, with
   the real values from the fixture; the draft deliberately prints none.

## Definition of done
`make check` is green. New behaviour has tests. Model changes ship with migrations.
API changes ship with a regenerated `openapi.json`. Decisions that change the architecture get an ADR.

## Module boundaries (enforced by import-linter in `backend/pyproject.toml`)
- `core` → nothing
- `accounts` → core
- `billing` → accounts, core
- `catalog` → core
- `index` → catalog, core
- `crawler` → catalog, index, core
- `matching` → index, catalog, core
- `delivery` → core (it moves signed bytes; what goes into a bundle is decided in `marceline/`)
- `marceline` (project) may import anything; apps never import `marceline`.

If a change needs a new dependency direction, stop and write an ADR instead of loosening a contract.

## Hard rules
- **Never store pirated media.** Store URLs, sha256, sizes and PDQ hashes only. Images are
  discarded after hashing. No media in fixtures, the repo, or the database.
- **No live network in tests.** Crawler tests use recorded HTML fixtures in
  `backend/crawler/tests/fixtures/<domain>/` (HTML only).
- **Crawler policy is enforced in code, not in prompts:** domain allowlist (`catalog.Site`),
  robots.txt — the site's for pages, each media host's own for media (`robots_blocked`, never
  fetched) — per-host delay, daily request budget, no login / CAPTCHA / paywall bypass and no age
  *verification* bypass; self-declaration age gates only with per-site operator approval
  (`Site.age_confirmation`, which the capture browser honours too since 2026-09-18); no anti-bot
  evasion. Blocked or challenge pages are skipped, never worked around (ADR 0007 and its
  2026-09-15 amendment). **The headless browser's only interaction is one click on the page's own
  play control**, on a site with `Site.capture_play_click` (default off), decided by the pure rule
  in `marceline/capture/click.py` and only after the block/gate detector has cleared the page —
  never a consent, age, login, subscribe, download, close-ad or skip-ad control, never a second
  click, no keyboard input (operator decision, ADR 0016 amendment 2026-09-18).
- **A policy switch is changed through the admin or a command that records a reason, never a bare
  `.update()`** (issue #106). Every change to `Site.enabled`, `capture_enabled`,
  `capture_play_click`, `age_confirmation`, `crawl_delay_s` or a request budget writes an
  append-only `catalog.SitePolicyChange` row from a **database trigger**, so an unattributed write
  is recorded as actor `database` rather than lost — but it is then a change nobody can explain.
  Use the admin (it demands a reason for the weighty switches) or
  `catalog.policy_audit.policy_actor` / `apply_policy_update`. **Never replace the trigger with a
  model signal**: the flip this exists for was a queryset `.update()`, which fires none. A
  machine-readable TDM/AI reservation in a site's robots.txt is recorded the same way
  (`catalog.SiteRobotsPolicy`, issue #105): one addressed to us pauses the site for review (while
  `ROBOTS_RESERVATION_PAUSE_ENABLED`), one addressed to other companies' AI crawlers is recorded
  and reported and **never acted on**.
- **Crawler User-Agent carries no contact data or personal information** — no email addresses,
  names or accounts. Default `MarcelineBot/0.1` (`CRAWLER_USER_AGENT`). A dedicated project mailbox
  is a production decision, never a personal address.
- **LLM in the crawler:** text/HTML only, never images. Page content is untrusted input
  (prompt injection). Output must validate against a Pydantic schema before use. Every call is
  logged as `crawler.LLMCall`. Page text is judged by the safety terms, the minor near misses and
  the media gate's score **before** it may be sent (ADR 0042 amendment, `crawler/text_gate.py`, the
  first statement of `llm.extract_page_fields`); a refusal is recorded by rule name, never with the
  text, and costs no budget. Moving that call later is a release blocker.
- Secrets only via environment variables (`.env`, see `.env.example`); never commit `.env`.
- **Staff surfaces are never public** (ADR 0028): the admin, the review API and the moderation SPA
  run on the development VPS and are reached over Tailscale only. The public production host
  serves `/app`, `/api/v1/app/*` and `/api/v1/health` — Caddy and `DJANGO_PUBLIC_SURFACE=customer`
  both refuse everything else. Since ADR 0036 `/app` is the Next.js customer app (`basePath=/app`,
  so its chunks are under `/app/*` too), chosen by `CUSTOMER_APP=next|spa` in Caddy; the writes it
  relays live at `/app/api/*` and reach Django's same customer API. A new customer endpoint under `/api/v1/app/` is public by
  construction; anything elsewhere is private until both allowlists name it. **No CORS headers,
  ever** — the SPA is same-origin; CSRF guards every write. Production's only credential for the
  development side is a scoped, read-only export token over Tailscale (ADR 0028) plus
  development's **public** key — no database role, no Storage Box, no GitHub key, never the
  signing key. Customer writes on production only enter an outbox that development pulls and
  applies (ADR 0028 write path); production signs those batches with its **own** key, held by the
  Tailscale-only `internal` process, never by the public `web`. Notice drafts are served there too
  since ADR 0046 — reads from the bundle, every write through the outbox — **except the sandbox
  send**, whose path puts the verb before the id (`/api/v1/app/notices/sandbox-send/{id}`) so the
  prefix allowlist keeps the one path that may hand a draft to a mail transport off the public host. Caddy caps customer request
  bodies at 1 MB except exactly `POST /api/v1/app/fingerprints` (16 MiB, Django's cap for that
  route too). On production (`MARCELINE_ROLE=prod`) customer data comes only from signed bundles;
  the export API is 404 there and the purge and outbox endpoints are 404 on development.
  Runbook: `docs/runbooks/production.md`.
- **A suppressed name is never indexed again** (ADR 0038, docs/suppression.md). A person who is
  not our customer may tell us to stop: `catalog.CreatorSuppression` is the durable marker,
  `Creator.objects.usable()` the single rule every hunting, linking, discovery and reporting path
  reads, and a database trigger on the page-creator link table drops a link any other writer still
  tries to store — a re-crawl of a page that credits the name stores no link and no breadcrumb.
  Apply and lift it with `suppress_creator` (dry run first), never by hand. **Never send a
  requester a list of URLs, site names or counts**: short names produce false
  positives and the answer could itself be the harm. A **safety-flagged page stays off-limits even
  here**: links and breadcrumbs go, but its stored text is never rewritten, and no suppression or
  lift links, activates, re-crawls or releases it.
- **No outbound contact without `may_contact`** (issue #98, ADR 0047). A
  "no" is recorded as a keyed hash of each contact identifier — never the identifier — in
  `catalog.DoNotContactRecord`, and every outbound approach starts by asking `manage.py
  may_contact`, which answers by position and never echoes an input. The record is permanent: no
  delete in the admin, untouched by `suppress_creator`, out of reach of customer-data deletion.
  `DO_NOT_CONTACT_HMAC_KEY` is **never rotated** — the hashes are one-way, so a new key silently
  orphans every objection. This is **not** index suppression (ADR 0038): neither implies the other.
- **Findings name identifiers, never content.** A report, commit message, issue or chat answer
  refers to a page id, a site's role, a rule name or a sha256 — never a pirated title, a piracy
  URL, a cast name read off a pirate page, the detail of one safety-flagged page, or a secret's
  value. Long conversations are summarised and the summary is carried forward, so anything said
  once gets repeated; the sensitive fact belongs in the database or in a pinned constant (e.g.
  `IGNORED_FILES` in `import_fingerprints`) and the text points at it.
- **Prove it before reporting it.** Never state that something is merged, deployed, enabled,
  running or fixed without running the command that shows it — `git log base..branch`,
  `docker inspect`, a settings read, a query. Intending to do it is not evidence, and neither is a
  subagent's summary: a subagent's report is a claim. Spot-check its load-bearing number, above all
  when it names a helper ("customer-visible", "what the gate allows") — read which function the
  real path calls. Two traps that have already cost a day each: **a container keeps the environment
  it was created with**, so `.env` and the running process can disagree; and **a setting absent from
  `infra/compose.yaml` is silently empty forever**, however carefully `.env` sets it. `make status`
  answers all of this in one command — run it at the start of a session and after a compaction.
  "I don't know yet, checking" is always an acceptable answer; a confident wrong one is not.
- **Commit and push without asking** (operator, 2026-09-18). Finished work is committed in small,
  focused commits and pushed: a feature branch as soon as it has commits — work that exists only on
  this server is one disk away from gone — and `main` as soon as the check on the exact commit is
  green ("Integration on main"). Never a force-push, never a push to `production`: a release is a
  promotion PR (`docs/runbooks/release.md`). `git push` sends the branch, not your commits — if
  someone else's commits sit on it, name them in the report.

## Reporting, and where the work happens
- **Reports are short.** The operator reads them in a terminal, between other things. Lead with the
  answer, then the evidence for it; a number and the command that produced it beat a paragraph
  describing them. Say what changed, what is blocked and what needs a decision — not what was
  considered and dropped, not a narration of the steps taken to get there, not a summary of a
  summary. Brevity never buys an exception to "Prove it before reporting it" or to "Findings name
  identifiers, never content".
- **Do the work in subagents; keep the main session for the operator.** A build, a broad search, a
  merge, a research pass: hand it to an agent with an explicit brief and file ownership (below) and
  report the result. The main session stays short, responsive and interruptible — which is the
  point, because the operator can then ask a question or change direction without waiting for a
  build to finish, and the tool output of the work never crowds out the conversation. Opus for
  architecture, research and deep work; Sonnet for chores and simple builds. Its report is a claim
  ("Prove it before reporting it").

## Working in parallel (several agents)
- **Never run docker compose (or `make up/db/workers/migrate/test`) from a git worktree.** All
  checkouts share one compose project, and a worktree has no `.env`, so compose recreates the main
  tree's containers with different settings and kills whatever is running against the database
  (2026-09-16: a fingerprint import lost its connection mid-run). The Makefile now refuses; in a
  worktree run `POSTGRES_DB=marceline_<task> make COMPOSE=true check` and apply migrations from the
  main checkout after merging. `COMPOSE` is a `:=` variable, so it must be a make argument —
  `COMPOSE=true make check` does not work.
- Every agent gets explicit file ownership; don't edit files outside it.
- **Give every agent its own scratchpad subdirectory.** The session scratchpad is shared: on
  2026-09-16 six parallel recon agents overwrote each other's `scratchpad/raw/home.html` and
  helper scripts, and one nearly reported another site's age-gate markup as its own finding.
  Recon findings must be re-verified against the fixture's own `<title>` before they are believed.
- **One worktree, one agent, and never `git add -A` in a tree you did not create.** On 2026-09-18
  three agents were handed the same worktree directory (a finished agent's, reused by name): one
  ran `git add -A` and swept two others' half-finished edits into its own commits, and another
  found its files replaced under it mid-edit. Nothing was lost — the contaminated branch was
  rebuilt by staging explicit paths and the foreign work was preserved on a recovery branch — but
  it cost three rebuilds and one invalidated before/after test comparison. A worktree directory is
  never recycled while any agent may still be using it, and a commit stages the paths its brief
  names, one by one. If a tree changes under you, do not tidy it: save your own diff, move to a
  fresh worktree, and say so in your report.
- Only one agent at a time changes `backend/pyproject.toml` / `backend/uv.lock`.
- Generate migrations immediately after changing a model, so other agents' tests don't hit a
  schema that doesn't exist yet.
- Shared registries (e.g. `crawler/sites/registry.py`): re-read right before editing.
- **Integration on main.** The containers mount `backend/` from the main checkout: the workers run
  merged code on their next restart, and **`web` reloads it at once**. So a merge that carries a
  migration is followed by `make migrate` **immediately**, before the check — on 2026-09-18 the
  staff UI answered 500 for half an hour because the column waited for a green check (additive
  migrations only; one that drops or rewrites goes check first, with `web` stopped). Sequence:
  merge → `make migrate` → `make workers-stop runtime-stop && make check-posted` (this server; the
  check-runner host is gone since 2026-09-19, so never two checks at once — read **make's own
  exit code**, not a wrapper's: a red main was pushed that way on 2026-09-19) → push → recreate
  what runs (`make up`, the scheduler, `make runtime`; `make workers` only if legacy per-site
  workers were running — beside the runtime lanes they restart-loop on the taken locks). A
  container keeps the environment it was created with: a compose change needs a recreate, not a
  restart. One writer on `main` at a time, and only the main session merges a branch that touches
  the root `CLAUDE.md` — the permission system refuses that to a subagent.
