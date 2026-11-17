# 0014 — Internal moderation UI

Status: accepted (2026-09-15)

## Context

The operator labels M2 ground truth and reviews cluster and safety proposals in the Django admin.
One decision takes several clicks and a change form per row (120 leak suggestions meant 120 forms).
Review needs to be keyboard-driven, one item at a time, with bulk actions and undo. The customer
dashboard planned in ADR 0004 will need the same stack, auth and API conventions, so the review UI
is its first user rather than a throwaway.

## Decision

- **SPA on ADR 0004.** `frontend/`: Vite + React + TypeScript strict, Tailwind, TanStack Router and
  Query, types generated from `backend/openapi.json` (`openapi-typescript` + `openapi-fetch`),
  vitest. `make check` runs typecheck, lint, tests and a drift check of the generated types.
  shadcn/ui and Playwright are deferred until the customer dashboard.
- **Staff session auth.** Ninja endpoints `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`,
  `GET /api/v1/auth/me` use Django sessions. Login and logout check CSRF explicitly; every review
  endpoint uses `StaffSessionAuth` (CSRF-checked, 401 anonymous, 403 non-staff). Same origin: Vite
  proxies `/api` in development, Caddy later in production. No tokens in browser storage.
- **Review API in the project package** (`backend/marceline/review/`), reading app models and
  reusing app services (`matching.services.move_member`/`merge_clusters`,
  `index.services.set_safety_status`). Apps do not import it; no import contract changes.
- **Reference queue unit = (reference, page) pair**, joining `ReferenceLeak` and `ReferenceMatch`.
  Pair status follows the M2 report: confirmed in either table is ground truth, rejected in either
  is a false match, both is `conflict`. A decision on either row applies to both rows of the pair.
  Confirm/reject never overwrite an opposite human decision (409, reset first); `updated_at` is set.
  Keyset cursors (reference, score desc, page) stay stable while items are decided. Items show
  the stored ranking score, the leak `source` (suggested/manual/match/cluster) and, for
  propagated proposals, the `related_kind` (cluster/mirror) and seed page; cluster members show
  their `evidence` (pdq/sha256/external_link/mirror/manual).
- **Cluster context (2026-09-15).** Reviewers could not tell which original a cluster was about,
  nor why members they did not remember confirming were confirmed. Each cluster now carries
  `linked_originals`: references with a `ReferenceMatch` or `ReferenceLeak` on an active member,
  using the pair status above; rejected pairs and rejected members do not link. Pages are listed
  best evidence first (confirmed, conflict, proposed; strong, weak, leak only). Each member lists
  its own pairs (`originals`, rejected ones included). The queue (`GET /review/clusters`) defaults
  to `show=linked`; `show=all` orders linked clusters first; `reference_id` keeps clusters linked
  to that original. The cursor is `<linked 1|0>:<cluster id>`. Linked sets are computed in Python
  from both pair tables per request, like the reference queue.
- **Provenance is derived, not recorded.** Nothing is auto-confirmed: `build_clusters` only
  proposes, but it imports SAME PairLabels as `confirmed` with evidence `manual`. `ClusterMember`
  has no decider field, so the API returns `provenance`: `system` for proposed rows,
  `pair_label` for confirmed + `manual` + a SAME PairLabel with another page of the cluster, and
  `reviewer` for every other confirmed/rejected row (admin or this UI). The UI labels these
  "Proposed by system — PDQ", "Confirmed — from your pair labels", "Confirmed — by you (…)" and
  says in the legend that the origin is derived. A decider column would need a model change and
  is not part of this decision. Reset (`decision: reset`) puts members back to proposed; for
  `pair_label` members the response `notes` warn that the next `build_clusters` confirms them
  again until the pair label changes.
- **Availability and targeted rechecks.** Page rows show `availability` and
  `availability_checked_at` ("never checked" when null); removed pages are dimmed and can be hidden
  (`hide_removed` on reference items). `POST /review/pages/recheck` (ids) and
  `POST /review/clusters/{id}/recheck` (active members) call `crawler.services.recheck_page_ids`,
  which only enqueues DETAIL tasks at `RECHECK_PRIORITY`: finished tasks are refreshed, pending
  ones keep their retry schedule and are raised to that priority, running ones are reported as in
  progress, pages of disabled sites or with off-site URLs are skipped (allowlist). The site's
  crawler fetches them later under robots.txt, delay and budget; the browser never contacts them.
- **Confidence in the reference queue (2026-09-15, ADR 0015).** Items carry the match's `band`,
  `confidence`, timeline (`timeline_score`, `consistent_frames`, `ordered_frames` parsed from
  `timeline_reason`), `loose_frames`, `common_frames` / `common_seconds` (mm:ss), `decided_by` and
  `confidence_signals` as readable rows (label, input, weight, stored contribution; unknown numeric
  components are passed through). The response `meta` has `band_thresholds` from
  `ConfidenceSettings` (defaults without a row) and `hidden_low`. Order follows `review_queue()`:
  proposed first, band high → review → low, pairs without a match last, confidence desc, then the
  ranking score, reference and page. The cursor encodes that whole key, so deciding pairs does not
  shift the proposed queue; with `status=all` a decided pair moves behind the proposed ones and
  may appear again on a later page (the SPA keeps the first occurrence). Low-band matches are
  hidden unless `include_low` or `band=low`; `band` (repeatable) keeps only matches in those bands;
  `decided_by=auto` means the match row was decided automatically, `human` any other decided pair
  (leaks have no author). The API still does not write `decided_by`.
