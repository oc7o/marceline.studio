# 0006 — LLM inside the crawler, policy enforced in code

Status: accepted (2026-09-14); rollout order amended by ADR 0007 — hand-written adapters come
first, the LLM follows for new sites and broken adapters. The policy rules below still apply.

## Context
Each piracy site has a different layout and changes often. An LLM can extract structured data and
decide which links to follow without hand-written parsers. But pages are untrusted (prompt
injection), LLM output is non-deterministic, per-page calls cost money, and some actions
(logins, CAPTCHA bypass) are off-limits legally.

## Decision
- The LLM receives cleaned HTML/text only — never images.
- Its output must validate against a Pydantic schema (page type, fields, classified links, next
  URLs); invalid output is retried or fails the task, never used raw.
- Code enforces policy regardless of LLM output: domain allowlist (`catalog.Site`), per-domain
  rate limit, crawl budget, no login / CAPTCHA / paywall / age-gate bypass.
- Provider is behind a small interface (`crawler/llm.py`), Claude API via the Anthropic SDK.
- Every call is logged in `crawler.LLMCall` (model, tokens, cost, input hash, output, decision).
- Evolution: LLM per page now → LLM-generated per-site extraction rules stored on `Site`, with the
  LLM called only when rules fail.

## Consequences
- Fast start without parsers; cost is visible per call.
- Tests mock the LLM and use recorded HTML fixtures.
- As volume grows, the LLM moves out of the hot path.
