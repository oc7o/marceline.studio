# 0013 — Source discovery with human approval

Status: accepted (2026-09-15).

## Decision

Google Organic SERP through DataForSEO v3 is the first discovery backend: one small API client
supports immediate live results and cheaper asynchronous standard tasks. This supersedes the
planned Brave integration for discovery; Bing can later use the same client structure and audit
model. No matching imports: reference titles can be supplied as repeatable free-text queries.

Creator names and aliases expand into three named query templates. Organic results and indexed
Page external links become SourceCandidates, never CrawlTasks. Existing Sites and a short named
platform ignore list are excluded. Offline hoster/social rules mark candidates hoster/ignored. The short ignore list also excludes
LinkedIn and creator reference databases. Matching domain
labels across TLDs are conservatively treated as mirrors of existing Sites; redirects to existing
Sites also mark candidates ignored with a mirror note and evidence.

Issue #18: three further ignore groups, each domain carrying its own specific reason (stored on
the candidate as its note, instead of the list's older generic note) so a candidate shows *why*
it was ignored — public broadcaster media libraries (DACH catch-up TV, e.g. ARD Mediathek, ZDF
Mediathek, arte, 3sat, ORF TVthek, SRF Play), legal streaming guides/aggregators (werstreamt.es,
JustWatch) and other leak-protection services. None of these are leak sources, so — like the rest of the ignore list —
they are excluded before a candidate row is ever created, and `--reapply-rules` reclassifies any
row already proposed for one of them.
Review decisions survive further evidence. Evidence samples are capped at 20; relational creator
and page memberships preserve exact deduplication without an unbounded evidence JSON document.

Normalization uses IDNA, lowercase, drops www and all other subdomains, and retains the last two
labels, or three for a small explicit multipart suffix list (co.uk, com.au, co.nz, co.jp, com.br,
co.in). This is deliberately not a full public suffix implementation: uncommon suffixes and shared
hosting tenants can merge incorrectly. Review before approval; extend the list as needed. No suffix
downloads or DNS requests are made. IP literals and malformed/non-HTTP URLs are excluded.

## Spend and API behavior

Credentials are optional environment settings; paid commands fail clearly when absent. Every API
HTTP request (including ready polls, retrieval and errors) has a SearchCall with the response's
top-level cost, not a fabricated billed price. Transport failures record zero reported cost.
Errors retain bounded, credential-redacted provider codes and messages (or HTTP status / transport exception class); account errors (401xx/402xx) stop discovery while other provider errors allow the next query.
Code 40101 (transient/auth error) is retried in place with a bounded, short backoff (two retries,
patchable `time.sleep`) before giving up; each attempt is an ordinary call through the same spend
guard and call cap, so a retry can never exceed the run's budget. Exhausting the retries still
stops discovery like any other 401xx/402xx code.
Only this API module may use httpx directly; crawling still uses core.http.PoliteClient.

A Postgres transaction advisory lock serializes the rolling-24-hour spend check and each request.
The next paid request is estimated at $0.002 live / $0.0006 standard for depth 10. These are named,
conservative planning constants, not a price guarantee; verify pricing before production. Calls
stop before estimated spend exceeds the configured $0.25 daily budget or 60-request run cap.
Actual response costs feed the next check. A provider charge above its estimate can overshoot;
a process crash after billing but before commit can lose an audit record. This is a v0 local guard,
not a provider-enforced spending limit.

Standard mode posts, polls ready at most ten times, then gets regular results; free poll/get
requests still count toward the run cap. A not-ready task is reported with its ID; no background
worker or automatic retry is started. Dry run shows the query plan and estimated cost with no API
requests or database writes. Its estimate uses the configured budget, not remaining historical
spend, and assumes one ready poll; actual execution checks history before every request.

## Probe exception and human review

Non-allowlisted domains may be probed only while proposed and kind unknown/tube: at most
`MAX_PROBE_REQUESTS = 3` logical requests per invocation/domain: robots.txt, then homepage if
robots allows /, plus one robots.txt for a homepage redirect host (below). Each may follow at most
`MAX_REDIRECT_HOPS = 3` redirects within the normalized registrable domain (hops of a resumed
homepage request count together).
Every hop uses PoliteClient with at least five seconds per-host delay; homepage hops also honor
crawl-delay and disallowed paths. No links, search, media or automatic retries. Cross-domain
redirects stop before fetching the destination; the latest target is stored in evidence.
Robots redirects/errors fail closed when unresolved; robots 4xx otherwise follow the existing
allow-all policy. The homepage starts at the final robots origin. A homepage redirect to another
origin within the same registrable domain (apex/www to www or any other subdomain, or a scheme
change) pauses the homepage request for the one extra robots.txt request on the target origin;
its rules apply to that origin (RFC 9309 initial authority), and the homepage request continues
there only if they allow the target, with crawl-delay honored. A second origin change stops with
"redirect requires unchecked host robots". Redirects to another registrable domain still stop,
recording the target. First live run (2026-09-15): site05.example, site25.example, site18.example,
one further candidate and site22.example stopped on apex → www homepage redirects; `--retry-failed` selects
them. Challenges/login/age gates stop processing. The project
User-Agent and size/time limits come through PoliteClient. A scoped compatibility shim disables
its internal client's redirects so discovery can validate and pace every hop.

