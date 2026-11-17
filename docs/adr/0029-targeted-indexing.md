# 0029 — Targeted indexing: crawl and match what there is an Auftrag for

Status: accepted (2026-09-17)

Issue #71. Operator decisions of 2026-09-16: site onboarding is frozen; stop indexing everything;
crawl what a customer has commissioned (an *Auftrag*); deactivate, never delete; keep site-level
breadcrumbs of which creators a site carries.

## Context

The scheduler seeded every enabled site × every `catalog.Creator` from `sources.md`, and three
adapters also seeded the whole site (site11's `/videos/` feed, site09' `/videos`, site07's date-
sorted home listing over ~26k videos). Matching, clustering, duplicates and common frames then
scanned every page frame. Most of that is about creators nobody pays us to protect: it costs crawl
budget, media requests and scan time, and it fills review queues with noise.

Deleting it would be wrong twice over: a few MB are cheap, and re-crawling a site to rediscover
what we already knew is not. And when a creator signs, we want to know at once where they already
are.

## Decision

### Targets

`marceline/targets.py` is the single answer to "whom do we hunt, at what priority":

- **Auftrag** — until real contracts exist, a creator who performs in at least one original owned
  by an organisation. Ownership is exactly `marceline.app.ownership.owned_references` (ADR 0021;
  since ADR 0031 the organisation column, no longer `uploaded_by` → membership), evaluated per
  organisation. **Every
  performer counts**: the rights holder (`Reference.creator`) and co-performers
  (`Reference.creators`, ADR 0010 amendment). Unassigned originals (operator/CLI imports nobody assigned) own
  nothing and make nobody a target.
- **Watchlist** — `catalog.WatchlistEntry` (creator, note, added_at), staff-managed in the admin:
  creators the operator follows deliberately. A creator with an Auftrag is never also a watchlist
  target. *(Superseded 2026-09-17: the watchlist is noted, not crawled, unless an entry's `crawl`
  is ticked; co-performers rank below rights holders — see the amendment.)*

It lives in the project package because it needs `accounts`, `matching`, `crawler` and `index`
together. No import-linter contract changes.

### Crawling

- `crawl_seed` moves from `crawler` to the project package (it now needs the targets) and seeds
  **targets only** by default: Auftrag creators at `SEED_PRIORITY` (30), watchlist creators at
  `WATCHLIST_SEED_PRIORITY` (15) — behind every Auftrag seed and its top results (20), ahead of
  ordinary details (10). `crawler.services.enqueue_seeds` moves a pending seed to its current
  priority, so a creator promoted from the watchlist is promoted in the queue too.
  `--creator NAME` stays the manual override (any creator, Auftrag priority), `--query` is
  unchanged. The scheduler's `crawl_seeds` job calls it as before, so it is targeted too.
  *(Priorities amended 2026-09-17: rights holders 30, co-performers 12–28, crawled watchlist
  entries 11, prospects 11 and last, with a daily seed cap — see the amendments.)*
- **Broad listings leave `seed_tasks`.** The `SiteAdapter` protocol gains
  `listing_tasks(site)` — start URLs about no creator. Only site11, site09 and site07 have
  any; they run only with `crawl_seed --listings`.
- **Chains already queued.** A newest/catalogue chain in the queue keeps paginating after seeding
  stops. The dry run counts its pending listing/search tasks; `deactivate_untargeted --apply
  --stop-broad-listings` skips them (pending detail tasks finish). The scheduler never passes the
  flag, so a deliberate `crawl_seed --listings` run is not undone behind the operator's back.
