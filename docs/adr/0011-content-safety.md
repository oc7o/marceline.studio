# 0011 — Content safety: text flags now, hash lists later

Status: accepted (2026-09-15)

## Context

The crawler indexes video piracy sites and fetches preview media for PDQ hashing. Some uploads may
show child sexual abuse material or other clearly illegal content. The roadmap requires flagging
and an incident procedure before the crawl is scaled (4 sites, up to 3,000 requests a day each).
Nobody may look at such media to verify it, and the system must stop touching it.

## Decision

- **Text-based flags.** `catalog.SafetyTerm` holds a short, admin-editable list of indicators
  (plain words and age regexes), each with a category and an action. `crawler/safety.py` checks
  the parsed title, tags, performers and extracted text. Matching is case-insensitive, ignores
  punctuation and works on whole words.
- `block` (clear indicators of minors): keep the URL and minimal metadata for reporting. Attach no
  media, cancel queued media, delete stored frame hashes, link no creators and follow no links.
- `review` (ambiguous wording): index the page, but hold its media until a human clears it.
- The media worker enforces the same rule when it claims and stores media (defence in depth).
- Statuses live on `index.Page`. Blocked pages are never unblocked automatically. A reviewer's
  decision stands until the page matches a term it was not reviewed for.
- `safety_scan` applies changed terms retroactively. Operators follow `docs/safety.md`.

**Amendment (2026-09-16, ADR 0018).** The term verdict is no longer the only thing standing between
a page and a media fetch. Pages also get a safety score from their own text, and only clearly safe
pages are fingerprinted; everything else stays text-only. Media discovered on a listing card is
recorded but never fetchable until the detail page itself has been parsed and scored, and the deny
is enforced at the fetch layer rather than by the worker's queue query. See
`docs/adr/0018-media-safety-gate.md`.

**Amendment (2026-09-17, ADR 0018 amendment).** The two actions became four — `review`,
`auto_block`, `auto_block_and_incident`, `customer_release_queue` — with the rule set versioned
and every rule decision logged. See docs/safety.md, "Rule actions".

**Amendment (2026-09-17): a blocked page's hash may stay on its incident.**

The decision above — "delete stored frame hashes" — cost us the ability to recognise our own
finding again. docs/safety.md said so in its own words: a page blocked on first sight brings no
PDQ evidence of its own, so a re-encoded copy of the same material at another URL becomes a second
case and a second report to the hotline. The operator approved closing that gap, in the narrow form set out below.

**Decision.** When a page is blocked by a rule that opens a case, the PDQ hashes it already had
are **moved** onto that case (`index.SafetyIncidentHash`) in the same step that deletes them from
`index.FrameHash`. The deletion is unchanged. `safety_scan` reads them immediately before it
deletes them and hands them to `sync_incidents` (they never touch persistent storage in between);
deduplication case (d) links a newly blocked page whose own hash is within `HASH_MAX_DISTANCE`
(8 bits) of a hash a running case kept, alongside the existing `same_url`, `mirror`, `sha256`,
`cluster` and `term_change` rules.

What makes it narrow, and what each limit is for:

- **Not in `index.FrameHash`.** Every matcher loads that table; a fingerprint of material we
  refuse to touch is not evidence that anything was copied, and it must not be able to become a
  customer-visible finding. It lives on the incident, `matching` does not know the table exists
  (a test walks `matching/` and fails if any module names it), and the Hamming arithmetic is a
  local function because `index` may not import `matching` in the first place.
- **One purpose.** Deciding whether a newly blocked page is the same material as an existing case,
  so one case still produces one report. There is no other read, no admin listing and no API
  field.
- **Nothing new is fetched, ever.** Only a hash computed while the page was still clear and above
  the gate score can be retained. Where none exists, the case has none — blocked pages fetch
  nothing, and hashing is never re-enabled for one.
- **Only cases.** Pages blocked by `auto_block` or `customer_release_queue` rules open no hotline
  case, so nothing about them is retained.
- **The incident's retention, not a new one.** The rows cascade with the case `safety_retention`
  deletes (open, then twelve months after closure). At most 16 hashes per case: a handful of the
  sharpest pictures answers the one question as well as dozens would.
- **8 bits, not matching's 31.** A wrong link here would fold two distinct findings into one case
  and silence a report; only a re-encode or a re-upload of the same picture gets that close.

None of this adds a known-CSAM hash list (ADR 0011, "Later"): the one
free programme's published SDKs transmit the media itself, which our hard rule forbids. The first
question to any such provider stays whether a hash-only endpoint exists.

## Consequences

- Words give only weak evidence. Uploads with innocuous titles are missed, and ambiguous titles
  cause review work. The list stays small to keep the review queue manageable.
- Review pages lose time on sites with expiring preview links; a re-crawl fetches fresh links.
- Existing duplicate pairs, clusters and reference matches of a newly blocked page are not
  removed automatically. Its hashes are deleted, so it drops out of new matching runs (the copy
  the 2026-09-17 amendment keeps on the incident is invisible to matching).

## Later: hash-list matching

The proper long-term control is matching against a list of hashes of known CSAM, e.g. from
an industry hash-sharing programme (NCMEC, IWF Hash List, or a provider such as Thorn Safer). Such
a list would be checked before any hash is kept or compared. Access requires an agreement and
vetting, and the formats (PhotoDNA, MD5, PDQ) may need a licence. Pursue this before production.
Until then, text flags are the only automated control.
