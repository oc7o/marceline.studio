# The public marketing site — marceline.studio

Static HTML and CSS. No framework, no build step, no JavaScript, no images, no third-party
request of any kind. `public/` is the site: what is in that directory is what a server serves.

```
website/
  public/            the site, exactly as served
    index.html         German, at the apex
    so-funktioniert-es.html
    fuer-studios-und-agenturen.html
    kontakt.html
    impressum.html          § 5 DDG provider identification
    datenschutz.html        Art. 13 DSGVO, live since 2026-09-18 — see "Going live"
    en/                English, at /en/ (same six pages)
    assets/site.css    the design system's tokens, copied from the product
    assets/favicon.svg
    assets/fonts/      six woff2 files + LICENSES.txt (SIL OFL 1.1)
    robots.txt
    sitemap.xml        hand-maintained; `check.py` fails when it drifts
  check.py           the whole build system (`make website-check`)
  publish_document.py  prints the ONBOARDING_DOCUMENTS entry for a published document
  README.md
```

How it is served is not in this directory: on production a small Caddy of its own serves
`public/` from `/srv/site` (`infra/prod/site/`, project `marceline-site`; the customer stack's
`infra/prod/Caddyfile` keeps the same block behind `PUBLIC_SITE`, off, for the day the two are
merged back — "Going live" below says why). There is deliberately no third copy of that
configuration here, and `backend/marceline/tests/test_site_edge_config.py` keeps the two that
exist identical.

## Onboarding documents

The documents a creator accepts during onboarding by invite (ADR 0052) are **not** part of the
site, and their drafts are not in this archive. `publish_document.py` prints the
`id:version:sha256:url` entry `ONBOARDING_DOCUMENTS` needs, by hashing exactly the bytes a server
hands out, and refuses a page that still carries a `{{PLACEHOLDER}}` or a draft marker.

## Editing it

There is nothing to install and nothing to compile. Open a file, change it, run

```
make website-check
```

To look at it, any static server will do; `python3 -m http.server` does not do extensionless
URLs, so links will 404 under it. The closest thing to production is production itself:
`make prod-surface-check` starts the real Caddy image with the real `infra/prod/Caddyfile` and
this directory mounted, and asserts every page of the sitemap by its extensionless URL.

## Why it is built this way

The product's pitch is that it stores nothing about anyone and asks nobody else for anything.
A brochure that ships an analytics script, a cookie banner and a font from someone else's CDN
contradicts that on the way in. So the site has none of them, and `check.py` fails the build if
one ever appears. That is also why there is no cookie banner and the site says so: consent is
needed where something is stored on a visitor's device or read from it that the site does not
need, and none of that happens here.

A second Node process next to the product, on a small VPS, to serve six pages of prose would be
waste. `file_server` on a directory is the whole runtime.

The design tokens are **copied** out of `frontend/customer/app/globals.css` into
`public/assets/site.css` rather than imported — that file is a Tailwind v4 `@theme` block. When a
token changes there, change it here too; no check can see that drift.

## What `make website-check` checks

`check.py`, standard library only, no network, under a second:

| | |
|---|---|
| **No foreign origin** | Every `.html`, `.css`, `.svg`, `.xml` and `.txt` file is scanned for any URL that is not `https://marceline.studio`. XML namespace URIs pass only as the value of an `xmlns` attribute. `assets/fonts/LICENSES.txt` is the one documented exemption: the SIL OFL requires that notice to travel with the files, and it names the upstream projects' URLs. The Impressum links to it — that is where the fonts are credited since 2026-09-18, instead of in every footer — but it is served as plain text, so a browser shows those URLs and never fetches one. |
| **No banned wording** | The forbidden patterns are ported from `frontend/src/i18n/wording.test.ts` — the list the product enforces on every customer string. Both languages' lists run against every page, over the visible text plus the title, the meta description and reader-visible attributes. A set of canary sentences makes sure a future edit cannot pass by weakening a pattern. A hit means rephrase, never loosen. |
| **HTML** | Tag nesting, doctype, `charset`, viewport, `lang` matching the tree, exactly one `<h1>`, one `main`/`header`/`footer`/`nav`, no heading-level jumps, unique ids, a `<title>` naming the company and under 70 characters, a 50–200 character meta description. No `script`, `iframe`, `form`, `video`, `canvas`, inline `on*` handler or inline `style`. Any `<img>` must carry `loading="lazy"`, `decoding="async"`, `alt`, `width` and `height` — there are none today, and prose, CSS and inline SVG are the reason. Inline `<svg>` is allowed and is how the icons are drawn, but only as drawing: `<image>`, `<foreignObject>`, `<use>` and any `xlink:` attribute are refused, and `SVG_CANARIES` keeps that rule from being widened. |
| **Links** | Every internal `href`/`src`, every `url()` in the stylesheet and every `<loc>` in the sitemap has to resolve to a real file, by the same three `try_files` rules the site block in `infra/prod/Caddyfile` gives Caddy. A `#fragment` must name an id that exists on the target page. |
| **No publishing blocker** | A fact only the operator can supply stands in the page as a literal marker (`PUBLISHING_BLOCKERS` in `check.py`), and the check refuses to pass while one does, naming what to fill in. Today there is one: `TODO(hoster)`. A legal page that looks finished and is not is the one failure this site cannot afford. |
| **Sitemap** | Every non-`noindex` page is listed, and no `noindex` page is. `robots.txt` points at it. |
| **Weight** | Per page and per first view, against a budget. Printed every run. |
| **Forbidden content** (issue #128) | No image/thumbnail is already covered above. This adds: no site name (a domain-shaped string in prose — site names live in `catalog.Site`, never in this repository, and this file must not grow a list of them), no quoted title of a work, no testimonial naming a creator (`blockquote`/`cite`/`q` are forbidden elements; quoting is forbidden text). Purely prospective — the site is already past this bar. Canaries (`FORBIDDEN_CONTENT_CANARIES`) keep the patterns from being quietly narrowed; `backend/marceline/tests/test_website_check.py` proves each guard fires. |
| **No registered-trademark claim** (issue #145) | The character `®` and the phrases *eingetragene Marke*, *als Marke eingetragen*, *eingetragenes Warenzeichen*, *markenrechtlich geschützt*, *registered trademark* / *mark* / *brand* are refused on every page. The name is not registered (issue #55), so no page claims it is. `™` asserts nothing and is allowed. Purely prospective: a sweep of `website/public/`, `frontend/src/i18n/` and `frontend/customer/` on 2026-09-19 found no such claim. `REGISTRATION_CLAIM_CANARIES` fails the check if a pattern is weakened, and `REGISTRATION_CLAIM_ALLOWED` fails it if a pattern grows wide enough to also refuse the Impressum's own "Nicht eingetragener Einzelunternehmer" (§ 5 DDG). **This guard comes out on the day a registration exists — with the registration number in the commit message.** |
| **The cloud-fingerprinting claim** (issue #124) | "Dein Video bleibt bei dir." / "Your video stays with you." is a headline promise and the `<meta name="description">` on both home pages. The check reads every place `CLOUD_FINGERPRINTING_ENABLED`'s *default* is written (`backend/marceline/settings.py`, `infra/compose.yaml` ×2, `infra/prod/compose.yaml`) and fails if any of them says `true` while either page still states the claim unconditionally, naming ADR 0044's conditional wording as the fix — the wording has to move first, never the flag. It does not read a running container's environment. |

Measured, uncompressed (2026-09-18, after the second trim): **3.0–9.6 KB** per page of HTML.
The two long ones are the legal pages; every marketing page is **3.0–3.7 KB**. 17.0 KB of CSS and
68 KB of latin-subset woff2. A first view of the heaviest page is **94.7 KB** uncompressed and
about 77 KB over the wire (woff2 is already compressed). Every page after that is the HTML alone,
because the stylesheet and the fonts are cached.
The latin-ext font files ship but are fetched only for a character neither German nor English
needs.

## Two URL trees, German at the apex

`/` is German and `/en/` is English, with a switch in the header on every page that links to the
**corresponding** page rather than the home page. German is at the apex because the first
customers are German-speaking.

The alternative — one tree that picks a language from `Accept-Language` — needs either a script or
a server rule, which makes the page a visitor gets depend on a header. That is harder to check and
easier to get wrong than two directories you can read. Every page carries `rel="alternate"`
`hreflang` for both languages plus `x-default` on the German page.

## Going live

> **2026-09-22: the site goes offline with the production VM** (sunset).
> The DNS records must be removed or repointed when the VM is deleted, not left dangling.

**Live since 2026-09-18 19:04 UTC** at https://marceline.studio, served by the standalone
`marceline-site` project (operator decision, same day; "Publishing steps" below).

### Done (2026-09-18)

- **`impressum.html` / `en/imprint.html`** carry the § 5 DDG provider identification (the
  operator's name and address are placeholders in this archive).
- **`datenschutz.html` / `en/privacy.html`** carry the Art. 13 notice (the controller and the
  supervisory authority are placeholders in this archive).
- **No `noindex` anywhere, all twelve URLs in `sitemap.xml`**, `robots.txt` says so.
- **Pricing**: the private beta is free of charge and says so; the page names no figure and
  no "from".
- **The claims the privacy notice makes were checked against the built site**, not assumed: no
  script, no cookie or storage API, no form, no embed, no image, no foreign origin, all six font
  files on our own origin. `make website-check` keeps each of them true.

### What it took (all done, checked 2026-09-18)

1. **The hosting provider's registered seat is filled in.** No page carries `TODO(hoster)` any
   more and `make website-check` passes. The entry stays in `PUBLISHING_BLOCKERS` in `check.py` on
   purpose: it costs nothing and it keeps failing the build if the marker is ever reintroduced.
2. **The signed Art. 28 processing agreement with that provider exists** (operator, 2026-09-18).
   It is concluded in the provider's own customer account; nothing in this repository can do it or
   check it.
3. `contact@marceline.studio` receives mail, and is read (operator, 2026-09-18).
4. Deployed — 2026-09-18 19:04 UTC. The steps below are how, kept for a redeploy from scratch.

### Publishing steps (how it was deployed, and how a full redeploy would go)

The site is served on the production VPS by **its own small Caddy**, in its own compose project
(`infra/prod/site/`, project `marceline-site`), from static files, on the public address — beside
the customer stack, sharing nothing with it (operator, 2026-09-18).

It is deliberately **not** the customer stack's own Caddy, although that one has the same site
block behind `PUBLIC_SITE`. Measured on the host, 2026-09-18: that Caddy publishes 80/443 on the
host's **Tailscale** address (`CADDY_BIND=100.64.0.2`), so nothing it serves is reachable from
the internet, and it issues **internal** certificates (`CADDY_TLS=internal`) because
`dash.marceline.studio` is still private on a `100.x` A record. `CADDY_TLS` is one variable for
every site block in that file, so switching it for the brochure would also make Caddy order a
public certificate for `dash` — which Let's Encrypt can never validate, costing failed ACME
attempts and `dash`'s working certificate. Publishing from there is a launch, not a website
change; `PUBLIC_SITE` stays off until the cut-over that merges the two into one process, which is
now decided and planned (operator, 2026-09-19): "After the cut-over" below.

This is the deploy that ran on 2026-09-18 19:04 UTC, kept as the record and as the procedure a
future redeploy from scratch (a new host, a rebuild after data loss) would repeat — steps 1 and 2
are already true today.

```bash
# 0. On the development VPS, from the main checkout. The first command must pass.
make website-check          # fails while a PUBLISHING_BLOCKERS marker stands — that is the point
make prod-config            # both stacks' invariants (the site edge's too); starts nothing
make prod-surface-check     # real Caddy + the real Caddyfile + this directory (docker, ~2 min)

# 1. DNS: A for marceline.studio AND www.marceline.studio at the production VPS, TTL 300 until
#    stable (done 2026-09-18). No AAAA yet — the deploy is IPv4-only; see "Later: IPv6" below.
#    Optional: a CAA record allowing letsencrypt.org.
# 2. Provider firewall: inbound TCP 80, TCP 443 and UDP 443 open (done 2026-09-18 — the stack
#    was Tailscale-only before that). Both before step 4: Caddy orders the certificate the
#    moment the site's own stack starts.

# 3. Ship the files and that stack. This development VPS has no rsync client installed — use
#    tar over ssh (the production host's own rsync still does the in-place sync below).
tar -C website/public -cz . \
  | ssh deploy@<prod> 'mkdir -p /srv/marceline/site && tar -C /srv/marceline/site -xz --no-same-owner'
tar -C infra/prod -cz --exclude .env site \
  | ssh deploy@<prod> 'tar -C /srv/marceline/infra -xz --no-same-owner'
# Where this development VPS does have rsync (or a redeploy host that does), prefer it directly:
#   rsync -a --delete website/public/ deploy@<prod>:/srv/marceline/site/
#   rsync -a --delete infra/prod/site/ deploy@<prod>:/srv/marceline/infra/site/

# 4. On the production VPS: fill in .env and start it. IPv4 only — no compose.ipv6.yaml until
#    marceline.studio has an AAAA record (site.env.example, infra/prod/site/README.md).
cd /srv/marceline/infra/site
cp site.env.example .env && chmod 600 .env
# edit .env:  SITE_BIND=<public IPv4>
#             SITE_DOMAIN=marceline.studio        SITE_WWW_DOMAIN=www.marceline.studio
#             SITE_CANONICAL_ORIGIN=https://marceline.studio
#             SITE_ACME_EMAIL=<the project mailbox>
#             SITE_ROOT=/srv/marceline/site       # there is no checkout on this host
#             SITE_IMAGE_TAG=<a marceline-prod-caddy tag this host already holds>
C="docker compose -f compose.yaml --env-file .env"
$C config --quiet && $C config --format json | python3 check_config.py
$C up -d
docker compose -p marceline-site logs --since 5m caddy    # a certificate for both names
```

**Removing a stale file** (tar has no `--delete`, unlike the direct rsync form above): unpack
into a fresh sibling directory and let the production host's own rsync sync it into the live
one *in place*, then drop the staging copy —

```bash
# after the tar | ssh line above landed the new snapshot at /srv/marceline/site.new on the host:
ssh deploy@<prod> 'rsync -a --delete /srv/marceline/site.new/ /srv/marceline/site/ && rm -rf /srv/marceline/site.new'
```

This is the recommended way, not listing and removing stale files by hand: `/srv/marceline/site`
is bind-mounted **read-only into the running `caddy` container** by directory
(`infra/prod/site/compose.yaml`, `${SITE_ROOT}:/srv/site:ro`), and a Linux bind mount follows the
*directory's own inode* — swapping which directory sits at that path (`mv site site.old && mv
site.new site`) would leave the running container serving the old, now-unlinked snapshot instead
of the new one, silently, until the container is recreated. Syncing file contents into the
existing directory in place has no such trap: the directory's inode never changes, only what is
in it, exactly like the plain `rsync -a --delete` form already did before this VPS lost its
`rsync` client.

**Later: IPv6.** Once `marceline.studio` and `www` have an AAAA record, add `SITE_BIND6` to
`.env` and pass `-f compose.ipv6.yaml` on **every** compose command against this project from
then on (`C="docker compose -f compose.yaml -f compose.ipv6.yaml --env-file .env"`) — leaving it
off one command brings the container back without the IPv6 ports.

Then prove it from outside, from a machine that is not on the tailnet **and not this development
VPS** — it cannot reach production's public IP on 80/443 either:

```bash
# every page: 200, text/html, and no noindex
for u in / /so-funktioniert-es /fuer-studios-und-agenturen /kontakt /impressum /datenschutz \
         /en/ /en/how-it-works /en/for-studios-and-agencies /en/contact /en/imprint /en/privacy; do
  printf '%-34s %s %s\n' "$u" \
    "$(curl -s -o /dev/null -w '%{http_code} %{content_type}' https://marceline.studio$u)" \
    "$(curl -s https://marceline.studio$u | grep -c 'content="noindex"')"     # must print 0
done
curl -sI https://marceline.studio/ | grep -i 'x-robots-tag' && echo 'BAD: the brochure is noindex'
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' https://marceline.studio/assets/site.css
curl -s -o /dev/null -w '%{http_code}\n' https://marceline.studio/sitemap.xml   # 200
curl -sI https://www.marceline.studio/kontakt | grep -i '^location'             # the apex, path kept
curl -sI http://marceline.studio/ | grep -i '^location'                         # https, path kept
curl -s -o /dev/null -w '%{http_code}\n' https://marceline.studio/admin/        # 404
curl -s -o /dev/null -w '%{http_code}\n' https://marceline.studio/api/v1/health # 404: no upstream
curl -s -o /dev/null -w '%{http_code}\n' https://marceline.studio/app           # 404: not here
```

**Publishing an edit today** is step 3 alone — ship the files, nothing restarts: the directory is
bind-mounted, so there is no rebuild, no migration and no container recreate, and the customer app
is not touched.
**Rollback** is `docker compose -p marceline-site down`; the certificates stay in that project's
named volumes, so bringing it back costs no ACME order.

### After the cut-over: one Caddy, both names

The standalone stack is temporary. The host has one public IPv4 and one pair of ports 80/443, and
`dash.marceline.studio` needs them too, so **one Caddy serves both names** — the customer stack's,
with its `PUBLIC_SITE=on` block — and `marceline-site` is retired (operator, 2026-09-19).

**Shipping an edit does not change.** It is still the files and nothing else:

```bash
make website-check                       # first, always
tar -C website/public -cz . \
  | ssh deploy@<prod> 'mkdir -p /srv/marceline/site.new && tar -C /srv/marceline/site.new -xz --no-same-owner'
ssh deploy@<prod> 'rsync -a --delete /srv/marceline/site.new/ /srv/marceline/site/ && rm -rf /srv/marceline/site.new'
# then, from a host that is neither the tailnet nor the development VPS:
curl -s -o /dev/null -w '%{http_code}\n' https://marceline.studio/datenschutz     # 200
```

What changes is only *which* container reads that directory: `${SITE_ROOT}:/srv/site:ro` moves from
`infra/prod/site/compose.yaml` to `infra/prod/compose.yaml`, on the same host path
(`/srv/marceline/site`). Still no rebuild, no restart, no container recreate — `file_server` reads
the directory on every request, and the mount follows the directory's inode, which is why the
edit is rsynced **into** it rather than swapping the directory (above).

Three things to keep in mind once the brochure shares a process with the customer app:

- **A Caddy recreate now touches both names.** `docker compose up -d --no-build --no-deps caddy`
  (for a routing change, `CUSTOMER_APP`, a new release) is a second of downtime for the site as
  well. It is never needed for a content edit.
- **The site block must stay free of the customer edge's headers**, above all
  `X-Robots-Tag: noindex` — the brochure exists to be found. `make prod-surface-check` asserts that
  on both hostnames, and the two copies of the block are compared directive by directive
  (`backend/marceline/tests/test_site_edge_config.py`).
- **No access log, still.** `datenschutz.html` says this server keeps none, so the merged Caddyfile
  has no `log` directive anywhere — a guard the same test enforces, and the surface check reads the
  running container's log to prove it. An access log for the customer app would have to live inside
  the app's own site block and would mean narrowing that test deliberately.

## Who it is written for, and in what voice

The site has **two doors of equal standing** (operator, 2026-09-18): an independent creator who
sells their own videos, and a studio or agency with a catalogue. Neither is the preferred customer
and **no sentence may rank them** — no "first … then", no "we start with". The home page speaks to
the creator directly and ends with one line pointing at the other door;
`/fuer-studios-und-agenturen` keeps its URL, opens with "derselbe Dienst" and is not written as the
lesser page.

The German marketing pages — home, `so-funktioniert-es`, `fuer-studios-und-agenturen`, `kontakt` —
address the reader as **„du"**, lower case, in body text, buttons, links and meta descriptions
(operator decision, 2026-09-18). The boundary is sharp and deliberate: **`impressum.html` and
`datenschutz.html` stay formal („Sie")**, because they are legal texts that the Art. 13 notice and
the pilot agreement have to read consistently with. A formal legal page beside an informal site is
normal; a privacy notice that says „du" is not.

The voice is short declarative sentences saying what the reader gets, not how it works. **No
technology nouns** — no algorithm name, no bit counts, no crawler, no index, no frames. A creator
on a phone has to grasp the home page in ten seconds: what it is, that it is safe, that the private
beta is free, how to get in. Everything past that belongs in a conversation.

**The word budget is the rule, not a guideline** (operator, 2026-09-18, after reading the live
pages: "still very verbose … it looks too much like AI slop"). Visible words in `<main>`, per
language:

| page | budget | today |
|---|---|---|
| home | ~90 | 55 (DE) / 58 (EN) |
| how it works | ~100 | 83 / 85 |
| studios & agencies | ~100 | 61 / 69 |
| contact | ~60 | 57 / 66 |

Nothing counts those words for you: keep a page under its budget by writing less. What was cut to
get there, and must not come back: headings built as "X — und was nicht" pairs, overline
meta-labels above sections ("Stand", "Die Aufgabenteilung"), rhetorical contrasts ("nicht X,
sondern Y"), triads of parallel sentences, closing lines that restate the page, and any sentence
that would fit on any other SaaS site. **A process description is not a benefit**: what the
customer gets is said in six words, not in five steps of prose. **"A person reviews every match" is
not a selling point** and is not on the site (operator, 2026-09-18) — and nothing claims the
opposite either.

## Icons

The only pictures on this site are inline SVG icons, and they follow one rule set:

- hand-drawn paths in a 24×24 box, **one stroke width** (1.5), `fill: none`, `stroke: currentColor`
- sized in `em` by `.icon` in `assets/site.css`, so an icon follows the text beside it
- `aria-hidden="true"`, always: **the sentence next to the icon carries the meaning**, never the
  drawing
- no `<img>`, no sprite file, no icon font, no emoji, no external asset — an icon that would need
  one does not get drawn
- colour comes from the tokens (`--brand-ink`, `--positive-ink`), never a literal

`check.py` allows inline `<svg>` and refuses the four ways one could reach outwards (`<image>`,
`<foreignObject>`, `<use>`, `xlink:`). **Six drawings** today, seven instances per language: three
on the home page (device, no picture, envelope), three on the studios page (one pass, separate
mandates, invoice), the envelope again on the contact page. The numbered rail on "So funktioniert
es" is CSS, not an icon — that page carries no SVG at all.

## What the site deliberately does not say

The site makes no claim it cannot back: no testimonial, no client logo, no "trusted by", no invented number, no statistic without a
source, no promise that anything gets removed and no promise of a time. Since 2026-09-18 the site
**says nothing about customers at all**; it says only that the private beta is free and who it is
looking for.

What it *does* say about safety is three short statements, each a fact somebody can check in this
repository rather than a security adjective:

| On the page | Why it is true |
|---|---|
| Your video stays with you — what reaches us is a file of numbers | the fingerprints are computed in the customer's own browser (`frontend/customer/lib/lazyFingerprinting.test.ts`, `frontend/customer/app/api/fingerprints/route.ts`), and server-side hashing is off by default and development-only (`CLOUD_FINGERPRINTING_ENABLED`, `backend/marceline/settings.py`) |
| Kein Bild gespeichert, keins gezeigt | picture bytes live in memory and are dropped once hashed (`backend/matching/media_hash.py`), and the customer UI contains nothing *able* to load a remote image — a test over its own sources says so (`frontend/customer/lib/privacy.test.ts`) |
| You send it yourself; nothing goes out from us | ADR 0035: notice drafts are customer-sent, never transmitted by Marceline |

Those three, plus **the private beta is free**, are the statements that have to stay present and
true however short the page gets; everything else legal belongs to the Impressum and the
Datenschutzerklärung, not to a marketing page.

No absolute or unverifiable security claim appears anywhere — no "100 % sicher", no "unhackable",
no "anonym". None of them could be proved.

Nor does the site say **where or how it is built**: no "wird in [Ort] aufgebaut", no "built and
run in Germany", not even in the footer (operator, 2026-09-18). The Impressum carries the address
because § 5 DDG requires it there; the footer says only that the data sits in the EU.

Marceline never sends a notice — the customer does, from their own account. That is a design
decision (ADR 0035), not a limitation to be marketed around, and the site states
it as such.

No source is ever named on this site and no creator is named. No photograph, screenshot, poster
or frame appears on it either — the only pictures are the abstract icons above, and they depict
nothing that exists.
