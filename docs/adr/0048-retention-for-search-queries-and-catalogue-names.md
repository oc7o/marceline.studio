# 0048 — A search query's text goes in days, a catalogue name once nothing uses it

Status: **accepted** (2026-09-18). Built and **off by default** (`RESEARCH_RETENTION_ENABLED`); the
three periods are settings and **the operator confirms the numbers**. Issues #103 (search queries)
and #104 (the name itself).

Builds on ADR 0013 (discovery and its spend guard, which is the same table), ADR 0029 and its
retention amendment (the house pattern: scrub first, delete later, dry run by default, counts only,
protected rows reported as kept, scheduled last, off until enabled), ADR 0038 (index suppression —
the marker that must survive), ADR 0031/0034 (a rights holder or co-performer of an owned original
is customer data), ADR 0045 (customer-data deletion, whose pass frees cast names) and ADR 0047 (the
do-not-contact record, which must outlive everything here).

The ADR number: 0046 is taken by `origin/notices-on-production`, 0047 by
`origin/do-not-contact-record`; this branch took **0048**.

## Context

Two rows had no retention period at all, and "forever" is the one period Art. 5 (1) (e) never
allows:

- **`crawler.SearchCall.query`** keeps the full discovery search text. A query is a name or an alias
  in one of ADR 0013's templates, so the row is a record that *this named person was searched for in
  this context* — and it is the one place in normal operation where a name
  leaves the house, to a search-results API provider.
  Nothing deleted such a row, and `crawler/admin.py:279-283` refuses add, change **and** delete, so
  even a deliberate erasure needed raw SQL.
- **`catalog.Creator`** holds the name and its aliases. Index suppression (ADR 0038) removes the
  *links* and deliberately **keeps the name**, because `CreatorSuppression` hangs off that row and
  is what stops the name being indexed again. Outside a suppression there was no path at all.

Two facts about the catalogue that this ADR depends on:

- **Crawling never creates a `Creator` row.** The four writers are `seed_sources` (the operator's
  own `sources.md`), the customer upload path (`marceline/app/fingerprints.py`, `needs_review=True`),
  `seed_demo_dataset` and staff in the admin. The crawler only *links* names that already exist
  (`cached_creators` → `Creator.objects.usable()`). A deleted name therefore does not come back on
  the next crawl.
- **`seed_sources` is idempotent and re-creates every name in `sources.md`.** So a name on that list
  cannot be expired without the two fighting each other every night. That list is a keep rule, not
  an oversight.

## Decision

One command, `manage.py research_retention`, with three passes in one order; one scheduler job,
**last of all**, behind one flag. Dry run by default, counts and ids only, **never a name in output
or in a log line**, every kept row reported with a reason code.

### Pass 1 — `queries`: the text goes, the row and the money stay

`crawler/retention.py`. After `SEARCH_QUERY_RETENTION_DAYS` (**30**), `SearchCall.query` is set to
`""` and `query_scrubbed_at` is stamped. Everything the row is *also* for is kept: `engine`,
`mode`, `location`, `language`, `result_count`, `cost_usd`, `status`, `error`, `created_at`.

**The row is never deleted, and the admin's "no delete" stays.** These rows are the money ledger:
ADR 0013's spend guard sums `cost_usd` over a rolling 24 h window before every paid request, and the
admin and the staff discovery API show the lifetime total. Deleting rows would make the lifetime
total a lie and would let a restart buy again what a crash already paid for. The personal part of the
row is the query text, so **scrubbing the text is the deletion** — what remains is an accounting
line, and a retention rule that keeps an accounting line is ordinary.

Why days and not months: a query's text has no use once its candidates are recorded. The candidates
*are* recorded, in the same transaction (`SourceCandidate`, its capped evidence samples, and the
`CandidateCreator` row). 30 days is one candidate-review cycle — the 2026-09-17 review of proposed
candidates read runs from days earlier — and nothing operational reads the text after that. 7 days
would also be defensible and is the alternative put to the operator.

