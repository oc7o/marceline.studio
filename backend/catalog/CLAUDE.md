# Catalog: suppression, do-not-contact, site policy, catalogue share, objections, hosters

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Index suppression (ADR 0038, docs/suppression.md)
- **`catalog.CreatorSuppression` is the marker**: one row per name a performer who is not our
  customer told us to stop indexing, `OneToOne` on `Creator` (`PROTECT`), active while `lifted_at`
  is NULL. Kept for as long as we crawl — it is what stops the name coming back.
- **`Creator.objects.usable()` is the single read rule**: staff-reviewed (`needs_review`, #29) and
  not suppressed. `cached_creators` (page linking), `targets()`, `plan_queries`,
  `safety_rules.release_pages` all go through it. A new path that hunts, links, searches or reports
  on creators uses it too; free-text paths (`crawl_seed --query`, `discover_sources --query`)
  refuse a query naming a suppressed person, and `prospect_report` refuses outright.
- **Two write-side guarantees, because a read rule can be bypassed**: `index/breadcrumbs.py`
  filters suppressed creators out of every writer and out of a full rebuild, and a
  `BEFORE INSERT … RETURN NULL` trigger on `index_page_creators`
  (`index/migrations/0021_suppressed_creator_links`) silently drops a suppressed link. Silent on
  purpose: a crawl must store the page even when one credited performer is suppressed.
- `marceline/suppression.py` owns applying and lifting (CLI `suppress_creator`, dry run by
  default, idempotent). `redact` is pure; the name matcher `names_person` / `name_keys` lives in
  `crawler/sites/common.py` next to `normalize_name`. What is deleted and what is kept (titles,
  hashes, customer casts) is listed in that module's docstring and in docs/suppression.md — read
  it before changing either. It refuses to suppress the rights holder of an owned original.
- Lifting restores nothing: the next crawl re-links and rewrites. Never keep a copy to restore.
- **A database restore does not undo a suppression any more** (ADR 0057, DPIA R24):
  `apply_suppression` and `lift_suppression` append one line to `core.deletion_journal` — the creator
  id, the basis, the request date, the identity-check code, **never the name** — and
  `manage.py reapply_deletions` replays it after a restore. A **lift** is listed for a human, never
  replayed: the reason is free text and a still-suppressed name is the safe side.
- **A safety-flagged page is off-limits here too** (operator, 2026-09-17). Links and breadcrumbs
  are removed on every page, but `_page_text_changes` rewrites the stored text of **safety-clear
  pages only** — a flagged page's text is what `safety_scan` re-judges and a hotline report is
  built from. The flagged count is reported instead. Never add a suppression path that links,
  activates, re-crawls, rescores or releases a flagged page, on a lift or otherwise.

## Do-not-contact record (issue #98, ADR 0047, docs/do-not-contact.md)
- **A different promise from index suppression above.** That one is "stop indexing me"; this one is
  "stop writing to me". Neither implies the other, neither applies the other, and both staff screens
  say the other marker exists. Never wire one to set the other.
- **`catalog.DoNotContactRecord` stores a keyed hash and nothing that names anybody**: HMAC-SHA256
  over `kind:normalised identifier` with `DO_NOT_CONTACT_HMAC_KEY`, plus kind, date, channel,
  who recorded it, an optional `SET_NULL` link to a prospect `Creator` and a one-line note. One row
  per identifier, so one person may have several. Never add a field that could name somebody, a
  finding, a count or a site.
- **`catalog/do_not_contact.py` is pure**: `normalise` (the only place spellings are folded),
  `digest`, `blocked_positions`. Conservative on purpose — equivalent spellings collide, two people
  never do, and where the two pull apart (a national phone number, a handle without its platform)
  the rule keeps them apart or refuses. `catalog/services.py` holds the Django side:
  `do_not_contact_secret`, `record_no_contact` (idempotent), `contact_check` (one query, by
  position).
- **An empty key refuses, on every path.** Hashing with an empty key gives digests anybody can
  recompute; a check that cannot see the records answers "allowed" for everyone. **The key is never
  rotated** — the hashes are one-way, so a new key silently orphans every objection. It reaches
  `web` through `infra/compose.yaml` and is pinned by `marceline/tests/test_compose_env.py`.
- **Nothing prints an identifier.** `manage.py may_contact` answers by position, the admin takes the
  identifier as a write-only form field, and the row's `__str__` is kind plus date. Keep it that
  way: the admin's change log and any future report see field names, never values.
- **The journal hook is on the model, not on the service** (ADR 0057): `DoNotContactRecord.save()`
  appends one line per **new** row, because this model has two writers — `record_no_contact` and the
  admin form, which sets `identifier_hash` itself. A restore does not resurrect anything here; it
  **loses** the newest objections, so `reapply_deletions` re-inserts them from the stored digest
  (plus kind, date and channel). The note and `recorded_by` stay out of the journal — free text never
  enters it — and a replayed row says so in its own note.
- **The record is permanent**: no delete permission in the admin, no cascade from `Creator`
  (`SET_NULL`), untouched by `suppress_creator`, and out of reach of the customer-data deletion of
  ADR 0045 — no module of `marceline/deletion/` imports `catalog`, and two tests in
  `marceline/tests/test_customer_data_deletion.py` hold both ends. A prospect who became a customer
  and left is still someone who said no.

## Site policy: the two tripwires (docs/legal/crawl-position.md)
- **`catalog.SitePolicyChange` is the append-only record of every policy switch** (T4, issue #106):
  `enabled`, `capture_enabled`, `capture_play_click`, `age_confirmation`, `crawl_delay_s` and both
  budgets always; `paused_until` / `pause_reason` **only when an actor is set**, because the crawler
  writes those on every outage. Field, old → new, actor, reason, timestamp.
- **It is written by a database trigger, never by a signal** (`catalog/migrations/
  0024_site_policy_audit.py`). The flip that prompted this was a queryset `.update()`, which fires
  no `pre_save`/`post_save`; the trigger also covers the admin, a command, a shell `.save()`, a data
  migration and raw SQL. **Never replace it with a `post_save` audit.** The actor comes from two
  transaction-local Postgres settings that `catalog.policy_audit.policy_actor` sets; unattributed
  writes read `database`, which is a finding, not a bug. `apply_policy_update` is the checked helper
  from code. UPDATE and DELETE on the log are refused (SQLSTATE 23000), like the rights and notice
  logs — so a `Site` carrying audit rows cannot be deleted, which only `undo_approval` ever tries
  and which it already refuses for any related row.
- **`age_confirmation` is recorded as a shape** (`empty` / `approval, N cookie(s)`), in SQL and in
  `policy_audit.age_confirmation_shape`; a test compares the two. **Never store a cookie value.**
- The admin (`SiteAdminForm`) requires a reason for `capture_play_click`, `age_confirmation`,
  `enabled` and a **budget raise**, and saves as `admin:<user>`; `seed_sources` records itself with
  the sha256 of the `sources.md` it applied. `make status` prints the last 7 days.
- **`catalog.SiteRobotsPolicy` is what a site's robots.txt reserves** (T2, issue #105), written from
  the crawl worker's own robots.txt fetch: `catalog/robots_policy.py` stores it, decides the pause
  and renders the report lines, `catalog/robots_alerts.py` stamps `notified_at`/`notified_hash` once
  per new or changed file (which the daily operator report reads for its Crawler section,
  `crawler/safety_digest.py`) and mails on its own only while `tdm_reservation` is in
  `OPERATOR_MAIL_IMMEDIATE` (`core/operator_mail.py`, empty by default since 2026-09-19). See
  `backend/crawler/CLAUDE.md` for the three outcomes and the two settings — the rule that matters
  here is that a third-party AI reservation is **recorded and reported, never acted on**; the
  operator decides per site.

## Retention for the name itself (ADR 0048, issue #104)
- **A name is kept while it is *used*, and the rule lives in `marceline/name_retention.py`** —
  deciding "used" needs `matching`, `index` and `marceline.targets` together, which `catalog` may
  not import. Seven keep rules, each a reason code the report prints: `suppression` (active **or
  lifted**), `customer_original` (a `matching.Reference` cast, ADR 0031/0034 — ADR 0045's business,
  never this pass's), `target`, `watchlist`, `active_page`, `safety_record`
  (`Page.safety_release_creator` / `SafetyRuleDecision.creator`, both `SET_NULL`) and `source_list`.
- **`sources.md` is a keep rule, not an oversight**: `seed_sources` re-creates every name on that
  list, so expiring one would have the two fight each other nightly. The pass **refuses to run**
  when the file cannot be read — an unreadable keep rule must not look like an empty one.
- **Crawling never creates a `Creator`** (the four writers are `seed_sources`, the customer upload
  path, `seed_demo_dataset` and the admin); the crawler only links names that already exist. A
  deleted name does not come back on the next crawl.
- The clock is `marceline.CreatorRetentionClock.unused_since`, the shape of `Page.deactivated_at`:
  started when the pass first sees the name unused, cleared the moment a rule holds again. Deleting
  the row takes its `Page.creators` links **on deactivated pages** with it — ADR 0048 weighs that
  against ADR 0029's `linked` reactivation rule and decides for the deletion; the page row itself is
  never touched here.
- **A suppression marker is undeletable by this path, and not only in Python**: `PROTECT` raises
  `ProtectedError`, and the foreign key refuses raw SQL too (deferred to the constraint check, which
  `marceline/tests/test_name_retention.py` forces). A `ProtectedError` is reported as kept
  (`protected`), never allowed to fail the run.
- **Never a name in the output or in a log line** — ids, counts and reason codes. The `needs_review`
  flag is deliberately **not** a keep rule.

## Catalogue size and our share of it (issue #107, ADR 0049, tripwire T3)
- **`catalog.SiteCatalogueSize` is the denominator of our share of a site's catalogue**: one row per site with
  the site's own total, the `method` it came by, the **path shape** it was read at (never a URL,
  never an identifier), when it was last seen and a note on what it is worth.
- **No new request type, ever.** A figure comes from a page the crawler already fetched — the one
  optional, pure adapter hook `catalogue_total` (`crawler/sites/base.py`, same shape as
  `card_text`; `crawler/catalogue_totals.py` writes it, best effort, never costing a page) — or
  from a person (`manage.py catalogue_size --record`). Never a sitemap fetch or a last-page probe.
- **`METHOD_RANK` in `catalog/catalogue_size.py` decides who may overwrite whom**: a scraped number
  never replaces what a person entered, and an unchanged figure is re-stamped at most once per
  `RESTAMP_AFTER` so a site that prints its total on every page is not an UPDATE per page.
- **The numerator is counted in `index/catalogue_share.py`** — `catalog` may not import `index`, so
  the counts are stamped into the record with `counted_at` and read from there by the Site admin,
  `request_report` and `make status`. **Always both shares** (ever indexed and active): targeted
  indexing (ADR 0029) makes them very different and either one alone misleads.
- **The threshold monitors, it never enforces.** `CATALOGUE_SHARE_ALERT_PERCENT` (25, on the *ever
  indexed* share) mails the operator once per crossing through `SAFETY_ALERT_TO`
  (`catalog/catalogue_alerts.py`; `notified_at` cleared when it falls back under) and **changes
  nothing about any crawl**. It is a monitoring number, not a legal line.
- A site with no total is reported as **unknown**, never as zero.

## A site operator's objection (issue #108, ADR 0050, docs/runbooks/site-objection.md)
- **A different route from index suppression above.** That one is a *performer* saying "stop
  indexing me"; this is a *site* saying "stop crawling us". Neither implies the other.
- **`catalog.SiteObjection` is the record**: site, date, kind and channel as codes, note, decision,
  reason, decider, applied stamp. **Never the sender's name, address, telephone number, signature
  block or a quoted letter** — that stays in the mailbox, exactly as for `CreatorSuppression`.
- **It is a record, so it cannot be deleted**: no delete permission in the admin, `PROTECT` on the
  site. `declined` without a reason is refused by a database constraint.
- **`catalog/site_objections.py` is the one path that applies a decision**, and it adds no switch:
  `paused` → `paused_until` + `pause_reason` (`PAUSE_DAYS`, reversible, expires on purpose),
  `disabled` → `Site.enabled = False`, `media_stopped` → origin media budget 0, `declined` →
  nothing. Idempotent; applying again is not deciding again. **Every switch is written through
  `policy_audit.apply_policy_update`** (issue #106), so the trigger records actor `objection:<pk>`
  and the objection as the reason instead of the anonymous `database` — never a bare `.save()`
  here. A site with audit rows cannot be deleted, which is a second reason it outlives the record.
- **The pages stay.** Stopping a crawl is not a deletion, and **never reply to an objector with
  findings, counts or URLs** — read the runbook before answering anyone.

## Hoster intelligence (ADR 0027)
- `catalog.HostingRecord` (per hostname), `HostingProvider` (per origin AS), `HosterContact` (one
  notice and its outcome; nothing writes it until notices exist, #33/#46). Logic in
  `catalog/hosting/`: `jurisdiction`, `cdn`, `cooperation` are pure; `dns` (resolver + Team Cymru
  TXT, hand-rolled UDP), `rdap` (IANA bootstrap cached a day, `PoliteClient`), `lookup` (one host,
  no DB), `services` (refresh policy, `store_facts`, aggregates).
- **Never request the host itself** — DNS and RDAP registries only. Tests inject
  `httpx.MockTransport` and fake resolvers; RDAP fixtures in `catalog/tests/fixtures/rdap/` are
  trimmed registry responses with every individual's entity removed.
- `marceline/hosting.py` picks the hosts (customer findings' sites and preview hosts first, then
  enabled sites; flagged pages contribute nothing) and reduces media URLs to **hostnames only**.
  CLI `lookup_hosting`; scheduler job daily, limit 25.
- Customer API: `hosting = {page_host, content_hosts[]}` on each case finding
  (`marceline/app/hosting.py`), read-only, codes only. Jurisdiction is derived from `country` at
  read time. **Customers see only providers attached to their own findings, never a list of
  hosts, providers or sites, and never notice counts, exact rates or durations** — only
  `response_history` (ADR 0027 amendment 2026-09-17, `catalog/hosting/history.py`, pure): null
  below `HOSTING_HISTORY_MIN_NOTICES` (5) observed notices, else `removed_percent` rounded to 5 and
  `usual_removal_time` (median of timed removals floored to `same_day` … `30_plus_days`, null below
  3 timed removals). The `takedown_chance` / `takedown_speed` bands are **internal** now (forecast
  wording); raw history and numbers stay in the model and the `HostingProvider` admin.
  `jurisdiction` is a *suggested notice channel* code, never a statement that a law applies.
- **Customer wording** (operator 2026-09-17): API descriptions of customer schemas follow the
  wording rules in `frontend/src/app/CLAUDE.md` — matching copy, record, human-reviewed match, suggested
  notice channel; never leak, infringing, verified, escalate, handled. JSON keys stay stable.
- Takedown speed (ADR 0027 amendment): `catalog/hosting/speed.py` is pure (upper nearest-rank
  percentiles, "never" counts only after `OPEN_NOTICE_WINDOW`). `HosterContact.removed_observed_at`
  is stamped by `marceline.hosting.observe_notice_removals` (daily scheduler job) from the pages a
  notice names (`index.Page.notices`) — never typed by hand, and only as precise as the recheck
  interval (about a day).
- **Provider function and complaint channels (ADR 0027 amendment 2026-09-17).** Location/nexus
  (`eu_facing|us_nexus|other|unknown`, a hint, never a verdict) and function are separate. Pure
  policy in `catalog/hosting/providers.py`: `derive_function` (override with note → mechanical:
  registrar slot, detected CDN = `reverse_proxy_cdn` → directory default → `origin_host`/`unknown`;
  **never derive `storing_cdn`**, only a `ProviderFunctionOverride` sets it), `resolve_nexus`,
  `v1_support(function, kind)` (host = `primary`, search = `delisting_draft`, reverse proxy =
  `forwarding_only`, registrar/payment/advertiser = `additional_contact_only`), `suggest_channel`,
  `is_role_address`. DB side `catalog/hosting/directory.py`: `provider_roles(hostnames, now)`
  (slots `service`/`network`/`registrar`; RDAP abuse contacts only as fallback channels),
  `supersede_channel` / `mark_rechecked` / `retire_channel`, `due_channels`.
- `ComplaintChannel` rows are **versions**: never update one in place (the admin's `save_model`
  supersedes). Role addresses only, never a person's name or mailbox. `transfer_basis` is staff-set
  per recipient, never inferred from a country. Model choices must equal the policy tuples (a test
  compares them). Staff-only today: admin + `provider_directory_report`; the customer `hosting`
  section does not carry these fields yet.
