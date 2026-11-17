# 0027 — Hoster intelligence: who hosts a leak, whom to complain to, which regime applies

Status: accepted (2026-09-16)

Implements the "what we know about where it sits" part of `docs/customer-ui-plan.md` §3 on top of
ADR 0025 (the case file), which named hoster metadata, jurisdiction and cooperation history as not
built. The operator decided to build it now.

## Context

A finding tells a customer *that* their work is on a page. The next question is always the same:
who can take it down, how do I reach them, and what law makes them act. The answer lives in public
registries — DNS, and the RDAP servers of the five RIRs and the domain registries — and it changes
slowly. Three constraints shape how we may get it:

- **We never request the piracy site or its media for this.** Not a HEAD request, not a
  "harmless" homepage fetch: the crawler has its own policy (ADR 0007) and this feature is not a
  second crawler.
- **No live network in tests**, and registry servers are someone else's infrastructure: one lookup
  per host per refresh window, sequential, identified by the crawler's User-Agent (no contact data).
- The customer case file is **codes, never prose** (ADR 0025), scoped by organisation with the 404
  rule (ADR 0021), and never mentions a safety-flagged page (ADR 0018).

## Decision

### It lives in `catalog`, with the selection in the project package

Per-hostname facts are catalog data: like `Site`, they describe a place on the internet, not a
page. `catalog` may import only `core`, and the lookup needs nothing else — `core.http.PoliteClient`
for RDAP, the standard library for DNS. So:

- `catalog.HostingRecord` (one per hostname), `catalog.HostingProvider` (one per origin AS) and
  `catalog.HosterContact` (one notice to a provider and its outcome); migration
  `catalog/0007_hosting_records`.
- `catalog/hosting/`: `jurisdiction` (pure), `cdn` (pure), `cooperation` (pure), `dns`, `rdap`,
  `lookup` (one hostname, no database), `services` (refresh policy, storing, aggregates).
- `marceline/hosting.py` chooses *which* hosts matter — the site of each customer finding and the
  hosts its previews are served from — because that needs `index` and `matching` together, like
  `marceline.scheduler`. `marceline/app/hosting.py` renders the customer section.

No new app, no new import-linter contract, no new dependency.

### Sources, and where each fact comes from

| Fact | Source |
|---|---|
| addresses (A/AAAA) | system resolver, `socket.getaddrinfo` |
| origin ASN, AS name, allocation country | Team Cymru IP-to-ASN **DNS** service (`origin.asn.cymru.com` TXT) |
| network name/handle, registrant organisation, network country | RDAP IP lookup of the first address |
| abuse contact | RDAP entity with role `abuse` under the network — top level or nested (ARIN nests it in the registrant) |
| registrar, IANA id, registrar abuse contact | RDAP domain lookup of the registrable domain (`abuse` nested in the `registrar` entity) |
| CDN in front | origin ASN (Cloudflare, Fastly, Akamai, BunnyCDN, DDoS-Guard, StackPath), else Cloudflare's published ranges |

The RDAP server is found through IANA's bootstrap files, cached for a day per client. The abuse
contact falls back from the IP network to the registrar, and the record stores **which** it was
(`abuse_source`) and the RDAP URL it was read from (`abuse_source_url`, the response's `self`
link) — a notice has to be able to say where its recipient came from.

The standard library has no TXT query, so `catalog/hosting/dns.py` speaks the handful of RFC 1035
bytes it needs over UDP to the resolvers in `/etc/resolv.conf`. RDAP IP responses carry no origin
AS at most RIRs, and a DNS TXT query is not a request to anyone's website.

Each part fails on its own and leaves a machine-readable code in `errors`; `status` is `complete`,
`partial` (something missing) or `failed` (no address and no registrar). A failed refresh keeps
what an earlier success found.

### Refresh policy

A record is looked up again after `HOSTING_REFRESH_DAYS` (default 30); a failed one after a day.
`lookup_hosting` (`--domain`, `--limit`, `--refresh`, `--dry-run`) looks up hosts of customer
findings first, then every enabled site, and refuses a `--domain` that is neither — it does not
look up arbitrary names. A scheduler job runs it daily with a limit of 25. A dry run makes no
request at all.

### Jurisdiction is derived, not stored

