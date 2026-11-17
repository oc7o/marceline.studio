# 0041 — A media URL shape that keeps answering 404 stops being fetched, per site

Status: **accepted** (2026-09-18). Built and **off by default**
(`MEDIA_URL_BREAKER_ENABLED`). Extends ADR 0007 (crawler policy and request budgets) and ADR 0018
(the media gate); shares its notion of a URL *shape* with ADR 0016's capture refusals
(issue #78 amendment). Issue #82.

## Context

site17.example's KVS theme advertises a `preview.jpg` for every video in its own `og:image` and
JSON-LD, but the file was only ever generated for the newest slice of the catalogue. We fetched
what the site's metadata claimed, and the site answered 404 — with a 246 kB HTML error page, every
time.

Measured on the stored rows of that site (`index.Media`, shape
`site17.example/contents/videos_screenshots/*/*/preview.jpg`):

| | |
|---|---:|
| poster rows settled on that shape | 4,448 |
| of them 404 | **3,810 (86 %)** |
| of them fetched successfully | 638 (14 %) |
| real media requests to that site answered 4xx in the 24 h of request accounting we have | **1,771** |
| bytes spent on them | **436 MB** |
| the site's origin media budget | 6,000 / 24 h |

Age was not the variable: 1,099 of the failures were fetched within an hour of being queued. The
site's own upload-id sequence separated the living from the dead, and nothing in a URL tells us
which side of that line a given id is on.

That site is fixed — `KvsSite.poster_from_screenshot` now takes the poster from a screenshot the
site actually serves — but the fix is site-specific. Nothing in the media path notices that a URL
*shape* is reliably dead on a site, so the next site with a lying template teaches us the same
lesson one request at a time, and we only find out by an operator reading a budget report.

The measurement that shaped this design is the second row of the table: the dead shape was **not**
uniformly dead. A rule of the form "N failures and *no* success" — the obvious one, and the one the
issue proposed — would never have fired here, because a success landed in every single hour the
worker ran. Whatever we build has to trip on a shape that is *mostly* dead, which is exactly the
case where being wrong is expensive. Everything below is arranged around that.

## Decision

**Give every (site, URL shape) pair a circuit breaker on the one answer that means "this is not
here": 404 and 410. Suppression postpones, never discards. Off by default.**

### 1. A shape is the host plus the path with its identifier-looking segments starred

One heuristic, in `core/url_shapes.py`, used by both consumers. The capture work
(`marceline/capture/requests.py`) invented it and keeps its names by importing them; the media path
is the second consumer, which is why it moved to `core` (the project package `marceline` may import
apps, apps may never import it).

`url_shape("https://cdn.example.test/thumb/9f1c2b7a91e0.jpg")` →
`cdn.example.test/thumb/*.jpg`. Per `/`-separated segment: all digits, 6+ hex characters, a 12+
character alphanumeric run mixing letters and digits, or — **new here** — a canonical UUID, is
replaced by `*`; a file extension is kept, since it describes the resource rather than who is
asking. Query strings and fragments are dropped outright.

The UUID clause is an amendment to the shared heuristic, not a second notion of a shape. It is
needed because media URLs are full of them where capture's were not: `site10.example`'s CDN keys
every thumbnail by a UUID, so before the clause its 290 4xx answers of one day spread over 261
shapes seen exactly once each — counters that can never accumulate evidence and can never protect
anything. With it they collapse into ~30 shapes with 5–16 % failure, which is a *healthy* shape
correctly measured and correctly left alone.

The heuristic stays a heuristic: a short, literal-looking segment that happens to be an id is kept
verbatim, and a stored shape is read as "shaped, not guaranteed id-free". Two things make that
acceptable here. Over-splitting (an id kept verbatim) is safe by construction — the counters never
reach N and the breaker never trips. Over-merging is bounded by the fact that a shape is scoped
**per site**, and it is checked by the rate clause below.

Two properties of the heuristic were verified against the real case rather than assumed:

- It separates the dead shape from the healthy one *on the same site and the same URL prefix*:
  site17.example's dead posters are `…/videos_screenshots/*/*/preview.jpg` and its healthy hover
  frames are `…/videos_screenshots/*/*/566x302/*.jpg` (4,627 fetched, 0 gone). `preview` is not
  id-like, so it survives starring, and the breaker can suppress one while leaving the other alone.
- It does not merge a site's whole media into one bucket: 306,460 media rows shape into 104,465
  distinct (site, shape) pairs, and only **3** of them have reached 20 gone answers ever.

### 2. Only "this is not here" counts

The breaker is fed by exactly two outcomes and ignores everything else:

- **gone** — HTTP **404** or **410**, on a URL that is not a tokenized expiring link.
- **ok** — any **2xx**. It proves the shape serves something; whether the bytes then turn out to be
  undecodable or the wrong content type is a different problem and never the shape's fault.

Everything else leaves the state untouched — not "counts a little", untouched:

- **5xx, 429, connection errors, timeouts**: the server is unwell or is telling us to slow down.
  A shape must never be suppressed for that; it is the "merely slow or rate-limited" case the issue
  names, and the one that would silently stop us fingerprinting a working site.
- **403**: an authorization answer, not an absence. The media worker groups it with 404/410 for its
  own retry decision (`GONE_STATUSES`), and this ADR deliberately does not: hotlink protection, a
  WAF and a geo-block all answer 403, and every one of them can be lifted without the URL ever
  having been wrong.
- **A 404 on a tokenized expiring URL** (`matching.media.EXPIRING_URL_REGEX` — site03's CDN issues
  a per-asset token with a 10-digit expiry). That 404 is about the token's age, never the shape.
  This exclusion is not theoretical: without it the measured data trips the breaker on **four**
  healthy site03 CDN shapes, one of them with a 45-answer run of 404 against 491 successes. The
  worker already labels these `link expired or gone` and distinguishes them from `media gone`; the
  breaker reads that same distinction.
- **Anything refused before the request was sent**: the safety gate (ADR 0018), robots.txt, an
  exhausted budget, a busy host slot, a non-public address. No answer was received, so there is
  nothing to learn. This falls out of where the recording happens — in the media worker, on a
  response — rather than needing a rule of its own.

A **soft 404** — a site answering 200 with an HTML error page — is recorded as `ok` and keeps the
breaker closed. That is the wrong answer in the cheap direction: we go on paying for the shape
instead of going blind on it, and the worker already skips such a response as `not media`. The
site that motivated this ADR answered a real 404, so nothing here is fitted to a case we have not
seen.

### 3. A shape is suppressed at 20 gone in a row while 70 % of its recent answers are gone

Two clauses, both required:

- **`MEDIA_SHAPE_GONE_STREAK` = 20** consecutive gone answers with no ok in between, and
- **`MEDIA_SHAPE_GONE_SHARE` = 0.70** of the shape's recent settled answers gone, over a decayed
  window of the last **`MEDIA_SHAPE_WINDOW` = 100**.

N was chosen from the measured runs, not from taste. Simulated against every settled media answer
in the index, replayed in the order it really happened:

| rule | requests saved | successes delayed | sites tripped |
|---|---:|---:|---|
| streak ≥ 10, no rate clause | 5,257 | 5,011 | site17.example, site04.example, **site03.example** |
| streak ≥ 20, no rate clause | 4,397 | 2,565 | site17.example, site04.example, **site03.example** |
| streak ≥ 50, no rate clause | 3,728 | 1,550 | site17.example, **site04.example** |
| streak ≥ 20, expiring links excluded, share ≥ 0.5 | 3,809 | 1,596 | site17.example, **site04.example** |
| **streak ≥ 20, expiring links excluded, share ≥ 0.7** | **3,788** | **638** | site17.example |
| streak ≥ 10, expiring links excluded, share ≥ 0.7 | 3,798 | 638 | site17.example |
| streak ≥ 50, expiring links excluded, share ≥ 0.7 | 3,707 | 1,550 | site17.example |

The rows in bold are the interesting failures. `site04.example`'s `imgs.cdn-site04.example/thumb/*.jpg`
is a **healthy** shape — 10,301 fetched, 211 gone, 2 % — and it still produced a run of **51
consecutive** 404s, because the queue is ordered by page, not by chance. A streak rule alone
therefore cannot be made safe by raising N: 51 today says nothing about 80 tomorrow, and an N high
enough to be safe is an N that wastes hundreds of requests before it acts. The rate clause is what
protects it: 51 gone in a window of 100 is 51 %, and 0.70 has margin above that from the worst case
we have actually seen. site03 is protected twice over — by the expiring-link exclusion and by its
32 % share.

Given the rate clause, N between 10 and 50 changes the saving by 2 % (3,798 → 3,707). So N is not
chosen for its saving; it is chosen for how much we are willing to pay to *learn* that a shape is
dead. 20 requests at the media delay is well under a minute of one worker, and it is far enough
above the longest run a healthy shape produces *within one window* to make a coincidence trip
implausible. **N = 20.**

The window is decayed rather than exact: `gone_recent` and `ok_recent` are incremented per answer
and both halved when their sum exceeds 100. That keeps the state to two integers, survives a race
between two workers on the same shape (a counter off by one changes nothing), and weights the
recent past the way the rule wants.

A shape whose last gone answer is older than **`MEDIA_SHAPE_STREAK_TTL_H` = 168** (7 days) starts its
streak again. Stale evidence should not accumulate across a re-crawl months later.

### 4. Suppression postpones; it never discards, fails or skips

This is the safety property that makes the whole thing acceptable, and it is why the enforcement
point is the media worker rather than the crawler that queues the row.

A suppressed media row goes back to **PENDING** with `next_attempt_at = suppressed_until`, its
attempt refunded, and `last_error` naming the shape and the reason — the same thing that already
happens when a budget is exhausted or a host slot is busy (`matching.media.defer_media`). It is not
`SKIPPED` and not `FAILED`, so nothing has to be re-queued by hand when a shape is rehabilitated:
the rows are simply claimed again when the clock says so.

The check runs in `MediaWorker.process`, immediately before `fetch_media`, so it covers both
generations — the legacy per-site `media_worker` and the crawl runtime's media lane (ADR 0030) —
and it cannot get in front of the safety gate, which is checked inside the fetch against the
database (ADR 0018). A suppressed shape never reaches the network, which is the entire point; it is
also never allowed to *become* a reason to request something, so nothing else in the system reads
this state.

The moderation UI's image passthrough (`enforce_budget=False`) does not consult the breaker. It is
one operator-driven image, not a budget, and being able to look is how an operator confirms what
the counters claim.

### 5. Rehabilitation: a time-boxed open state, then exactly one probe

A site fixing its theme must not be locked out, and it must not have to be noticed by a human. The
breaker is therefore never permanent — the hard half of this design, and the half that decides
whether the operator can leave it switched on.

```
closed ──20 gone in a row, ≥70 % of the window gone──▶ open (6 h)
  ▲                                                     │ the window passes
  │                                                     ▼
  └────────── 2xx ─────────────────────────── half-open: the next claimed row is the probe
                                                        │ 404/410
                                                        ▼
                                              open again (12 h, 24 h, 48 h, 72 h …)
```

- **open**: rows of the shape are postponed to `suppressed_until`. Nothing is requested.
- **half-open**: the first row claimed after the window becomes *the probe* and is fetched
  normally. Other rows of the same shape are postponed by `MEDIA_SHAPE_PROBE_TIMEOUT_MIN` (10 minutes)
  so that the whole backlog cannot pour back onto a site that has not answered yet. If the probe's
  worker dies, the timeout releases the next claimant.
- A **2xx anywhere** — the probe, or any row of the shape that was already in flight when it
  tripped — closes the breaker, clears the streak and resets the backoff to zero. One success is enough: the whole claim of this ADR is that a 404 is
  cheap to keep re-learning, and being wrong about a *living* shape is the expensive direction.
- A gone answer from the probe re-opens with the next backoff step: 6 h → 12 h → 24 h → 48 h →
  72 h, capped at `MEDIA_SHAPE_MAX_BACKOFF_H` = 72.

So a dead shape costs **one request per backoff window** — at the cap, 1 request per 3 days instead
of one per page — and a shape the site repairs is back within one window of the repair, with no
human involved and nothing to re-queue. The cap is deliberately days and not weeks: the probe is
one request, so there is nothing to buy by waiting longer, and a shorter cap is a shorter blind
spot.

Two further doors exist for an operator who does not want to wait: deleting the `MediaUrlShape` row
in the admin (*Catalog → media URL shapes*), and `manage.py media_shapes --site DOMAIN --clear
SHAPE`. Both are ordinary state resets, not overrides — the breaker will simply re-learn whatever
is true.

### 6. Off by default, and visible before it is switched on

`MEDIA_URL_BREAKER_ENABLED` (default **false**) decides only whether a shape is ever *suppressed*.
The counters are kept either way, so the operator reads a real report of what the breaker would
have done on live traffic before handing it the budget — the same shape of decision as ADR 0037 and
ADR 0040, and the reason this ADR could be written from measurement instead of taste.

The counts are visible in two places, both fed by one function so they cannot disagree
(`catalog.media_shapes.shape_lines`):

- `manage.py request_report`, under the site whose budget the waste comes out of, and
- the Site admin page, which already renders the same request report, plus a `MediaUrlShape`
  admin listing every shape with its counters, state and last answers.

A suppressed shape is named as a shape — `site17.example/contents/videos_screenshots/*/*/
preview.jpg` — never as a URL. The starred form is what the operator needs to fix a site adapter,
and the identifiers that would make it a real piracy URL are exactly the part the shaping removes.

## Consequences

**What it would have saved.** Against the request accounting we have (which begins 2026-09-17
11:00, when issue #74 landed — there is no 30-day history to report, the whole media index is four
days old), the breaker would have removed **1,771 requests and 436 MB in 24 h**, all of it on
site17.example, which is 28 % of that site's daily media budget. Replaying every settled media answer in the index
through the rule **as built** — the decayed window, the UUID clause, the expiring-link exclusion,
the half-open probe and the 6 h → 72 h backoff — it suppresses **3,788 of the 3,810** gone answers
on that shape (99.4 %), delays 638 successes, trips 3 times, and touches **no other site**:
site04.example, site03.example, site10.example and site05.example all come through untouched.

Per day, from the media rows themselves (the longer record — 2026-09-14 to 18), the gone answers
that this breaker would have caught are: 2026-09-16 **2,033** (500 MB), 09-17 **1,282** (316 MB),
09-18 **495** by mid-day (122 MB). So on the order of **1,300–2,000 requests and 300–500 MB a
day**, on one site, for as long as nobody noticed.

**Across all other sites the saving is zero, and that is the honest headline.** The other sites'
404 answers in the same period are site04.example 25–93 a day (a 2 %-failure shape the rate clause
protects), site10.example 82–297 a day (spread over ~30 shapes at 5–16 % failure, none of which
trips), and single answers on site05.example, site02.example, site09.example and site06.example. site17.example
is the only real case in the data, and it is already fixed at the adapter, so switching this on
today saves nothing at all. What it buys is that the *next* lying template costs 20 requests and a probe
every three days instead of one request per page for as long as it takes an operator to read a
budget report — 3,810 requests and 436 MB, in the one case we have measured.

**What it costs.** On site17.example the breaker would also have postponed the 638 posters that did
work. Those pages are not left without evidence: every one of them also carries hover-frame rows on
a separate, healthy shape (4,627 fetched, 0 gone), and 0 of the 638 pages have no other media row
at all. 242 of them had no other *fetched* media at the moment of the trip, so on a site in this
state the breaker trades a delay on those pages' first evidence against the site's whole media
budget. That trade is only sound because the delay is bounded by the backoff and the rows are never
discarded (§ 4).

**What can still go wrong.** A shape both mostly-dead and partly-alive — site17.example exactly —
will flap: probe succeeds, breaker closes, 20 more 404s, breaker opens. Each cycle costs 20
requests. That is by design; the alternative is refusing to fetch something the site is serving.
Replaying the measured window through the rule as built gives **3** trips, not dozens.

**Where this is not the answer.** The real fix for a lying template is the site adapter — take the
poster from something the site actually serves, as site17.example's now does. This breaker is the
backstop that bounds the cost of not having noticed yet, and a suppressed shape in the report is a
bug report against an adapter, not a resting state.