**The same pass scrubs the discovery evidence**: `SourceCandidate.evidence["queries"][*]["query"]`,
for every candidate not touched for the same period (`last_seen_at`). That is the same text, copied.
The sample's `rank`, `url` and `title` stay: they are the evidence that this domain carries copies,
and a result title is the pirate site's own words — the same category as `Page.title`, which
suppression keeps on purpose. A candidate still being touched (link mining keeps bumping
`last_seen_at`) keeps its samples while that review lasts; the period is purpose-linked, and the
record says so rather than pretending otherwise.

**A candidate keeps its pointer to the creator that prompted it** (`crawler.CandidateCreator`). It
is two integers, it is what `distinct_creators` — a candidate's rank in the review queue — is
recomputed from, and it is not a free-text record of a search. It needs no clock of its own: it is
`CASCADE` on `Creator`, so pass 3 takes it with the name.

**Suppression scrubs instead of deleting, for the same reason** (`marceline/suppression.py`). It used
to `DELETE` the matching `SearchCall` rows, which removed the name *and* silently changed the spend
ledger. It now scrubs them through the same function. The name goes exactly as before; the
accounting no longer moves.

### Pass 2 — `prospects`: research nobody acted on

`marceline/name_retention.py`. A `catalog.WatchlistEntry` with `purpose=prospect` older than
`PROSPECT_RESEARCH_RETENTION_DAYS` (**90**) is *forgotten*: the entry, the
`index.ProspectSampleCheck` verdicts, the `Page.creators` links and the
`index.SiteCreatorBreadcrumb` counts for that name. This is the clearest case in both issues:
research nobody acted on is deleted after three months. 90 days is that period, counted from the
day the prospect was put on the list.

**The clock is `added_at` and a new verdict does not extend it.** A rolling clock would keep the
research alive for as long as we keep looking at it, which is precisely what a fixed period
forbids. A prospect who signs stops being seen by this rule
because they become a rights holder or co-performer target; a prospect still in conversation at day
90 is a conversation, and the operator raises the period or converts them. The dry run prints the due
date per entry (by creator id) so it is never a surprise.

**A forgotten prospect's name goes in the same run** if nothing else uses it — no second clock. The
research *is* the only reason we held the name, and "nothing of the research remains" should not
mean "except the name, for another three months".

### Pass 3 — `names`: a name is kept while it is used, and not after

A `catalog.Creator` is **in use** when any of these holds; each is a reason code the report prints:

| Code | Rule | Why |
|---|---|---|
| `suppression` | any `catalog.CreatorSuppression` row, active **or lifted** | the marker is what keeps the name out of the index; deleting it would undo the request. A lifted row is the accountability record of a request and its lift |
| `customer_original` | `matching.Reference.creator` or `Reference.creators` | a rights holder or co-performer of an original is customer data (ADR 0031/0034); its retention is ADR 0045, never this pass — including an *unassigned* original, which is ours but not research |
| `target` | in `marceline.targets.targets()` | whoever we hunt today: rights holder, co-performer, a ticked watchlist entry, a prospect inside its period |
| `watchlist` | any `catalog.WatchlistEntry` | noted interest is a purpose of its own; a prospect entry past its period is deleted by pass 2 first, and then this rule no longer holds |
| `active_page` | linked to at least one **active** `index.Page` | the index is doing its job with this name |
| `safety_record` | `index.Page.safety_release_creator` or `index.SafetyRuleDecision.creator` | both are `SET_NULL`, so deleting the name would quietly empty a field of a safety record. A safety record is never edited by a retention pass |
| `source_list` | the name is in `sources.md` | `seed_sources` would re-create it tonight. It is the operator's own list, and a list is a purpose |

A creator with no reason gets a clock: `marceline.CreatorRetentionClock.unused_since`, set the first
time the pass sees it unused and **cleared the moment any rule holds again** — exactly
`Page.deactivated_at` in ADR 0029. The clock is not a column on `catalog.Creator`, because deciding
"unused" needs `matching`, `index`, `accounts` and `marceline.targets` together, which `catalog` may
not import; the table lives where the rule lives, `OneToOne` and `CASCADE`, so it disappears with
the row. `CREATOR_NAME_RETENTION_DAYS` (**90**) after that, the `Creator` row is deleted with its
aliases; the cascade takes the breadcrumbs, the prospect verdicts, the candidate-creator pointers and
the `Page.creators` links.