`probe_candidates --retry-failed` selects previously probed, still-proposed unknown/tube
candidates with redirect failure markers, including legacy 3xx results with empty reasons or
"robots unavailable". Unrelated failures and human review decisions are left alone.

Ignore, hoster/social and mirror rules run when a candidate is created or probed, so rule changes
or new Sites leave older rows proposed. `discover_sources --reapply-rules [--dry-run]` is offline
(no API or site requests) and re-evaluates these rules, in that order, for `proposed` candidates
only, under a row lock; approved, rejected, hoster and ignored rows are never changed.
Its first dry run (2026-09-15, read-only) would ignore one reference-database domain (ignore list) and site03.example
(mirror of site03.example) out of 55 proposed candidates.

Homepage inspection stores text metadata and counts only. KVS/WordPress markers, search forms
and video-like links are heuristic, not proof. Re-running the explicit probe command can re-probe
a candidate; it is not scheduled.

Admin evidence is escaped text only: no images, iframes or external resource loads. Approval is
an explicit admin action creating a disabled Site with an audit note if absent. Existing Sites
are not modified. Enabling a Site and implementing/registering its adapter remain separate human
onboarding steps. Reject and mark-hoster are explicit alternatives.

## Telegram channel candidates: stored, never crawled

Telegram crawling is "not yet" (`docs/research/telegram.md`: after M2 and legal review, then only
public `t.me/s/` previews). Its offline first step ships now: `discover_sources --from-links`
also parses `t.me/<channel>`, `telegram.me/<channel>` and `t.me/s/<channel>` links from stored
`Page.raw_extraction["external_links"]` into `TelegramChannelCandidate` rows. The `t.me` domain
row keeps its social/ignored SourceCandidate as before. No Telegram request is made, now or on
approval; no CrawlTask, Site or adapter exists for channels.

A separate model instead of a `source_type` on SourceCandidate: SourceCandidate is keyed by domain
and carries probe fields that `probe_candidates` acts on (robots, homepage, CMS), and its approval
creates a Site. A separate table keeps channels out of probe selection and Site approval by
construction. Its key is unique (`private_invite`, `identity`).

Parsing (`crawler/discovery/telegram.py`, pure): hosts `t.me`, `telegram.me` (+ `www.`);
usernames are lowercased, query strings and fragments dropped, `/<channel>/<id>` post ids kept
only as capped sample evidence. Usernames must look valid (letter first, 4–32 of `a-z0-9_`).
Bots (usernames ending in `bot`) and the documented `RESERVED_PATHS` (e.g. `share`, `iv`,
`addstickers`, `addlist`, `proxy`, `c`, the official `telegram` channel) are ignored.
`<name>.t.me` subdomains and `tg://` links are not parsed. Private invite links (`t.me/+…`,
`t.me/joinchat/…`) are stored only as the SHA-256 of the case-sensitive invite code, created
`ignored` with an off-limits note, and a database constraint forbids approving them. Links on
safety-blocked pages are skipped (`docs/safety.md`: no links from a blocked page are followed).

Evidence is recomputed from a `TelegramChannelPage` relation on every run, so reruns are
idempotent: linking pages, distinct creators and sites of those pages, capped (20) site domains,
newest linking page URLs and highest post ids, first/last seen. Status is
proposed/ignored/approved/rejected with a note; mining never changes a review decision. The admin
list is text only, sortable by linking pages and distinct creators, filterable by status and
private invite; a public channel is shown as a plain link, never embedded.

## Amendment 2026-09-17: markets, query safety, desk-research candidates (issue #24)

**Markets.** A `Market` (`crawler/discovery/services.py`) bundles a search location, a language and
the per-name templates, plus optional generic queries. `default` keeps the configured
`DISCOVERY_LOCATION_CODE` / `DISCOVERY_LANGUAGE` and the three English templates; `de` searches
Google Germany (2276) in German with the templates of `docs/research/german-market.md` §4.
`discover_sources --market de [--generic]` selects it per run instead of editing the environment;
the client sends and logs that run's location and language on every `SearchCall`. Spend guard, run
cap and dry run are unchanged; the dry run now also names the market and how many planned queries
the cap or budget left out, with their estimated cost.