`jurisdiction_of(country)` maps the country of the IP allocation to `eu` (the 27 member states and
their outermost regions and Åland: DSA Art. 16), `us` (the US and its territories: DMCA §512),
`other` or `unknown` (no country, or a registry placeholder such as RIPE's `EU`). It runs at read
time, so correcting the table needs no re-lookup.

**Behind a CDN the class follows the CDN's allocation**, not the hidden origin's. That is the
party a notice can be sent to today, and `cdn` tells the UI that the origin host is not known.

### What a customer sees: a `hosting` section per finding, nothing else

Every `CustomerCaseFindingOut` (`GET /api/v1/app/findings` and `/cases/{id}`) carries
`hosting = {page_host, content_hosts[]}`, each a host with `hostname`, `lookup`, `looked_up_at`,
`stale`, `addresses`, `asn`, `as_name`, `provider`, `country`, `jurisdiction`, `cdn`,
`abuse_contact {email, source, source_url}`, `registrar {name, abuse_email, source_url}` and
`takedown_chance`. A host never looked up is listed with `lookup: not_looked_up` and every fact
null — leaving it out would read as "nobody serves this".

Operator decisions of 2026-09-16 bound this:

1. **Customers see only the providers attached to their own findings, never the portfolio.**
   Hosting providers and CDNs are not secret and may be shown; the list of sites we crawl may not.
   There is no endpoint that lists hosts, providers, records or sites, and a test walks the URL
   configuration to keep it that way.
2. **Exact cooperation statistics stay internal.** *(The band part is superseded by the
   2026-09-17 amendment: customers see a rounded response history instead.)* A customer sees a **takedown-chance band** per
   provider — `unknown`, `bad`, `mixed`, `good`, `very_good`, `exemplary`, ordered, stable codes
   the frontend translates — never counts, rates or response times. The raw history
   (`HosterContact`) lives in the model and the admin only.

`catalog.hosting.cooperation.takedown_chance` derives the band in one pure function, from
**decided** notices only (any outcome but `awaiting`), with named thresholds:
`MIN_DECIDED_NOTICES = 3` (below: `unknown`), removal rate ≥ 0.95 `exemplary`, ≥ 0.80
`very_good`, ≥ 0.60 `good`, ≥ 0.30 `mixed`, else `bad`; and `mixed` whenever more than half of the
decided notices were `forwarded` (a CDN passing it on says little about the provider itself).
**Today no notices exist, so every provider is `unknown`.** Nothing writes `HosterContact` until
notices do (#33, #46).

The case detail's `not_available` no longer lists `hoster_metadata` and `jurisdiction`. It keeps
`cooperation_history` (reason `prerequisite_unavailable`, #33): the band is honest, but it will
stay `unknown` until a first notice exists, and the case file says why. The codes stay in the
`CaseUnknownTopic` vocabulary.

Safety and imagery are inherited: only visible findings get a section, a flagged page contributes
neither its site's host nor its CDN to a response (and its CDN is not even a `lookup_hosting`
candidate), and content hosts reach the customer package as **hostnames only** —
`marceline/hosting.py` reads media URLs and hands over nothing else of them. This amends ADR 0025's "nothing in `marceline/app` reads
`index.Media`": the app package still does not, and still has no media field.

## Consequences

- **A migration ships** (`catalog/0007_hosting_records`). Apply it from the main checkout
  (`make migrate`) and **restart the workers** (`make workers`), which also starts the daily
  `lookup_hosting` scheduler job.
- New outbound traffic: DNS queries (system resolver and Team Cymru's service) and HTTPS to
  `data.iana.org` and RDAP servers — never to a crawled site. With ~20 sites and their CDN hosts
  that is well under a hundred requests a month after the first fill.
- Team Cymru's service learns which addresses we ask about. They are the addresses of piracy hosts
  and their CDNs, not of customers.
- The registrable-domain rule is small and offline (like `crawler.discovery.domains`, which
  `catalog` may not import): a multi-label public suffix outside its list resolves to the wrong
  domain and the registrar lookup fails with a code, visibly.
- CloudFront, Gcore and other CDNs that share an AS with general hosting are not detected; `cdn`
  is null there, which says "none known", not "none".
- Not every ccTLD registry is in IANA's RDAP bootstrap (e.g. `.de` is not); those domains get no
  registrar (`domain_rdap_no_registry`) and the abuse contact must come from the IP network.

## Alternatives rejected

- **A new `hosting` app.** It would need exactly `catalog`'s dependencies (`core`), so it would add
  a contract and a migration history without separating anything.
- **WHOIS on port 43.** Unstructured, per-registry formats, and being phased out for gTLDs; RDAP is
  the structured successor and names its sources.
- **Asking the site** (`Server` headers, a HEAD request for CDN detection). It is a request to the
  piracy site, which this feature must never make.
- **Storing the jurisdiction.** It is a pure function of the country; storing it would freeze
  today's table into every row.
- **Showing notice counts and response times.** Rejected by the operator: internal statistics
  are a competitive asset and invite arguments about the numbers; the band carries the meaning.
- **Omitting hosts not looked up yet.** A finding with no page host would read as "no host".

## Amendment 2026-09-16: takedown speed

The operator asked for a metric on how fast a hoster reacts to our notices and how fast content
comes down. Same rules as the takedown chance: **exact numbers are internal, a customer sees a
band**, only for providers attached to their own findings, as a stable code.

### Two durations per notice, removal observed rather than typed

- **Time to first response:** `HosterContact.sent_at` → `responded_at`.
- **Time to removal:** `sent_at` → **`HosterContact.removed_observed_at`** (new, migration
  `catalog/0008_hostercontact_removed_observed_at`), the first availability check after sending
  that found **every** page the notice named gone.

A notice names its pages through **`index.Page.notices`** (M2M to `HosterContact`, migration
`index/0011_page_notices`). The link is declared on `Page` because `index` may import `catalog`
and not the reverse; `catalog` code never reads it. `marceline.hosting.observe_notice_removals`
stamps the date: per page a case finding's `removed_at` (the first check that found it gone)
when one is at or after `sent_at`, else a removed page's `availability_checked_at` (removed pages
are not rechecked by default, so that is its first removal check); the notice's stamp is the
latest of its pages. A page already gone when the notice went out gives no stamp — its removal
says nothing about the provider. The stamp is written once and never moves, like every case
stamp (ADR 0025). A daily scheduler job `observe_notice_removals` runs it; it makes no request.

Where nothing can be observed (a notice naming no indexed page) but the notice was recorded as
`removed` with a reply date, the reply date stands in. That is the only hand-entered fallback.

**Granularity.** A removal is only as precise as the recheck interval: `crawl_recheck` visits a
page about once every 24 h, and later when a site's queue is long. A page taken down an hour after
the notice is seen gone at the next check. The durations are day-grained in practice and are
never presented with minute precision; `very_fast` (≤ 24 h) therefore means "gone by the first
check or so", and the band boundary between `very_fast` and `fast` is the least reliable one.

### Aggregation (`catalog/hosting/speed.py`, pure)

Per provider: median and p90 time to first response, median and p90 time to removal, share
responded, and both sample sizes (`services.speed_by_provider`). Rules:

- **"Never" is a sample**, sorted after every duration, but only once the notice is older than
  `OPEN_NOTICE_WINDOW` (30 days) — or its outcome already says so (`no_response` for both,
  `refused` for removal). Younger open notices are left out, so fresh notices don't skew anything.
- Percentiles take the **upper nearest rank**, never interpolated: with small samples and "never"
  in them an interpolated value is a number nobody observed, and the upper rank never flatters.

Internal display: read-only fields on the `HostingProvider` admin change page (and
`removed_observed_at` on the `HosterContact` admin). No staff API or new screen.

### The customer band

> **Superseded for customers on 2026-09-17** (amendment below): the band is internal now.


`takedown_speed` sits next to `takedown_chance` on every `CustomerHostOut`: `unknown`,
`very_slow`, `slow`, `moderate`, `fast`, `very_fast` (worst to best), derived from the **median
time to removal** with named constants: `unknown` below `MIN_SPEED_SAMPLES` removal samples
(= `MIN_DECIDED_NOTICES`, 3), then `very_fast` ≤ 24 h, `fast` ≤ 72 h, `moderate` ≤ 7 d, `slow`
≤ 30 d, `very_slow` beyond or never removed. No duration, count or sample size reaches a customer;
a test asserts the customer schema's property names. **Today no notices exist (#33), so every host
is `unknown`.**

Consequences: two migrations to apply from the main checkout, then restart the workers (the
scheduler gains the job). The thresholds are guesses until there is history; tune the constants,
not the API.

## Amendment 2026-09-17: a rounded response history replaces the takedown bands for customers

**Context.** The "takedown chance: good/bad" bands are not calibrated probabilities, and the
labelled data is far too small to support a predictive claim. Customers see nothing yet and later
a **provider response history**. Jurisdiction statements ("EU host — DSA applies") become
suggested notice channels. The operator decided both on 2026-09-17.

**Decision.**

- `takedown_chance` and `takedown_speed` are removed from `CustomerHostOut`. Their pure functions
  (`cooperation.takedown_chance`, `speed.takedown_speed`) and the exact statistics stay, internal
  only (the `HostingProvider` admin shows the speed band as "internal band").
- `CustomerHostOut.response_history` is `null` — the UI says "No response history yet" / "Noch
  keine Erfahrungswerte" — or `{removed_percent, usual_removal_time}`, derived in
  `catalog/hosting/history.py` (pure) and rendered past tense: "Past notices: About 75 % removed ·
  usually 3+ days" / "Bisherige Meldungen: Etwa 75 % entfernt · meist 3+ Tage". No count, exact
  rate, duration or sample size reaches a customer.
- **Observed notice:** its removal question is settled by the rules of `speed.time_to_removal` —
  removed (seen gone by an availability check, or recorded `removed` by the provider, with or
  without a date), or not removed (`refused`, `no_response`, or older than `OPEN_NOTICE_WINDOW`
  with nothing seen). Young open notices are not observed yet.
- **Minimum sample:** fewer than `HOSTING_HISTORY_MIN_NOTICES` (setting, default 5) observed
  notices for the provider → `null`.
- **Removal share:** removed / observed, rounded to the nearest 5 % (halves up), shown as
  "about/etwa".
- **Speed:** the median (upper nearest rank) of the **timed** removals, floored into `same_day`
  (< 1 d), `1_plus_days`, `3_plus_days`, `7_plus_days`, `14_plus_days`, `30_plus_days`. The share
  already says how many were not removed, so "never" is not part of this median. Fewer than
  `MIN_TIMED_REMOVALS` (3) timed removals → `usual_removal_time: null` and the line shows the share
  only, so one quick removal never reads as "usually".
- The rule that customers see only providers attached to their own findings is unchanged.
- The `jurisdiction` code keeps its JSON key and values; its description and every customer string
  now say "suggested notice channel" (DSA Article 16 / DMCA § 512 / the provider's abuse contact /
  none yet), never that a law applies.

**Consequences.** An API change without a migration (`openapi.json`, frontend types). The production
cache renders bundles through the same views, so it follows automatically (byte-identity test).
Today no `HosterContact` rows exist, so every customer sees "No response history yet". Wording
rules for the whole customer UI live in `frontend/CLAUDE.md` and are enforced by
`frontend/src/i18n/wording.test.ts`.

## Amendment 2026-09-17: provider function, location/nexus and the complaint-channel directory

The `eu / us / other / unknown` class is too coarse to pick a route: DSA Art. 16
is a *hosting* obligation, §512 separates hosting (c) from information-location tools (d), a CDN
must be classified by what it does for the material, and a registrar cannot remove one page. The
operator decided on 2026-09-17 to build the classification now. **No notice is sent by any of
this**; it is data for the draft engine and for staff.

### Two classifications per provider role, never merged

Every provider attached to a host carries, separately:

- **Location/nexus** `eu_facing | us_nexus | other | unknown` — a hint, **not a legal verdict**
  (a non-EU provider can be EU-facing). Staff set it on the directory entry; otherwise it follows
  the establishment country staff recorded, else the IP allocation country (`country`, as before).
- **Function** `origin_host | file_host | user_content_platform | reverse_proxy_cdn | storing_cdn
  | registrar | search_engine | payment | advertiser | other | unknown`, **per role** (`slot`):
  `network` (the AS serving the hostname), `registrar` (of its registrable domain) and `service`
  (an operator staff linked to the hostname itself, e.g. a file host). Precedence
  (`catalog.hosting.providers.derive_function`, pure): a per-hostname staff override with a
  required note → mechanical (`registrar` slot → `registrar`; a network we detect as a CDN →
  `reverse_proxy_cdn`) → the directory entry's default → `origin_host` for a network without a
  detected CDN, `unknown` otherwise. **Nothing derives `storing_cdn`**: a database constraint
  keeps it out of directory defaults, and only an override (a person verified the CDN stores the
  material) can set it.

### The provider directory (staff-maintained, `catalog`)

Migration `catalog/0015_provider_directory`:

- `DirectoryProvider` — an organisation: name, establishment country, nexus (+ note), default
  function, note. Linked to lookup results by `ProviderIdentifier` (`asn`, `registrar_iana_id`,
  `hostname`; a hostname matches its subdomains).
- `ComplaintChannel` — kind `dsa_art16 | dmca_512c | dmca_512d | provider_copyright_policy |
  provider_copyright_policy_dmca_style | abuse_email | web_form | forwarding_only | none_known`,
  URL and/or e-mail, accepted languages if published, `forwards_to_site_operator` and
  `publishes_to_transparency_db` (null = unknown; a set flag needs its source URL, enforced by the
  database), `transfer_basis` `not_required | adequacy | dpf | art46 | art49_legal_claim |
  counsel_review` (default `counsel_review`, staff-set per recipient, never inferred from a
  country — DPF certification is checked per organisation), `source_url`, `checked_at`.
  **Versioned:** an edit writes a new current row and stamps the old one `superseded_at` /
  `superseded_by`; old versions are read-only and nothing is deleted (retire instead). A channel
  verified longer ago than `PROVIDER_CHANNEL_RECHECK_DAYS` (90) is due: the directory changelists
  warn, a filter lists them, `provider_directory_report` prints them.
- **Role addresses only.** The e-mail validator refuses a mailbox without a role word (`abuse`,
  `dmca`, `legal`, `copyright`, …) — a guard against storing a named abuse officer, not a proof.
  There is no field for a person's name.
- `ProviderFunctionOverride` — (hostname, slot) → function with a required note.

Where the directory has no channel for a role, the RDAP abuse contact the lookup already stores
(network abuse role, registrar abuse contact) stands in as an `abuse_email` channel marked
`source=rdap`; once staff record channels for that provider, only theirs are used.

### v1 support per (function, channel)

`catalog.hosting.providers.v1_support` (pure):

| Function | Channels | Support |
|---|---|---|
| origin host, file host, user-content platform, storing CDN (override only) | Art. 16, §512(c), provider policy (incl. DMCA-style), web form, abuse mailbox | `primary` |
| search engine | §512(d), provider policy, web form | `delisting_draft` (Art. 16, §512(c), abuse mailbox: `not_supported`) |
| reverse-proxy CDN | anything but §512(d) | `forwarding_only` (forwarding, host identification) |
| registrar, payment, advertiser | any channel | `additional_contact_only` — no escalation |
| other, unknown | any channel | `needs_classification` |
| any | `none_known`; §512(d) at a host | `not_supported` |

`suggest_channel` orders usable channels by support, then a kind preference for the nexus (e.g.
Art. 16 first when EU-facing, §512(c) first with a US nexus), a directory channel before the RDAP
fallback, a fresh one before one due. It is a starting point for a person, not a legal conclusion.

### Staff surface only, for now

`catalog.hosting.directory.provider_roles(hostnames, now)` assembles the roles (a handful of
queries for any number of hosts, no request). Surfaces: the admin (*Directory providers*,
*Complaint channels*, *Provider function overrides*; each `HostingRecord` page shows its roles
and suggested channel) and `manage.py provider_directory_report [--hostname H] [--due-only]`,
which also lists roles the directory does not cover yet.

**The customer API is unchanged.** A parallel change is rewording the same `hosting` section of a
finding and replacing the takedown-chance band there; adding fields to `CustomerHostOut` at the
same time would conflict. A later change may add the function label and a "suggested notice
channel" code to that section from `provider_roles`. Until then the customer section keeps its
`jurisdiction` field, which this amendment does not reinterpret.

No new network source: every fact comes from the existing DNS/RDAP records or from staff. The
host itself is still never asked.

Consequences: one migration to apply from the main checkout, then restart the workers. Staff
need to fill the directory before any suggestion is more than the RDAP abuse mailbox: providers
with their AS numbers / registrar IANA ids / hostnames, their published channels with source
URLs, forwarding and transparency practice, and a transfer basis per recipient.