- **Image passthrough** `GET /api/v1/media/{id}/image`: staff only; only existing poster/frame
  `index.Media` rows (no caller URLs); never for pages in safety review or blocked (403); upstream
  host must resolve to public addresses (checked again after redirects); fetched through
  `core.http.PoliteClient` with the project User-Agent, 10 s timeout, no retries, 5 MB cap; only
  `image/*` (no SVG) is returned, with `Cache-Control: no-store`, `nosniff` and a sandbox CSP.
  Bytes stay in memory for one response and are never written to disk or the database. Upstream
  401/403/404/410 → 404 ("image unavailable"). Per-user rate limit 20 requests/second, in process
  memory. Preview clips and sprites are not served.
- **Privacy mode is the default.** The SPA requests no images until the reviewer switches to load
  mode (remembered per browser). The browser only ever loads `/api/v1/media/{id}/image`; the build
  ships a CSP with `img-src 'self'`, so hotlinks to piracy sites or CDNs cannot happen by mistake.
- **Safety exclusion.** The safety queue API has no media fields and the screen is text only, page
  URLs shown as text, not links (docs/safety.md). Other queues send no media ids for flagged pages
  and the image endpoint refuses them in every mode.

## Consequences

- Reviewers can see preview images for the first time. That is a deliberate, per-browser opt-in;
  the passthrough adds requests to piracy CDNs from the web host, bounded by the rate limit.
- The rate limit and the SSRF DNS check are per process and best effort (no shared store, DNS can
  change between check and fetch); acceptable for a staff-only tool, revisit before customers.
- Pair queues are assembled in Python from both tables (values only) on every page request. Fine
  for thousands of pairs; needs SQL-side pagination when matches reach hundreds of thousands.
- The Django admin stays the fallback for everything the UI does not cover (terms, sites, moves).
- Production serving (Caddy: `/api` → Django, `/` → `frontend/dist` with SPA fallback) is not
  set up yet.

## Overview, originals and source candidates (2026-09-15)

Three more staff-only screens replace admin clicking for operations checks and discovery review.
Same `StaffSessionAuth`, routers in `backend/marceline/review/` (`overview.py`, `originals.py`,
`discovery.py`), no model changes.

- **Overview** `GET /review/overview`: per enabled site, pages total / first seen in 24 h, crawl
  tasks pending and done / failed in the last hour (by `updated_at`), media pending / done in the
  last hour (by `fetched_at`) / skipped, availability counts and crawl fetches in the budget window
  (`crawler.services.BUDGET_WINDOW`) against `daily_request_budget`; `core.JobRun` rows; open review
  work (proposed pairs, leaks, matches, clusters with proposed members, safety review pages,
  proposed candidates); M2 totals. The SPA refetches every 60 s. Counts only, no URLs or media.
- **M2 numbers are parsed from `matching.m2.m2_report`** (`marceline/review/m2_summary.py`) instead
  of re-implementing its ground-truth rules. An unknown line raises `M2ParseError`, so a format
  change in the report fails the API tests rather than showing wrong numbers. The report runs on
  every overview request; fine for tens of references, cache it when the miss-reason index gets slow.
- **Originals** `GET /review/originals`, `GET /review/originals/{id}`: read-only counts per
  reference (frame positions, shared positions, matches by strength / status, leaks by status,
  distinct linked clusters) and, per original, every (reference, page) pair with the queue's pair
  status, match moments as mm:ss, and the report's miss reasons and cluster-linked pages. No page
  URLs; pairs link into the reference queue by route query parameters. Import stays CLI-only.
- **Source candidates** `GET /review/discovery/candidates` (filters status / kind / cms_guess /
  blocked; ordered by distinct creators, search hits, domain; at most 500 rows),
  `POST /review/discovery/candidates/decide` (`approve` / `reject` / `hoster` / `ignore`, bulk up to
  500 ids, all-or-nothing: unknown ids 404) and `GET /review/discovery/search-calls` (total and
  24 h cost, latest calls). Approve, reject and mark hoster **call the `SourceCandidateAdmin`
  actions** so the approval rule (disabled Site if absent, existing Sites untouched) has one
  implementation; `ignore` has no admin action and only sets the status. Approved candidates cannot
  be moved to another status from the UI (409): their Site exists and is handled in the admin.
- **No external resources for candidates.** Evidence is returned as plain strings and rendered as
  text; the candidate domain is the only link, a plain `noopener noreferrer` anchor for bare
  hostnames that the browser opens only on click. The build CSP still blocks any image or fetch.

Known gap: the overview lives at `/overview`, not `/`. Moving the reference queue off `/` needs a
one-line change in `ReferencesScreen` (`useSearch({ from: "/" })`), which was owned by a parallel
change; do it when those merge.
