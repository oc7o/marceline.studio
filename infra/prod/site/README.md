# The standalone edge for marceline.studio

One Caddy container, serving `website/public` as static files on the production VPS's **public**
address, in **its own compose project** (`marceline-site`). It has no upstream, no database, no
application code and no route to the customer stack: `docker compose -p marceline-site down`
removes the public marketing site and changes nothing else on the machine.

```
infra/prod/site/
  compose.yaml        the project: one service, IPv4 ports
  compose.ipv6.yaml   the IPv6 ports, as a second -f file — not in the live deploy yet, no AAAA
                       record (the host itself has a public IPv6)
  Caddyfile           the site block, mounted read-only
  check_config.py     the invariants `make prod-config` enforces (standard library only)
  site.env.example    the template for infra/prod/site/.env on the host
```

## Why a second Caddy

The customer stack's Caddy already has the same site block, behind `PUBLIC_SITE` in
`infra/prod/Caddyfile`, and it cannot be used yet (measured on the host, 2026-09-18):

- it publishes 80/443 on the host's **Tailscale** address (`CADDY_BIND=100.64.0.2`), so
  nothing it serves is reachable from the internet;
- it issues **internal** certificates (`CADDY_TLS=internal`), and that variable is used by *every*
  site block in that file, so switching it for the brochure also switches
  `dash.marceline.studio` — whose A record is a `100.x` Tailscale address that Let's Encrypt can
  never validate, which would mean repeated failed ACME orders and a customer app with no usable
  certificate.

Serving the brochure from that process therefore means publishing the customer edge and changing
its issuer at the same time: a launch, not a website change. So the brochure gets 40 MB of its own
(operator's decision, 2026-09-18), and the two are merged back into one Caddy when the customer
app launches publicly — `PUBLIC_SITE=on` is exactly that merge.

**This stack is being retired.** The host has one public IPv4 and one pair of ports 80/443, and the
customer app needs them, so one Caddy serves both names from the cut-over on (operator,
2026-09-19). The procedure, including the certificate copy that keeps the live site's downtime to
seconds, is `docs/runbooks/production.md` **§14**. Until it has run, everything below is current: this stack
holds the public ports and the customer stack's `PUBLIC_SITE` stays `off`. Afterwards this
directory stays in the repository as the documented rollback.

Both copies of the site block are kept identical by
`backend/marceline/tests/test_site_edge_config.py`: it compares them directive by directive and
fails on any substantive difference. A shared snippet file imported by both was the alternative
and was rejected — it would have to be mounted into the customer stack's Caddy too, and a missing
import file is not a degraded brochure but a Caddy that does not start, taking the customer app
with it. A test cannot break production; a missing mount can.

## What it may and may not be

`make prod-config` renders this project against `site.env.example` and runs `check_config.py` over
it (both with and without the IPv6 file). It refuses:

- more than one service, or anything but `caddy`;
- a published port other than 80 and 443, a **wildcard** bind (a docker-published port bypasses
  the host firewall, so `0.0.0.0` publishes on the tailnet as well) or a **Tailscale** address
  (`100.64.0.0/10`, `fd7a:115c:a1e0::/48` — that mistake is why this stack exists);
- a writable mount of the site or the Caddyfile, and `/data` or `/config` that is not a **named
  volume** (certificates that do not survive a recreate order a new one on every deploy, and
  Let's Encrypt allows five duplicates a week);
- an application network (`edge`, `data`) or any external network, a `network_mode`, a
  `depends_on`, a writable root filesystem, a capability beyond `NET_BIND_SERVICE`, a missing or
  oversized memory cap, a `restart` policy that needs a human, a `build`, an image tagged
  `latest`, or any `DJANGO_*` / `POSTGRES_*` / `CACHE_*` / `OUTBOX_*` / `CUSTOMER_*` / `SENTRY_*`
  variable;
- a `reverse_proxy`, `forward_auth`, `php_fastcgi`, `import` or access-log line in the Caddyfile,
  or a missing `admin off` — it reads the very file the compose file mounts, when it is there.

There is deliberately **no access log**: `website/public/datenschutz.html` says this server keeps
none, and the checker fails if a `log` directive appears. Errors still go to stderr
(`docker compose logs`).

## Which image

`SITE_IMAGE_TAG` has **no default**: the production VPS builds and pulls nothing, so the tag must
be one it already holds. Use the tag the customer stack is running —

```bash
docker compose -f /srv/marceline/infra/prod/compose.yaml ps        # the IMAGE column
grep '^MARCELINE_RELEASE=' /srv/marceline/infra/prod/.env          # the same value
docker images marceline-prod-caddy --format '{{.Repository}}:{{.Tag}}'
```

— and set `SITE_IMAGE_TAG` to it. The image is the customer edge's (`marceline-prod-caddy`, Caddy
2.10 on Alpine plus the Vite SPA build at `/srv/spa`). Nothing here routes to `/srv/spa`: this
Caddyfile's only root is `/srv/site`. A new release does not need this stack recreated; when it is,
raise the tag to one the host holds.

## Checking it without a host

```bash
make prod-config                     # renders both variants, runs the invariants

# the real Caddy binary against this file, no ports, no ACME, nothing started:
docker run --rm -e SITE_DOMAIN=marceline.studio -e SITE_WWW_DOMAIN=www.marceline.studio \
  -e SITE_CANONICAL_ORIGIN=https://marceline.studio -e SITE_ACME_EMAIL=contact@marceline.studio \
  -v "$PWD/infra/prod/site/Caddyfile:/etc/caddy/Caddyfile:ro" \
  marceline-prod-caddy:<tag> caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

`make prod-surface-check` still exercises the *customer* stack's copy of the block against the
real `website/public` directory, which is the same 12 pages by the same `try_files` rules.

## Deploying it

`docs/runbooks/production.md` §12 is the runbook (ship the files, `.env`, validate, `up -d`, prove
it from outside, rollback) — live there since 2026-09-18 19:04 UTC. In short, on a host with
`rsync`:

```bash
rsync -a --delete website/public/ deploy@<prod>:/srv/marceline/site/          # from a checkout
rsync -a --delete infra/prod/site/ deploy@<prod>:/srv/marceline/infra/site/   # this directory
# on the host, in /srv/marceline/infra/site: cp site.env.example .env; chmod 600 .env; edit it
docker compose -f compose.yaml --env-file .env config --quiet   # add -f compose.ipv6.yaml once
docker compose -f compose.yaml --env-file .env up -d            # an AAAA record exists (below)
```

This development VPS has no `rsync` client; `docs/runbooks/production.md` §12 gives the `tar`
form instead, plus the safe way to remove a file the repository no longer has (`tar` has no
`--delete`, and the site directory is bind-mounted read-only into the running container by
inode — swapping the directory itself would break that mount, so a removed file's replacement has
to be synced into the live directory in place, with the production host's own `rsync`).

**IPv6.** `compose.ipv6.yaml` is not part of today's deploy: the production VPS has a public IPv6
address, but `marceline.studio` has no AAAA record yet. Add it as a second `-f` file, on **every**
command against this project, once that record exists (`site.env.example`'s `SITE_BIND6` stays
commented out until then).

Publishing an edit afterwards is shipping the files alone — they are mounted, so there is
nothing to restart. **Rollback** is `docker compose -p marceline-site down`: the certificates stay
in the named volumes, so bringing it back costs no ACME order, and the customer stack never
notices either way.
