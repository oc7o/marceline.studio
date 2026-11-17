# 0042 — Adapter breakage detection, and LLM extraction as repair

Status: accepted (2026-09-18). Issue #22. Amends ADR 0006 (provider) and stands beside ADR 0007
(hand-written adapters first, which it does not change: the adapter is still the truth).

## Context

Every site has a hand-written adapter (`backend/crawler/sites/<site>.py`). When a site changes its
markup the adapter does not crash — it keeps returning, and what it returns is empty. The failure
mode is a page stored with no title, no duration, no performer credits, no media, or no page at
all. Nothing fails, nothing is logged, and the crawl budget is spent either way. We have found out
late, by noticing a site's numbers looked wrong.

ADR 0006 already allows an LLM in the crawler under strict rules, and its "evolution" line ends at
exactly this case: the LLM called only when the rules fail. This ADR is the two halves of that —
noticing the failure, and what may be done about it.

## Decision — half one: the signal

**A site's adapter is broken when the share of its detail parses that come back *blind* crosses a
threshold. Blind = no page at all, or a page with no title, or a page with no media.**

The three fields were chosen from the data, not from intuition. Measured on the development
database on 2026-09-18, over the 38,110 active, safety-clear pages (inactive pages excluded —
their title is blanked by retention scrubbing, which looks exactly like a break):

| field | how often it is missing today |
|---|---|
| title | **0** of 38,110 (0.00 %), on all 17 sites |
| media | **10** of 38,110 (0.026 %) — 8 on one site, 2 on another |
| duration | 0 % or 100 % *per site*: three sites never publish one |
| performers | 0 % … 100 % per site, and **0.8 % → 16.9 % day to day** on one healthy site |

So title and media are near-binary and comparable across every adapter, while duration and
performers are neither. The day-to-day swing of the performer rate on adapters nobody touched
(site19 0.8 → 16.9 %, site03 0.4 → 10.5 %, site08 10.3 → 22.6 %) is larger than many real
breaks would be. **Duration and performers are counted and reported, never alarmed on.** A
threshold on them would cry wolf several times a week, and an alarm nobody believes is worse than
none.

- `BROKEN_SHARE = 0.30` over a 24 h window, against a measured normal of 0.026 %. Three orders of
  magnitude of headroom, which is what buys the one number the stored pages cannot show: how often
  `parse()` legitimately returns nothing for a fetched detail URL.
- `RECOVERED_SHARE = 0.10` is hysteresis, not a second threshold: a site already called broken has
  to be clearly working again before it is called healthy, so a parser wobbling near the line does
  not mail twice an hour.
- `MIN_PARSES = 25`: below that the verdict is `unknown`, never `ok` — and a site already broken
  stays broken, because a broken parser often means no pages at all and silence must not read as
  recovery.
- Counting happens in the crawl worker, per site and hour, as one `INSERT … ON CONFLICT DO UPDATE`
  (`crawler.AdapterParseStat`), so every per-site worker and every crawl-runtime thread writes the
  same bucket without a lock. **Counters only** — no URL, no title, no page text, ever.
- The verdict is stored (`crawler.AdapterHealth`), reported by `manage.py adapter_health`, computed
  by the scheduler job `adapter_health` every six hours, and mailed to the operator once per break
  plus logged at ERROR for Sentry (`crawler/adapter_alerts.py`). The mail carries the domain, the
  verdict and the counters; never a page, a URL or any extracted text.

## Decision — half two: the repair

**When a site's adapter is *recorded* broken, a model may be asked to read the fields off that
page's text, so data keeps flowing while the parser is fixed by hand.** It is a stopgap with an
expiry date, not a second extraction strategy.

- **Off by default** (`CRAWLER_LLM_REPAIR_ENABLED`). With it off nothing in the crawler ever
  contacts a model.
- **Capped**: `CRAWLER_LLM_REPAIR_DAILY_CALLS_PER_SITE` (default 50) per site per rolling 24 h,
  counted from the `crawler.LLMCall` rows themselves, so a restart, a second worker or a crash
  cannot reset it.
- **Never in a loop**: one call per page, no retry on a refusal, an error or an invalid answer.
  The next attempt can only come from a later crawl of that URL.
- **Text only, never images** (ADR 0006). `clean_page_text` drops scripts, styles and comments,
  renders an allowlist of `<meta>` values and the `<title>` as lines *we* format, strips the
  remaining markup and truncates.
- **Every call is logged as `crawler.LLMCall`** — including refusals and invalid answers — with the
  prompt's sha256, never the prompt, plus tokens and the provider's own cost.