**Query safety.** Discovery never searches for wording that points towards minors or
non-consensual content. Before any call (dry run included) every planned query — templates,
`--query` text and generic queries — is checked against the enabled `SafetyTerm`s, the media
gate's minor near-miss signals (`AGE_PATTERN`, `MINOR_CONTEXT`; also on the umlaut-folded
spelling) and a short list of German non-consent words the term list does not cover; one unsafe
query refuses the whole run with the query and reason. Refusing rather than skipping keeps the
operator in the loop.

**Desk-research candidates.** `discover_sources --add-domain DOMAIN --note TEXT` records a domain
found by desk research (traffic rankings, court reports, press) as a `SourceCandidate`, offline:
no search call, no request to the domain. The ignore, existing-Site and mirror rules apply as for
search results; the note lands in `evidence["research"]` (capped at 20), never as a search hit or
rank, and a reviewed candidate keeps its decision. Probing stays a separate, explicit step.

## Amendment 2026-09-17: torrent/P2P sources, hoster brands, candidate review cleanup
(issues #23, #25)

**Torrent/P2P is its own kind and status, not a hoster.** p2p-site.example (1,149 linking pages from
`--from-links` mining site03.example, by far the largest evidence count of the 2026-09-17 candidate
review) is a torrent/P2P file-sharing site, not a classical embed/file hoster — different
technology (no page we would crawl, no file URL we would fetch — a torrent client and a swarm, not
an HTTP response) and a different legal footing (the site itself typically hosts no infringing
bytes, only .torrent/magnet metadata; who to notice is a separate question from a hoster's abuse
contact). Lumping it into `SourceCandidate.Status.HOSTER` would say "infrastructure, decided,
nothing to do here" about a finding with the single largest evidence count in the batch, and
`ignored` would drop it from view entirely. Neither is honest, so `SourceCandidate` gets a third
finding kind alongside `file_hoster`/`hoster` and the mirror note under `ignored`:
`Kind.TORRENT` / `Status.TORRENT` (`crawler/models.py`), set together by
`crawler.discovery.domains.classify()` via a `TORRENT_LABELS` set (the same shape as
`HOSTER_LABELS`) and by the admin action "Mark torrent/P2P source" (alongside "Mark hoster").
Never given a page adapter, never crawled — the candidate stays visible, tagged as a torrent
source, for a later decision (a magnet-link/notice-to-tracker question, not a crawl question).

**Known embed/file-hoster brands get marked hoster.** The same review's tier of 22 file-hoster/
embed-provider domains (mostly surfaced
by mining site03.example's outbound links) get marked `Status.HOSTER` — the existing mechanism, no new
one. `cuty.io` (a URL shortener) joined `HOSTER_LABELS` in the same bucket as the existing
`bit`/`tinyurl` entries, so a future rotated domain of it auto-classifies without a one-off desk
decision.

**Rule cleanup.** `IGNORE_REASONS` gained domain-specific entries for the review's remaining false
positives: seven off-topic search hits the German-market templates pulled in (mainstream news, a
sports-association site, a Discord directory, a takedown-service blog post, a traffic-analytics
page, a motorcycle-parts shop, a talent agency's results page — none hosting or linking a video),
four third-party Telegram channel directories (not `t.me` itself — Telegram crawling is its own
"not yet" track, `docs/research/telegram.md`), two paste services (link-list hosting, no
video/performer content) and one file-hoster typosquat (a transposed-letter clone
of a known hoster's domain). `discover_sources --reapply-rules` reclassifies every one of them, plus
`justwatch.com` (already ignored since the issue #18 amendment, just not yet re-swept).

**A candidate whose domain already exists as a `Site` is onboarded, not a mirror of itself**
(operator decision, same day). `apply_rules()` called `mirror_domain(candidate.domain)`, whose
exact-match branch fires identically whether that domain is a genuinely different mirror or the
candidate's *own* domain — the latter meaning it went proposed -> approved -> built already and
this row was simply never closed out, exactly `site12.example`'s bookkeeping gap (its Site is
approved and built but disabled — `Site.enabled` never enters the
check, only that the row exists). Misreading that as "ignored, mirror of itself" would silently
destroy a fact (this *is* a real, working site) as if it were noise. `SourceCandidate` gets
`Status.ONBOARDED`; `crawler/discovery/services.py` adds `existing_site_domain()` (exact match
only) and `mark_onboarded()`, checked in `apply_rules()` before `mirror_domain()` — which is
otherwise unchanged, still needed for a genuinely different domain (e.g. a probe's redirect
target, `probe.py`). Note text: `"already onboarded as <domain>"`.