- **Rechecks** (`crawl_recheck`, the scheduler's recheck and never-checked batch) skip inactive
  pages; `--include-inactive` asks for them. Reviewer rechecks of named pages are unchanged.

### Deactivation, never deletion

`index.Page` gains `active` (indexed, default true), `inactive_reason` (`untargeted`; a check
constraint ties it to `active`) and `active_changed_at`. New pages start active.
`deactivate_untargeted` (`--dry-run`, the default, or `--apply`) sets a page inactive unless one of
these holds, and sets it active again when one does:

| Rule | Holds when |
|---|---|
| `linked` | `Page.creators` names a target (rights holder, co-performer, crawled watchlist entry or prospect) |
| `matched` | a `ReferenceMatch` or `ReferenceLeak` row, any status, ties the page to an original of a target |
| `clustered` | the page is a non-rejected member of a cluster with a non-rejected `linked`/`matched` member |
| `seeded` | its detail task was discovered from a target's seed task, directly or through that seed's listing/search pagination — never through another detail page |
| `safety` | the page is not safety-clear: flagged pages keep their existing handling (ADR 0018, incidents, rechecks) |

Only pages this rule deactivated are ever reactivated by it. `--apply` refuses to run when there
are no targets at all, which would deactivate every clear page. The dry run prints, per site,
pages, active now, active after, deactivate, reactivate, and the same for frame hashes, plus how
many pages each rule keeps.

**How pages are tied to creators today** (the `linked` rule): `crawler.services.match_creators`
links every `Creator` whose name or alias appears as whole words (ASCII-normalised) in the detail
page's title, tags or performer credits — against all creators, not only targets. The seed query
is not recorded on the page; `seeded` reconstructs it from `CrawlTask.discovered_from`.

Weak spots, measured by nothing yet:
- A leak whose page never names the creator (hashtag titles, "FULL HD HERE:") is only kept by
  `seeded`, `matched` or `clustered`. A page first discovered from a broad listing keeps that
  `discovered_from` (`enqueue` only rewrites it while a detail task is still pending), so a later
  target search that finds it again does not make it `seeded`.
- Short or common aliases link unrelated pages (over-inclusion, harmless but noisy).
- `seeded` over-includes where a site's search is fuzzy, and site09' search enqueues the
  catalogues of every similar handle it lists.
- The page URL must equal its detail task URL; a canonical URL that differs breaks `seeded`.
- Proposals (`ReferenceMatch`/`ClusterMember` `proposed`) on inactive pages are derived data and
  disappear on the next matching run; human decisions stay. Originals whose creators are no target
  (the operator's CLI imports) therefore lose their proposed matches on inactive pages — put those
  creators on the watchlist with `crawl` ticked first (amendment below) if their M2 numbers
  still matter.

### What skips inactive pages

Reference matching (`load_page_frames`, shared-frame marking, the recall helper, the incremental
scan), duplicates and cluster evidence (PDQ, SHA256, external links, mirror groups), common frames,
leak suggestions, the media queue (`claim_media`: nothing is fetched for them) and browser-capture
triage (`page_inactive`). `evaluate_threshold` and label imports still load pages named by human
labels. The ANN/Hamming index needs no change: it only ever holds what those loaders hand it.

The incremental scan (ADR 0015) treats frames of a page reactivated since the last scan as new
(`active_changed_at`), and the scheduler's frame-hash guards count such a page as new work, so a
new target's old pages are matched without `--full`.

### Breadcrumbs

`index.SiteCreatorBreadcrumb` (site, creator, page_count, first_seen, last_seen): which creators a
site roughly carries. Counts and dates only, no URL, **every creator**, active page or not.
*(Superseded 2026-09-17: a deactivated page no longer counts — see the retention amendment.)* The
crawler refreshes the rows of the creators it links on each detail page
(`index.breadcrumbs.record_page_links`, a recount, so re-crawls do not inflate it);
`deactivate_untargeted --apply` rebuilds all of them from `Page.creators`. This is the "standing
per-creator interest in a site" issue #32's re-upload watch needs as well. Read-only admin.

### Scheduler

Job `sync_targets` (every 3 h, before `build_clusters`) runs `deactivate_untargeted --apply`, but
is skipped until `TARGETED_INDEXING_ENABLED` is true: the first application changes what matching
sees, so the operator reviews the dry run and applies once by hand. `crawl_seeds` is targeted from
the moment this is deployed.

## Consequences

- With no owned originals and an empty watchlist, the scheduler seeds nothing. That is the
  intended state until the operator adds watchlist entries or a customer uploads.
- site07 creators without a known performer page are no longer reachable without `--listings`.
- site11's `.mp4` URLs only exist in its global feed; without `--listings` site11 contributes
  photos and profile posts only.
- `Page.active` is a matching-side flag, not retention. Nothing is deleted, safety incidents are
  untouched, and a flagged page is never deactivated.

## Amendment 2026-09-17: co-performers by prominence, the watchlist is not crawled

After the first live dry run (26 targets; 50,054 pages → 14,965 active; 145,686 frame hashes →
54,219) the operator decided:

1. **Co-performers are indexed, below rights holders, scaled by prominence.** A *rights holder* is
   the primary creator (`Reference.creator`) of any owned original and keeps `SEED_PRIORITY` (30).
   A *co-performer* appears in owned originals only through `Reference.creators`, never as primary,
   and gets `marceline.targets.co_performer_priority(originals, partners)`:

   `min(12 + 3 × (originals − 1) + min(partners − 1, 3), 28)`

   where `originals` counts the owned originals they perform in and `partners` the distinct other
   performers of those originals. Named constants: `CO_PERFORMER_MIN_PRIORITY` 12,
   `CO_PERFORMER_PER_EXTRA_ORIGINAL` 3, `CO_PERFORMER_PER_EXTRA_PARTNER` 1 (bonus capped at
   `CO_PERFORMER_MAX_PARTNER_BONUS` 3, so a crowded cast never outranks repeated appearances),
   `CO_PERFORMER_MAX_PRIORITY` = `SEED_PRIORITY` − 2. A one-off co-actor next to one rights holder
   (1 original, 1 partner) gets 12; a performer in 4 owned originals gets 21 with one partner, up
   to 24 with four or more. A creator who is a rights holder anywhere is a rights holder.
   Co-performers are targets for the active-page rule like rights holders.
2. **The watchlist is not crawled.** It records interest — note, `added_at` — and the creator's
   breadcrumbs stay visible in the admin; its creators get no seeds and being on it keeps no page
   active. A per-entry `crawl` boolean (default false, migration `catalog/0010`) turns an entry
   into a target at `WATCHLIST_SEED_PRIORITY`, now 11: below every co-performer, above ordinary
   details. The dry run lists uncrawled watchlist entries separately.

`TargetKind` is now `rights_holder`, `co_performer`, `watchlist` (crawled entries only), and
`crawl_seed` reports the three counts. With no owned originals and no crawled watchlist entry the
scheduler seeds nothing.

## Amendment 2026-09-17: prospect research

A fixed list of creators ("prospects") is indexed without originals from them, to measure how
much of their content circulates.

1. **Prospects are watchlist entries.** `WatchlistEntry.purpose` (`interest`, the default, or
   `prospect`; migration `catalog/0012`). Aliases and studio names go into `Creator.aliases`.
   A crawled prospect is `TargetKind.PROSPECT` at `PROSPECT_SEED_PRIORITY` (11): the lowest
   target priority, below every co-performer, tied with crawled interest entries and listed after
   them. A prospect without `crawl` is noted like any uncrawled entry.
2. **Daily seed cap.** `crawl_seed` queues at most `PROSPECT_DAILY_SEED_CAP` (default 4) of one
   prospect's seed tasks in any rolling 24 hours, across all sites: a seed counts while pending or
   running and for 24 hours after it was fetched, and the allowance goes to the least recently
   fetched seeds first (`marceline.targets.prospect_seed_plan`). The plan depends only on the
   queue, so the scheduler's per-site calls agree on it. Rights holders, co-performers, interest
   entries and `--creator` overrides are never capped.
3. **The active-page rule is unchanged in form:** a crawled prospect is a target, so `linked`,
   `matched`, `clustered` and `seeded` keep its pages active, and `target_seed_urls` uses the
   prospect's full seed list (not the capped one), so a page found through a seed that is not
   queued today stays active.
4. `prospect_report` (`marceline/prospects.py`) reads the index and writes nothing; reviewer
   verdicts on its verification sample live in `index.ProspectSampleCheck` (migration
   `index/0014`). Attribution is by names and PDQ clusters only — never biometrics.

## Amendment 2026-09-17: a finite retention for deactivated pages

Deactivation was deliberately not deletion ("Consequences" above: "Nothing is deleted"), because
re-crawling a page to rediscover what we already knew is expensive and reactivation is a genuine
purpose. Left there, the retention period was in effect "forever", which storage limitation does
not allow. The operator decided the period: **24 months**. The fix is a cap on how long a deactivated
page is kept, not turning deactivation into an immediate delete — reactivation still has to work.

Two separate things happen, at two separate times, both in `index.retention`:

1. **At deactivation** (`marceline.targets.apply_activity`, which already runs
   `deactivate_untargeted --apply`), `scrub_deactivated_pages` drops what is about the people the
   page named:
   - **Dropped:** `title`, `tags`, and the performer credits inside `raw_extraction` (the
     parser's own `"performers"` key and `crawler.sites.base.DROPPED_PERFORMERS_KEY`,
     `"dropped_performers"`).
   - **Kept:** the URL, every timestamp (`first_seen`, `last_seen`, `uploaded_at`,
     `availability_checked_at`), the site, `raw_extraction`'s non-personal keys (e.g.
     `duration_s`, `external_links`), every `Media`/`FrameHash` — and, unlike the fields above,
     `Page.creators`. That link is not incidental: it is exactly what the `linked` reactivation
     rule reads to notice a page again once its creator becomes a target, so clearing it would
     make deactivation one-way for precisely the pages this feature exists to let back in.
     Instead, the site x creator breadcrumbs (above) now count only **active** pages
     (`index.breadcrumbs`, every counting query), so a scrubbed page's credit disappears from
     human view there the moment it deactivates without touching the page's own link.
     Reactivation via `matched`/`clustered`/`seeded` needs none of the dropped fields either, so
     reactivation works from what is kept whichever rule fires. A page can credit several
     people; scrubbing only ever clears fields on the page row itself, never the row, so removing
     one name never removes the page.
   - Idempotent: a page with nothing left to clear is a no-op. Reactivation clears the field
     below at once, and `Page.active=True` rows are never touched by any of this, however old.
2. **`index.retention.PAGE_RETENTION_MONTHS` (24) later**, `page_retention` (CLI, scheduler job
   guarded by `PAGE_RETENTION_ENABLED` exactly like `sync_targets`/`TARGETED_INDEXING_ENABLED`
   above) deletes the page row outright — `Media`/`FrameHash` and the kept `Page.creators` link
   cascade with it; the breadcrumbs need no rebuild, since a deactivated page was already excluded
   from them. `Page.deactivated_at` is the clock: set to the
   deactivation moment by `apply_activity`, cleared to NULL the moment the same page reactivates,
   so a page whose target status changed inside the window never expires by this clock — only a
   page that has stayed untargeted for the whole period is deleted. Two guards, of different
   kinds, keep a page a safety case still needs from being deleted out from under it:
   - **A page attached to an `open` or `reported` `index.SafetyIncident`** is excluded from the
     candidate set outright (`index.retention.OPEN_SAFETY_STATUSES`, a plain query, checked before
     anything is attempted — so a dry run reports it too). This closes a gap `keep_rules.safety`
     leaves open: that rule keeps any page whose *own* `safety_status` is not `clear` active
     forever, so a currently blocked or under-review page is never even a candidate here — but a
     page can be `clear` again (a staff reversal) while the case it belongs to, opened for other
     evidence too, stays open because nobody has closed it yet. Neither `SafetyIncidentPage`
     (`page`, `on_delete=SET_NULL`, its URL copied precisely so the case survives the page row)
     nor `SafetyIncidentHash` (no relationship to `Page` at all — it is copied onto the incident
     the moment a page is blocked, in the same step that deletes it from `FrameHash`) needs a
     guard of its own: deleting a page's row cannot corrupt either. The guard above is about not
     deleting evidence an open case might still need, not about protecting those tables.
   - **A page a customer has drafted a real notice about** is protected from deletion at the
     database level (`marceline.notices.models.NoticeFinding.page`, `on_delete=PROTECT`); the
     command reports it as kept rather than failing the whole run (a dry run cannot see that in
     advance — there is no query for it, only the attempt).

   Existing pages `deactivate_untargeted` had already deactivated before this
   field existed have their clock backfilled from `active_changed_at` (migration `index/0020`,
   the timestamp of that deactivation); they are not retroactively scrubbed of title/tags/credits,
   only newly deactivated pages are, from this change on. *(Reversed 2026-09-19: that stock is
   scrubbed after all, and the scrub is made durable — see the amendment below, issue #148.)*
   Like `rights_housekeeping`/`safety_retention`: `--dry-run` (default) prints per-site counts and
   writes nothing, `--apply` deletes; deletion is irreversible, so the command defaults to a dry
   run and the scheduler job stays off until the operator turns `PAGE_RETENTION_ENABLED` on by
   hand, after reviewing that dry run.

## Amendment 2026-09-18: breadth-first seeding with a title gate

Targeted indexing assumes every site can be asked *about a creator*. site13.example cannot:
its only usable search is a substring match over **uploader handles**, while our
targets are performers named in the *title* of somebody else's upload. Twenty-six correct "no such
uploader" answers in 27 requests stored zero pages — the adapter was right, the index we asked was
the wrong one. Its catalogue, on the other hand, is one request per 50 card titles over ~3,240
pages, and each card reads `Handle - Title`.

So broad listings get a second mode, general and per site, not a special case for this one:

1. **The adapter declares the capability.** `TaskSpec.text` carries the visible text of the card a
   detail link came from, and an adapter that fills it says so with `card_text = True`
   (`crawler.sites.base.has_card_text`). Adapters stay pure: they hand over the card's text, they
   do not know any creator. A site whose cards show no title simply never declares it and keeps
   the plain pagination chain it always had.
2. **The gate is this ADR's `linked` rule, applied one step earlier** — to the card instead of the
   page. `crawler.listing_walk.TitleGate` matches with `crawler.sites.common.names_person`: the
   same ASCII-folded, case- and separator-insensitive whole-word normalisation
   `crawler.services.match_creators` links a creator by, plus that function's separator-less
   spelling ("SashaMeyer"), applied per word because a card is `Handle - Title` and not a bare
   handle. It is therefore strictly wider than the rule that will judge the page, which is the
   direction we want: a false positive costs one page that ADR 0029 then deactivates like any
   other untargeted page, a false negative is a leak we never see. A card with no text at all is
   admitted; with no creators at all nothing is.
   The gate is compiled once per run from `crawler.services.cached_creators` — every linkable
   creator, not `marceline.targets` (the crawler may not import the project package, and the wider
   set again errs towards fetching).
3. **The walk is one frontier task.** A 3,240-page chain may not enter the queue at once, so a
   walked listing page keeps exactly one pagination link, the nearest page ahead, and drops the
   paginator's jumps to the far end. The walk advances as fast as the site's delay and daily
   budget allow and stops wherever the day ends. **The queue is the whole state** — no cursor
   model, the same way `crawl_recheck` reads `availability_checked_at` rather than keeping one: a
   task belongs to a walk by its start URL in `CrawlTask.context`, handed down by
   `store_parse_result` to the successor it enqueues.
4. **`crawl_seed --listings` stays the only entry point** and stays off by default. It now starts
   or resumes the walk (`crawler.services.enqueue_listing_walks`) and reports each one as started,
   walking, resumed or complete. A walk whose frontier was skipped by `deactivate_untargeted
   --stop-broad-listings` or failed goes back into the queue **at the page it stopped on**, never
   at page 1; a frontier that is done with no successor is the end of the catalogue and is
   reported rather than re-fetched (`--refresh` is how the operator re-reads the newest page).
   A chain queued before this existed is adopted by its start URL and walked again, gated.

Nothing about targeting changes. A page fetched this way is fetched *because* its card named a
creator, so the `linked` rule keeps it active on its own text as usual, and a page whose card
promised more than the page delivers deactivates with everything else untargeted.

Budgets are the other half of the boundedness: site13.example's page budget goes from 1,000
to 9,000 requests a day (`catalog/sources.py`), which buys the whole catalogue walk once (3,240
requests, 162,000 card titles screened) plus ~5,700 detail pages for the cards the gate admitted.
Its media budget stays at 1,000: the gate makes breadth cheap, not pictures.

## Amendment 2026-09-19: the scrub is durable, and the stock is caught up (issue #148)

Two defects in the amendment above, both measured on the development database on 2026-09-19:
97,782 deactivated pages, **54,669 of them still holding a title**, 48,557 tags and 39,231
performer credits — all with `inactive_reason = untargeted` (the total page count keeps moving
while the crawl runs, so it is deliberately not quoted here).

1. **No backfill.** `scrub_deactivated_pages` was called from exactly one place, with the pages one
   `apply_activity` run had just deactivated. Everything deactivated before the scrub shipped
   (2026-09-18) was never scrubbed, which is what the amendment above said in one clause while
   the project's documentation described the scrub as covering the whole index.
2. **The scrub did not hold.** `crawler.services.store_parse_result` wrote `title`, `tags` and
   `raw_extraction["performers"]` unconditionally in its upsert, so a re-crawl of a deactivated
   page put back exactly what the deactivation had deleted. 667 pages had been re-seen after their
   `deactivated_at`.

The decision, of the two options the issue names: **`store_parse_result` leaves those three fields
alone for a page that is still inactive for the `untargeted` reason** (`index.retention
.stays_scrubbed`), rather than re-applying the scrub after every parse. It is the smaller change
and it says what deactivation means — what the row may hold is decided by the deactivation, not by
the crawl. Everything else about the page is stored as before, including the `Page.creators` links
a reactivation needs, so nothing about letting a page back in changes; the moment it is active
again the next parse fills the three fields normally. A page inactive for some *other* reason is
not treated as scrubbed: a new `inactive_reason` has to say what it wants here deliberately.

- **Backfill:** `manage.py scrub_inactive_pages` (`index.retention.backfill_scrub`), dry run by
  default like every deleting command, `--apply`, `--batch-size`, `--limit`. Batched with a commit
  per batch and **resumable without a cursor** — the candidate query (inactive, `untargeted`,
  safety-clear, still holding one of the three) *is* the position, so an interrupted run is
  continued by running it again. It reports candidates per deactivation day and every inactive page
  holding text per reason.
- **A safety-flagged page is never scrubbed, and now by its own rule.** `keep_rules.safety` keeps a
  non-clear page active, so the deactivation path never handed one to the scrub; the filter now
  sits in `scrub_deactivated_pages` itself, so the backfill and any future caller inherit it. On a
  flagged page the stored text *is* the safety record (docs/suppression.md, docs/safety.md): the
  count is reported, the text is left.
- **One invariant, cheap enough to print:** `index.retention.scrub_gap` (a single aggregate) and a
  line in `make status` — "page scrub: N of M deactivated page(s) still hold title, tags or
  credits". It must read 0. A regression is then visible in the command the operator runs at the
  start of every session, rather than in a query nobody thought to run for two days.
- **Why an inactive page was re-crawled at all**, since `crawl_recheck` skips them:
  `crawler.services.recover_expired_media` re-queued the producer task of any page whose tokenized
  media links had expired, without looking at `Page.active` (649 of the 667 carried its own
  recovery counter). Media of an inactive page is never claimed (`matching.media.claimable_media`),
  so that re-crawl could not lead to a fetch — it only spent page budget. It now skips inactive
  pages. Discovery itself is unchanged: a listing, a search or a walk that links a deactivated
  page's URL may still lead to a fetch, and should — that fetch is how the page would be found
  again for a new target — it simply no longer rewrites the scrubbed fields.

No migration: nothing about the schema changes, only which rows are written and when.