Starting the clock at *first sight* rather than at the last real use is deliberate: nothing records
when a page link died, and inventing a date backwards would delete on the first run. The cost is
that the first run after switching the flag on deletes nothing, which is the right direction.

**`needs_review` is not a keep rule.** A name a customer typed that staff never confirmed is already
out of everything (`Creator.objects.usable()`); if it is in a reference's cast it is kept by
`customer_original`, and if it is attached to nothing it is exactly the row issue #104 names.

### The `Page.creators` link on a deactivated page goes with the name

This is the one real trade-off, and ADR 0029 decided the other way for its own purpose: scrubbing a
deactivated page keeps `Page.creators` so the `linked` rule can reactivate the page when that person
becomes a target. Weighed against 90 days after the name fell out of use:

- what is kept is the **last statement on that page that names the person** — its title, tags and
  performer credits were scrubbed at deactivation, so the link is all that is left — and it would be
  kept for up to 24 more months, until `page_retention` deletes the page;
- what it buys is a convenience we can reproduce: if the person later signs, `crawl_seed` seeds their
  name on every enabled site and the page is found again if it still exists. If it no longer exists,
  reactivating it was worth nothing.

So **the links go with the name**. The page row itself stays — it is ours, and its own clock runs.
The honest sentence for the privacy notice: after the name has been unused for the period, the
association between that name and a deactivated page is deleted with the name, and what remains of
the page is its address and our own technical records, with no name in them.

### Interactions

- **`page_retention` (ADR 0029)** runs before this job. When a page is deleted its links go too, so
  an orphaned name becomes unused and starts its clock on the same night.
- **Customer-data deletion (ADR 0045)** runs before this job as well, which is why `names` is last
  of all: deleting an organisation frees its cast, and those names are then judged by their own rule
  on the next run (never in the same one — `customer_original` is read before the deletion in the
  same pass order).
- **Suppression (ADR 0038)** is untouchable here: `suppression` is the first keep rule, and it is
  enforced by the database, not by the rule — `CreatorSuppression.creator` is `PROTECT`, so the ORM
  raises `ProtectedError`, and the foreign key refuses a raw `DELETE` on the creator row with
  `IntegrityError`. Both are tested. A `ProtectedError` from any guard is reported as kept
  (`protected`), never allowed to fail the run.
- **The do-not-contact record (ADR 0047)** is `SET_NULL` on `Creator` by design: a prospect row may
  go, the "no" stays. Nothing here reads or writes it.
- **Safety incidents** are never read or written by this pass. `SafetyIncidentPage` copies its URL
  and `SafetyIncidentHash` has no relationship to a page, so nothing here can reach a case.

### What this pass refuses to do

- It refuses to run at all when `sources.md` cannot be read (`--sources`, default the repository
  root): the file is a keep rule, and an unreadable keep rule must not look like an empty one. Same
  shape as `deactivate_untargeted` refusing with no targets.
- It writes **no audit row per deleted name**. An audit record of a name deletion would be a copy of
  the name. Counts, ids and reason codes go to the report and to one log line.
- It never rewrites the stored text of a page — not a title, not a tag, not `raw_extraction`. That is
  suppression's business, on request, and on safety-clear pages only.

## Consequences

- The three rows that had no period (search queries, the catalogue name, and prospect research)
  become rows with a period, a job and a switch.
- The service privacy notice can describe a job that exists.
- **The periods are the operator's to confirm.** They are settings, defaulted here at 30 / 90 / 90
  days, and the flag is off, so nothing is deleted before he says so.
- Three things stay open and are named rather than quietly left out: `marceline.CreatorMergeLog`
  keeps `source_name` as text and is outside every rule here (its rows do cascade when the *target*
  name is deleted, and the report counts that); `crawler.LLMCall` has its own no-period row (no name
  in it, and the feature is off); and a name that only ever appears inside a page's own text, with no
  catalogue row, is not ours to delete — the page's clock is what reaches it.
