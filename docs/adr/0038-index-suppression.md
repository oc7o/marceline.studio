# 0038 — Index suppression: a durable "do not index this name" marker

Status: accepted (2026-09-17)

A person who is not a customer can ask not to be indexed. Before this change the product had no
route for that request and no suppression list, so honouring an objection would have meant a
hand-written database change that the next `crawl_seed` could undo. Process: `docs/suppression.md`.

## Context

Most people named in our index are not our customers. They have no account, and under GDPR
Art. 21 any of them may object to being indexed. Before this change:

- there was no marker at all. Removing someone meant deleting `Page.creators` rows by hand;
- **the next crawl undid it.** `crawler.services.store_parse_result` links a creator whenever the
  name appears in a page's title, tags or performer credits, and it re-crawls the same page for
  other reasons — so the association came back within a day, and `record_page_links` rebuilt the
  breadcrumb with it;
- seeding (`targets()`), source discovery (`plan_queries`) and prospect research
  (`prospect_report`) would each have kept using the name independently;
- nothing said what we answer a requester, how we check who a stage name belongs to, or what we
  record.

The exclusion this needed already had a shape in the codebase: `Creator.needs_review` (issue #29)
is a name that must not be hunted, linked or searched for until staff look at it, and every reading
path filters it.

## Decision

### The marker is a small dedicated model attached to the creator

`catalog.CreatorSuppression`: one row per suppressed name, `OneToOneField` to `catalog.Creator`
(`related_name="suppression"`, **`PROTECT`**), the same shape as `catalog.WatchlistEntry`. Active
while `lifted_at` is NULL. It holds the basis (`objection` / `erasure` / `operator` / `other`), the
request and answer dates, an `identity_check` **code** with a one-line note, who decided, and the
counts of what was removed.

Why a row and not a `Creator` flag: a suppression is an event with dates, a basis, a verification
record and possibly a lift, and it must outlive the data it removed. Why keyed to the creator and
not to a free-standing name: the creator row already *is* our record of a name and its aliases, and
keying the marker to it makes every enforcement point a single join instead of a string comparison.
A name we hold nothing under gets a `Creator` row (with the requester's spellings as aliases) purely
to carry the marker — one name in one place, which is exactly the "minimal record kept so we do not
re-add them" that Art. 21 compliance needs.

The marker is kept for as long as we crawl. It is the only thing a suppression deliberately never
deletes.

### Enforcement is a code path every writer passes through, in three layers

1. **`Creator.objects.usable()`** (a manager method in `catalog/models.py`) is the single answer to
   "may we act on this name at all": staff-reviewed *and* not suppressed. Every reading path that
   hunts, links, searches or reports goes through it — `crawler.services.cached_creators` (page
   linking), `marceline.targets.targets` (seeding), `crawler.discovery.services.plan_queries`
   (search), `marceline.safety_rules.release_pages` (relinking after a customer release). Free-text
   paths that bypass the creator list (`crawl_seed --query`, `discover_sources --query`) refuse a
   query that names a suppressed person; `prospect_report` refuses outright rather than returning an
   empty report; the staff creator queue refuses a merge or alias touching a suppressed name,
   because an alias is a new spelling to link.
2. **The breadcrumb writers** (`index/breadcrumbs.py`) filter suppressed creators out of whatever
   ids they are handed, and out of a full rebuild, so a site × creator count cannot come back.
3. **A database trigger** on the page-creator link table
   (`index/migrations/0021_suppressed_creator_links`): `BEFORE INSERT … RETURN NULL` when the
   creator has an active suppression. Silently dropping the row, not raising: the crawler's job is
   to store the page, and it must not fail because one of the performers a page credits is
   suppressed. This is what survives a stale worker, a backfill, an admin action and the next
   feature nobody has written yet — the same reason `billing` and the rights logs use triggers.

`Creator` also becomes undeletable while suppressed (`PROTECT`), so a merge cannot take the marker
with it.

### What a suppression deletes, and what it keeps

Deletes every statement that *this person is on these pages*: `Page.creators` links,
`index.SiteCreatorBreadcrumb` rows, name-bearing entries of `Page.tags` and `Page.raw_extraction`
(performer credits, handles, profile slugs), `index.ProspectSampleCheck` verdicts, the
`catalog.WatchlistEntry`, and the discovery trail (`crawler.CandidateCreator`,
`SourceCandidate.evidence["queries"]` entries, `crawler.SearchCall` rows naming them).

Keeps, because a page credits several people and the rest of the record is not about the requester:
the page row with its URL, site, timestamps, availability and safety record; **`Page.title`**
verbatim (the site's own words, naming the work and often other performers, rewritten by the next
crawl anyway); media rows and PDQ hashes (a
fingerprint of a frame is not a name, and it is what finds another performer's copy on that page);
and `matching.Reference` casts of customer originals, which are contract data and possibly evidence
for that customer's claim.

**Pages are not deleted.** A page that was active only because of this link goes inactive through
the ordinary targeted-indexing rule (ADR 0029): the `linked` keep-rule stops holding and
`deactivate_untargeted --apply` deactivates it.

**A safety-flagged page is off-limits here too** (operator, confirmed 2026-09-17: flagged means
off-limits regardless of who is asking or whether the operator is present). Suppression removes
the association on every page, because deleting a link touches no safety field, term or incident —
but it **never rewrites the stored text of a page that is not safety-clear**, because on such a
page that text is the record `safety_scan` re-judges and a safety report is built from. One
function (`_page_text_changes`) decides which pages' text may be rewritten, the run reports the
flagged count instead (no id, no URL), and nothing on this path adds a link, activates a page,
queues a crawl, changes a safety status or releases media — a lift included. The residue (a name
surviving in a flagged page's tags) is documented in `docs/suppression.md` rather than
resolved by guesswork.

### The command refuses the one case it must not decide

`manage.py suppress_creator --creator NAME [--apply] [--lift --reason TEXT] [--list]`, dry run by
default, idempotent, logging each action, in the style of `assign_originals` and
`relink_flagged_pages`. It prints links, breadcrumbs, stored fields, watchlist entry, prospect
verdicts and discovery rows as counts — never a URL or a title.

It **refuses** to suppress the rights holder of an original an organisation owns: that is a conflict
between one person's objection and another party's claim (Art. 17(3)(e), the contract, the agency
mandate), and it belongs to the operator. Co-performers of owned originals are reported
and left untouched.

Lifting sets `lifted_at` with a required reason and **restores nothing**: the next crawl re-links
what a page credits and rewrites the extraction the suppression redacted. We keep no copy to put
back — keeping one would defeat the deletion.

### Nothing customer-facing changes

Staff and process only: no customer endpoint, no schema, no OpenAPI change. The admin shows the
marker read-only (applying and lifting go through the command, which is where the dry run and the
refusals live) and takes one field, the date the requester was answered.

## Consequences

- Honouring an objection is now one command plus a letter, and it cannot be undone by a crawl.
- One more exclusion rides along with `needs_review`: a new reading path that hunts, links, searches
  or reports on creators must use `Creator.objects.usable()`, and a new writer of page-creator links
  gets the trigger whether it remembers or not.
- `suppress_creator` reads every `Page` row once to find name-bearing extraction. It runs by hand,
  rarely, and reading the table is cheaper than guessing which spellings a site used.
- The open questions — masking titles, answering a verified Art. 15 access request, and an objector
  credited on a customer's original — are listed in `docs/suppression.md` under "What the operator
  must decide" rather than guessed at in code.
