# 0051 — Source kinds and coverage verdicts

Status: accepted (2026-09-18). Issue #118. Extends ADR 0013 (source discovery with human approval);
applies ADR 0007's crawler policy to the *candidate* stage; does not touch ADR 0029 or ADR 0038.

## Context

The primary audience changed (operator, 2026-09-18): independent creators on subscription platforms
rather than studios. The eighteen enabled sites are video-sharing sites full of studio *videos*; a
creator's paid posts circulate as pictures and short clips, in places organised around a **name**,
around a **thread**, or around a **link to a file host**. The typology and the evidence behind it
are in `docs/research/creator-leak-sources.md`.

Discovery's existing shape assumed one question — "is this a video site?" — answered by
`SourceCandidate.kind` (`tube` / `file_hoster` / `torrent` / `social` / `unknown`) and by
`video_links_seen`. That question is now too narrow in one direction and too permissive in another:

- too narrow, because a name-organised re-upload site or a picture-set site has no video grammar at
  all and lands as `unknown`, which reads as "somebody look at this" for something we may well
  want;
- too permissive, because a **leak forum** lands as `unknown` too — and a forum is a place our own
  hard rules forbid us to cover at all, since its content sits behind registration and we hold no
  account anywhere. Nothing stopped an operator from approving one at 2 a.m. and creating a `Site`
  for it; the refusal only happened later, in a person's head, or not at all.

ADR 0013's probe already records what would answer both: a login wall, a CAPTCHA or challenge, an
age gate and its two kinds, the robots.txt verdict, a CMS guess. The information was there and
nothing read it.

## Decision

**One: a candidate carries a *coverage verdict*, not only a kind.**
`crawler/discovery/kinds.py` (pure) defines four outcomes plus `unknown`:

- `coverable` — nothing recorded forbids an adapter. **Not** an approval: onboarding is unchanged
  (recon note, robots/ToS/budget decision, a disabled `Site`, a human enabling it).
- `link_intelligence` — we may record that an indexed page points there; we never fetch what it
  serves, never follow a timer or a captcha, never resolve a shortener. File hosts and torrent/P2P
  sources, which ADR 0013's amendments already treated this way, now say so in one word.
- `not_coverable` — a login, a CAPTCHA/challenge, a paywall, an age *verification* gate or a
  robots.txt disallow stands in front of it. Every one of these is something ADR 0007 forbids
  working around, so the verdict is a restatement of an existing rule at a new place, not a new
  rule.
- `parked` — held back for a legal reason rather than a technical one (messaging-app channels,
  issue #60).

**Two: the verdict is computed from what the bounded probe already recorded, and nothing else.**
No extra request, ever. Three counters are added to the *same* homepage the probe already fetched
(`crawler/discovery/structure.py`): links into a person's own page, links into a thread or board,
and the biggest family of same-shaped pictures. They are integers; no URL, name or title is stored.
This keeps ADR 0013's "at most three logical requests per candidate" exactly as it was.

**Three: a forbidden candidate cannot reach onboarding, and the database says so.**
A `CheckConstraint` on `SourceCandidate` refuses `status = approved` together with
`coverage in (not_coverable, parked)`. The admin's *Approve* action skips such a row with a message
naming the reason. The constraint is the guard and the message is the courtesy — the same shape as
the policy-audit trigger of issue #106, and for the same reason: the thing that must not happen is
enforced where every writer passes, not where a well-behaved one does.

**Four: coverage is only written while the row is unreviewed.** An `approved` candidate keeps the
operator's decision — the classifier's own rules will change, and a rule change must never
contradict a human's recorded call (it also cannot, by the constraint above). Re-running
`discover_sources --reapply-rules` re-decides coverage for unreviewed rows and reports each change.

**Five: creator-source query templates exist, and they run for targets only.**
Two template sets (English and German) ask for the vocabulary of these places — picture sets, packs,
folders, threads — instead of for a studio video. They are marked `targets_only`: `plan_queries`
**refuses** to plan them without the caller's verified list of target creator names, and no creator
outside that list is planned. The list can only come from `marceline.targets`, which `crawler` may
not import (module boundaries), so the entry point is a command in the project package,
`discover_creator_sources`, which **plans and prints and makes no request**. Prospects are excluded
by name: research before opt-in must not spend a search on somebody's name in a leak vocabulary.

The existing query-safety refusal is untouched and applies to every new template; a test asserts
each one against the live term list. The spend guard, the run cap and `DISCOVERY_SCHEDULE_ENABLED`
(off) are untouched. There is deliberately **no paid path** for these templates yet:
`discover_sources` cannot select a `targets_only` market, because it has no way to prove a name is a
target. Wiring one is the operator's decision, at about $0.024 per target creator per full pass.

## Consequences

- Four kinds join `SourceCandidate.Kind` (`name_index`, `gallery`; `forum` existed and is now
  actually set). One migration, `crawler/0009`.
- A probe of a forum-shaped or walled domain now ends in a verdict instead of a human review round.
  The classifier will be wrong sometimes — it judges one homepage — and it is wrong **towards
  `unknown`**, which costs a look, never towards `coverable`.
- `link_intelligence` gives the file-host and torrent decisions one word, so the next question about
  them ("who do we notice?") is visibly a provider-directory question (ADR 0027), not a crawl
  question.
- The forum verdict is a real product limit, and the founder must be able to say it out loud: we do
  not watch registration-walled boards, because watching them would mean holding accounts on them.
  `docs/research/creator-leak-sources.md` § 6 is the sheet that says which limits we accept.
- Nothing here enables a site, spends anything or changes what is crawled today.