### Prompt injection is contained by construction, not by wording

A pirate page is untrusted input and can carry text addressed to a model. Four layers, all in
`crawler/llm.py`:

1. **Separation.** Every instruction is the `system` message, a constant in that file. Page text
   only ever appears in a `user` message inside a `<page_text>` fence, and the fence token is
   stripped from the content first, so the page cannot close the block and address the model.
2. **Vocabulary.** The Pydantic schema has four fields — title, duration_s, performers, tags — and
   **no field that can express an action**: no URL, no link, no next page, no free-form key. The
   most successful injection imaginable can only change a title, a duration or a name, which is
   the same class of thing the site's own markup could have lied about. *Never add a URL-shaped
   field*: an extracted URL is a fetch instruction written by the page, and the media gate
   (ADR 0018) exists precisely because the crawler decides what it requests.
3. **Validation.** The answer must be JSON and must validate — extra keys are refused, not
   ignored. Anything else fails closed and the adapter's own result is stored unchanged.
4. **Least effect.** Only fields the adapter left empty are filled, so the model never overwrites
   what the parser found; the merged text then goes through `store_parse_result` like any other
   page text, with the same safety terms, score and media gate.

### The data is marked

A repaired page is stored with `index.Page.extraction_source = "llm_repair"` — a column, so a
cleanup finds every such row in one query, and so nobody reads a model's guess as the site's own
words — plus a `raw_extraction["llm_repair"]["fields"]` list naming exactly what the model
supplied. `LLMCall.site` and `LLMCall.purpose` make the spend attributable per site.

### Provider: OpenRouter, amending ADR 0006

ADR 0006 named "Claude API via the Anthropic SDK". The operator's standing preference for
pay-per-use LLM is **OpenRouter**, and it costs no new dependency: `httpx` is already here and no
vendor SDK is. `crawler/llm.py` is the small interface ADR 0006 asked for; swapping the provider is
one module. It is the second documented direct-`httpx` exception after DataForSEO (a paid API with
a credential, not a site being crawled), counted by hand as `kind=llm_api` and never against a
site's crawl budget. No model is pinned in code: picking one would be picking a price.

## Amendment, 2026-09-19 — "no media" is not a signal on every site

Three adapters (`site04`, `site02`, `site07`) parse video pages that carry **no image of
their own**: the player is a script and the cards below it belong to other videos, so every poster
and frame of a page travels with the task in `TaskSpec.media` from the card that linked it. Their
detail parses return `ParseResult.media == ()` *while working perfectly* — which is what their own
fixture tests have always asserted.

The rule above counted that as blind, so on 2026-09-19 two of the three were reported 100 % broken
(14,212 of 14,212 and 2,661 of 2,661 detail parses) and mailed to the operator, while in the same
24 h 11,758 of 11,903 and 2,337 of 2,340 newly seen pages had media and **not one** active,
safety-clear page on either site had none. The third was below `MIN_PARSES` and would have alarmed
as soon as it was busy. Nothing about either site had changed: the measurement behind the rule was
taken over *stored pages*, where the card's media are already attached, and applied to *detail
parses*, where they are not yet.

So the media half of the rule is asked of the adapter first. An adapter whose video pages carry no
media declares `card_media_only = True` — the protocol's **third** optional capability, asked
through `crawler.sites.base.has_card_media_only`, beside `card_text` and `catalogue_total` — and for
such a site **blind means no page or no title**, `no_media` becoming context only, like duration and
performers. The flag is never stored next to the counters: `crawler.adapter_health.evaluate` asks
the registry again, so one site can never be recorded one way and read another. An unregistered
domain answers "media count", the alarming default.

Two consequences beyond the false mail. A site with the flag has a thinner signal — a break in the
*listing* parse that stops the card media would not show here, because ADR 0042 counts detail
parses only; it shows in `media_shapes` and in the media worker's counters instead. And had
`CRAWLER_LLM_REPAIR_ENABLED` been on, every one of those parses was `blind` and therefore eligible:
the repair would have spent its 50 calls a day per site, with real money, on pages nothing was
wrong with.

## Consequences

- A silent parser break becomes an alert within six hours, with the numbers behind it.
- Half one has value on its own and runs with the repair switched off; that is the normal state.
- The repair does **not** restore media: media URLs are never taken from the model, so a repaired
  page carries the adapter's media or none. It keeps titles, durations and credits flowing — what
  creator linking, breadcrumbs and metadata scoring (ADR 0040) need — and nothing more. Restoring
  media is the adapter's job, and the alert is what says to do it.
