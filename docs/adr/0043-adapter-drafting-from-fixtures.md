# 0043 — Adapter drafting from recorded fixtures, and why the model stays out of it

Status: accepted (2026-09-18). Issue #21. Re-scopes it. Stands beside ADR 0007 (hand-written
adapters first, unchanged) and ADR 0042 (LLM *repair* of a broken adapter, unchanged); refines the
"evolution" line of ADR 0006.

## Context

Issue #21 asked for LLM-drafted adapters for approved non-KVS candidates: a model reads a new
site's pages and writes the parser, a human reviews it before the site is enabled. ADR 0042 closed
the sibling case (a model filling four text fields when an existing adapter has gone blind) and
explicitly left this one open as "a build-time, human-reviewed authoring workflow".

Two things have changed since #21 was written.

**Who writes adapters now.** All seventeen adapters in `crawler/sites/` were written by a coding
agent working in this repository (`git log --diff-filter=A -- backend/crawler/sites/`: every one
carries `Co-Authored-By: Claude`). That agent sees the `SiteAdapter` protocol, `sites/common.py`,
sixteen worked examples, the fixture-test conventions and the recon note it wrote itself. A
drafting call to a paid API would hand a weaker, repo-blind model 12,000 characters of one page
and ask for the same artefact. The expensive part of onboarding is not typing the parser.

**What the expensive part actually is.** Reading the two most recent onboardings
(`486afdc`, 3,237 lines for one bespoke adapter and 2,500 for a KVS theme option) and the two
sites that have recorded fixtures but no adapter (`docs/sources/site24.example.md`,
`docs/sources/site23.example.md`), the work is: record fixtures politely, decide *which engine
the site runs* (a KVS theme is a twelve-line `KvsSite` entry; a bespoke site is 200 lines), find
the URL grammar (detail, model, search, pagination), find where title, duration, upload date and
credits actually live, decide the removal marker, then write policy, budgets, robots and ToS notes.
Of that list, everything up to "then write policy" is **deterministic analysis of HTML we already
have on disk**. No model is needed to see that a page carries a JSON-LD `VideoObject`, that
twenty-four card anchors share one path template, or that `kt_player` appears in the markup.

## The trust boundary this has to respect

ADR 0042 forbids a URL-shaped field in the repair schema, because *an extracted URL is a fetch
instruction written by the page*. Drafting an adapter is about nothing but URL grammar and
selectors, so the rule cannot be carried over unchanged; it has to be restated at the level that
makes it true:

> **Nothing derived from a page's markup may reach the fetch queue without a human commit in
> between.** Repair's URL ban is the run-time instance of that rule — there is no human between a
> crawl and the next fetch, so the model may not name a URL. Drafting is the build-time instance:
> a path template proposed here is a *hypothesis about a site's URL grammar*, printed to a
> terminal, read by a person, and only ever executed after it has been written into a registered
> adapter, reviewed and merged. It is also bounded twice over by things that are not the draft:
> the domain allowlist (`catalog.Site`) and the worker's on-site link filter, which enqueue
> nothing off the task's own site whatever an adapter returns.

That boundary is what makes a drafting tool admissible at all, and it is the reason the tool below
**never fetches anything** and **writes nothing the crawler loads**: its only output is text on
stdout. A generated adapter that the crawler could load from the database would put a machine's
guess back inside the run-time path, and the crawler's policy is enforced in code, not in data.

## Decision

**One: the drafting step is offline fixture analysis, and it needs no model.**

`manage.py draft_adapter --fixtures backend/crawler/tests/fixtures/<domain>` (pure module
`crawler/drafting.py`) reads recorded HTML from disk and prints a reviewable draft:

- the **engine fingerprint** (KVS / WordPress / bespoke) with the markers that decided it, because
  "this is a KVS theme" is the single highest-value fact of an onboarding;
- a **`KvsSite(...)` snippet** when the fingerprint says KVS — for those sites the draft *is* the
  adapter, and the reviewer's job is to check it against the fixtures;
- per fixture, what kind of page it is (detail / listing / missing / unknown) and why;
- the **link families**: same-site anchors grouped into path templates with a suggested
  `video_path`-style regex, how many carry visible card text (`card_text`, ADR 0029 amendment) and
  which image attributes their cards use;
- the **structured data** available (JSON-LD `@type`s and which `VideoObject` keys are present,
  microdata `itemprop`s, the og/`video:` meta keys) and, for title, duration, upload date and
  credits, the **candidate selectors** — grouped by which of them agree with each other;
- the **search form** (action, `data-url`, input names) as a `search_path` proposal, the
  **pagination** shape, and the removal marker on the `missing` fixture;
- the checklist from `CLAUDE.md` "Adding a site adapter" with these facts filled in.

**Two: it prints structure, never content.** Selectors, attribute names, path templates, counts,
lengths and agreement groups — never an extracted title, a performer name, a tag or a full URL of a
page. That is the "findings name identifiers, never content" rule applied to a tool, and it is also
the better design: the draft is about where a field lives, and the value itself is in the fixture
file the reviewer has open. Removal markers are the one printed string, and they come from a marker
list **we** own (`deleted`, `not found`, …), not from the page.

**Three: no LLM drafting call is built.** Not "later behind a flag": not built. The interface it
would use exists (`crawler/llm.py`, `LLMCall`), and if the deterministic draft ever proves
insufficient the addition is small and constrained in advance — a new `LLMCall.Purpose`, a schema
whose fields are **selectors and path templates only**, output printed for review and never stored,
and the same per-call cap and logging as repair. The condition for revisiting is an honest one and
can be measured: *a bespoke onboarding where the draft above left the agent guessing*. Until then a
paid call would buy a worse version of what the reviewing agent already produces, and every
capability that exists has to be maintained, tested and reasoned about in every later security
review.

**Four: no generated fixture test.** A test whose assertions were written by the thing under test
asserts nothing. The fixture test stays hand-written (`crawler/tests/test_<site>.py`), which is
also what makes the review real: it is where a human states what the page *says*, and the draft
above deliberately never states that.

## Consequences

- Issue #21's acceptance criteria are met by a different mechanism than it imagined: the workflow
  from approved candidate to drafted adapter exists (`probe_candidates` → approve → record fixtures
  → `draft_adapter` → write and register the adapter → fixture test → policy, budgets, disabled
  `Site`), human review before enabling a site is unchanged and still the only path, and
  "choose a model with the `claude-api` skill" is moot — ADR 0042 already settled the provider
  (OpenRouter) and deliberately pins no model, and this ADR calls no model at all.
- The tool cannot onboard a site that needs JavaScript, and it says nothing about robots.txt, ToS,
  budgets or safety: those are decisions, and decisions stay with the operator.
- Its heuristics will be wrong sometimes. That is affordable precisely because its output is a
  draft read by a person with the fixtures open, and never a thing the crawler executes.
- Adding an engine fingerprint (a second CMS as widespread as KVS) is now a small, testable change
  in one pure module instead of tribal knowledge in a recon note.
