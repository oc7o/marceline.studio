# 0049 — A site's catalogue size, and our share of it as a monitored number

Status: accepted (2026-09-18)

Issue #107. Amends ADR 0029 (targeted indexing) in
one respect only: what targeted indexing achieves is now measured against the site's own catalogue
instead of being asserted.

## Context

How much of a site's catalogue we index is a question about a **share**, and a share needs a
denominator.

On 2026-09-18 one could be computed for **two of eighteen** enabled sites:
9.0 % and 14.6 % of the catalogue ever indexed, 0.6 % and 2.8 % active. One figure came from a
sitemap counted during reconnaissance, the other from a number the site prints on its own pages.
For the other sixteen the denominator was unknown, so every sentence about "a small share of what
those sites publish" rested on two data points.

Three estimation methods were tried and failed. They are recorded here so nobody
repeats them: a URL-id span is useless (ids are global or non-sequential — one site shows a maximum
id of 1.9 million against 10,160 pages held); the recorded fixtures carry per-tag and per-search
counts, not catalogue totals; stored listing pagination bounds a walk we ran, not the catalogue.

The issue proposed closing the gap with "one request per site". This ADR does not do that.

## Decision

### A per-site record of the site's own total, with its provenance

`catalog.SiteCatalogueSize`, one row per site: the total, the **method** it was obtained by
(`stated_on_site`, `sitemap_count`, `listing_pages_x_page_size`, `manual`), where it was read as a
**path shape** (`/* (header meta)`, `/sitemap.xml`, the name of a reconnaissance note — never a URL
and never an identifier), when it was last seen, a note on what it is worth, and who recorded it.

The method is stored because the figures are not equally good: "the site says 66,758" and "we
counted its sitemap" are both denominators, but only one of them was counted by us.
`catalog.catalogue_size.METHOD_RANK` makes that operational — a figure may only be replaced by one
of equal or better standing, so an automatic capture can never overwrite what a person entered.

### No new request type, ever

A figure reaches the record two ways and no other:

1. **From a page the crawler was going to fetch anyway.** `SiteAdapter` gains one optional method,
   `catalogue_total(kind, url, html) -> CatalogueTotal | None`, in the same shape as the existing
   `card_text` capability: not part of the protocol, absent on every adapter that does not declare
   it, and pure — it may read only the HTML it was handed. `crawler.catalogue_totals.record` is
   called by the worker after each fetch and is best effort in both directions: a figure is never
   worth losing a page over. One adapter implements it today (the one site whose fixtures state a
   total; the number appears in its page furniture on every page kind).
2. **From a person**, `manage.py catalogue_size --record`, which is how the sitemap figure already
   sitting in a reconnaissance note gets in without anyone fetching a sitemap.

No sitemap fetch, no last-page probe, no new request kind, no budget consequence. A site that
states nothing has no row, and unknown is reported as unknown rather than as zero.

### Both shares, always

`index.catalogue_share` counts the numerator — `catalog` may not import `index`, so the counts are
stamped into the record with a `counted_at` and read from there by the Site admin, `request_report`
and `make status`. Two numbers per site, always both:

* **ever indexed** — every page of the site ever stored. A page we deactivated and scrubbed was
  still fetched once.
* **active** — what is still live under targeted indexing (ADR 0029). On 2026-09-18, 58 % of
  everything ever indexed was already deactivated and scrubbed.

Reporting only the second would be flattering; reporting only the first would be pessimistic.

### The threshold monitors. It never pauses.

`CATALOGUE_SHARE_ALERT_PERCENT`, default **25**, compared against the **ever indexed** share. A
site that crosses it produces one mail to the operator (through the existing `SAFETY_ALERT_TO` pair
— one operator, one mailbox) and one `logger.error`, and **nothing else happens**: no site is
paused, disabled, slowed down or dropped, and no crawl behaves differently. `notified_at` is
cleared when the share falls back under, so one crossing is one mail.

Why 25:

* It is above both shares ever measured (9.0 %, 14.6 %) by a clear margin, so it cannot fire on
  today's practice and a crossing means something actually changed.
* It is far below "most of the catalogue".
* It is deliberately **not 10 %**: a setting at a round number would be read as a safe harbour by
  the next person to find it, and no such line exists.

It is a placeholder whose only job is to make a change visible; 0 switches the alert off.

## Consequences

* The share can be re-measured instead of asserted, and
  the honest "unknown for N of M sites" is now a number the system prints rather than a sentence
  someone has to remember to update.
* A second optional adapter capability exists. It is the same pattern as `card_text` and carries
  the same rule: an adapter is pure and never requests anything.
* An automatic capture cannot silently degrade a good figure, and a person can always correct one
  (admin, or `--record` with `--method manual`).
* The threshold is one more operational number that can be wrong. It is wrong quietly in both
  directions, which is why it is pinned in `infra/compose.yaml` and in
  `marceline/tests/test_compose_env.py` rather than left to `.env`.

## Alternatives considered

* **One request per site to a sitemap or a last listing page** (the issue's own proposal). Rejected
  for now: it is a new request kind against sites we mean to touch lightly, for a number
  that changes slowly, and the two-source route above already produces a real denominator for every
  site that publishes one. It stays available if full coverage is ever needed.
* **A live share computed on read.** Rejected: the Site admin and `request_report` live in
  `catalog`, which may not count pages, and a boundary is not worth bending for a number that
  changes daily at most. The stamped counts carry `counted_at`, so nobody can mistake them for live.
* **Pausing a site over the threshold.** Rejected: the threshold is a guess,
  and a guess must not be able to stop a customer's work.