- `adapter_health` never adds itself: a site with too little traffic reads `unknown`, which is an
  honest answer and not a green light.
- Issue #21 (LLM-drafted adapters for new sites) does **not** overlap: it is a build-time,
  human-reviewed authoring workflow for sites that have no adapter. This is a run-time fallback for
  a site that has one. They share `crawler/llm.py` and `LLMCall`, nothing else.

## Amendment 2026-09-18: the safety terms judge the page text before it may be sent (issue #120)

A review of the first production promotion rehearsal (PR #112) found one blocker against this
ADR. The finding is correct and this amendment answers it in code; the feature stays **off**.

**What was wrong.** The repair runs inside the parse path, *before* `store_parse_result`. The four
conditions in front of it — the feature flag, the stored breakage verdict, a blind parse, the daily
cap — are about spend and confidence, and **none of them is a safety check**. The safety terms
(ADR 0011), the safety score and the media gate (ADR 0018) are applied afterwards, to the merged
text. On the one page shape that triggers a repair (blind: no page, no title or no media) that means
**nothing has looked at the text at all** when up to `MAX_INPUT_CHARS` characters of it are posted to
an external LLM broker. The checklist's own comparator, `discover_sources`, refuses a
planned query in code before any call (ADR 0013 amendment); this path had no equivalent.

**What is checked before transmission**, in `crawler/text_gate.py`, called as the **first statement**
of `crawler.llm.extract_page_fields` — the only function in the crawler that opens a socket to the
provider — so it runs before the prompt is assembled, before the `LLMCall` row exists and before any
client is constructed:

1. **The site is not `enabled`** → no call. A site we have stopped crawling is not a site whose pages
   we pay to have read for us.
2. **This URL is already safety-flagged** from an earlier crawl (`index.Page.safety_status` other
   than `clear`) → no call. The text in hand has not been judged, but the page has.
3. **The text itself** — *exactly* the string that would leave, i.e. the cleaned, truncated excerpt
   that goes inside the `<page_text>` fence, never the HTML it came from — against:
   - today's `catalog.SafetyTerm` rows, **loaded fresh** rather than from the crawl hot path's
     two-minute cache: a control is worth a query;
   - the minor near-miss patterns of `crawler/safety_score.py` (`age_pattern`, `minor_context`);
   - the German non-consent wording `discover_sources` refuses a query for — now one list, in
     `text_gate`, read by both callers;
   - the media gate's own bar, `SAFETY_GATE_MIN_SCORE`. Text we would not fetch a *picture* for is
     text we do not hand to a third party. This is stricter than the query refusal, deliberately: a
     query is 40 characters we wrote, this is 12,000 characters a pirate site wrote.

**Fail closed.** Any exception from the gate — a database error, a broken term regex, anything — is
itself a refusal. There is no branch in which a failing check becomes permission to send.

**What a refusal records.** One `crawler.LLMCall` row per refused repair, so "what did this feature
do to this site" stays one query: `error` is `refused_safety` / `refused_page_flagged` /
`refused_site_disabled` / `refused_check_failed` with the **rule names** that fired (`block_term`,
`minor_context:school`, `low_score`, `forbidden_word`, and the matched terms' *categories*) and
**never a word of the page's text** — not even the matched word, which the query path may print
because its text is ours. `input_sha256` is empty (no prompt was built), `output` is null, no tokens
and no cost are recorded, and `crawler.repair.calls_today` **excludes** such rows: a call that was
never made costs nothing and may not spend anything either. The page then continues through the
ordinary path and is blocked by the ordinary safety pass exactly as before.

**No per-site switch was added.** `CRAWLER_LLM_REPAIR_ENABLED`, the recorded breakage verdict and
`Site.enabled` are the switches; a fourth would be a policy field nobody sets.

**Residual limit.** The excerpt cannot exclude performer names. The repair exists to recover the
fields the adapter lost — above all the **title** — and the title is frequently the place a
performer is named, so removing names would remove the thing the call is for. The gate therefore
reduces *what kind* of text may be sent (nothing that trips the safety terms, the minor near
misses, the non-consent wording or the media gate's score), not *whether a name* may be in it.

**Enabling the feature stays an operator decision.** The gate makes the switch safe to *consider*;
it does not make it a technical question.

Tests: `backend/crawler/tests/test_adapter_repair.py` — a page text that hits a term makes no HTTP
call at all (the transport raises if it is ever reached), the refusal is recorded by rule name and
without the text, the daily cap is not consumed, a gate that raises is a refusal, an already-flagged
page and a disabled site are refused, and clean text still repairs exactly as before.
