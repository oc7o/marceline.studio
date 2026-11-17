#!/usr/bin/env bash
# Exercise infra/prod/Caddyfile: what the public edge forwards, serves and refuses (ADR 0028).
#
# Starts the real Caddy image with the real Caddyfile over plain HTTP, stub upstreams named `web`,
# `internal` (the purge listener, ADR 0028) and `customer` (the Next.js app, ADR 0036) that echo
# what they received, and a fake SPA build — nothing from the development stack, no
# database, no Django. Django's own wall is tested separately in
# backend/marceline/tests/test_public_surface.py.
#
# A third Caddy runs with PUBLIC_SITE=on and the real website/public mounted, and asserts the
# marketing site on its own hostname: every page of the sitemap served by its extensionless URL,
# indexable (no X-Robots-Tag, no noindex), reaching no upstream at all, and the customer edge
# unchanged beside it.
#
# The last section is the shape the cut-over gives the host (one Caddy, both names — operator
# 2026-09-19, docs/runbooks/production.md section 14): PUBLIC_SITE=on **and** CUSTOMER_APP=next in
# one process, neither hostname serving the other's surface, and no access log for either — which
# website/public/datenschutz.html promises for the marketing site and this process now has to keep.
#
#   infra/prod/check-surface.sh          # needs docker; pulls caddy:2.10-alpine once
#
# On demand only (`make prod-surface-check`), not part of `make check`: it needs docker and an
# image pull. Exits non-zero on the first wrong answer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CADDY_IMAGE="${CADDY_IMAGE:-caddy:2.10-alpine}"
# The ACME account's contact address. Nothing here orders a certificate (every hostname is
# `http://…`, which turns automatic HTTPS off), but the global options block needs the value to
# adapt at all — see the empty-value case below.
ACME_EMAIL="ops@surface.test"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.13-slim}"
TAG="marceline-surface-check-$$"
WORK="$(mktemp -d)"
FAILURES=0

cleanup() {
  docker rm -f "$TAG-caddy" "$TAG-caddy-next" "$TAG-caddy-site" "$TAG-caddy-both" "$TAG-web" \
    "$TAG-internal" "$TAG-customer" >/dev/null 2>&1 || true
  docker network rm "$TAG" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/spa/assets"
printf '<!doctype html><title>spa-index-marker</title>\n' >"$WORK/spa/index.html"
printf 'console.log("asset")\n' >"$WORK/spa/assets/app.js"
cat >"$WORK/stub.py" <<'PY'
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Echo(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        # Read the whole request body, so a size limit is judged by Caddy, not by a stub that
        # answered before the upload arrived.
        received = len(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        body = json.dumps(
            {"path": self.path, "headers": dict(self.headers), "received": received}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream", os.environ.get("UPSTREAM", "web"))
        if os.environ.get("UPSTREAM") == "customer":
            # What the Next.js middleware does: a per-request nonce Caddy must leave alone.
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'nonce-abc'")
        self.send_header("Server", "stub-upstream")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args: object) -> None:
        pass


import threading

for port in (8000, 3000):
    threading.Thread(target=HTTPServer(("0.0.0.0", port), Echo).serve_forever, daemon=True).start()
threading.Event().wait()
PY
chmod -R a+rX "$WORK"

pass() { printf 'ok    %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1" >&2
  FAILURES=$((FAILURES + 1))
}

# Before anything is started: the file must adapt for both values of CUSTOMER_APP, and only those.
# `import customer_{$CUSTOMER_APP:spa}` resolves at config-adapt time, so a third value is not a
# broken /app — it is a Caddyfile that does not adapt at all, and a container that exits
# instead of serving the site. That is why the cut-over runbook validates the file before it
# recreates Caddy (docs/runbooks/production.md section 11) — asserted here rather than assumed.
echo "the Caddyfile adapts for CUSTOMER_APP, and for nothing else"
validate() {
  docker run --rm -e APP_DOMAIN="http://surface.test" -e CUSTOMER_APP="$1" \
    -e PUBLIC_SITE="${2:-off}" -e SITE_DOMAIN="http://site.test" \
    -e SITE_WWW_DOMAIN="http://www.site.test" -e SITE_CANONICAL_ORIGIN="http://site.test" \
    -e CADDY_TLS="${3:-acme}" -e CADDY_ACME_EMAIL="${ACME_EMAIL}" \
    -v "$HERE/Caddyfile:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1
}
for value in next spa; do
  grep -q "Valid configuration" <<<"$(validate "$value")" \
    && pass "validate   CUSTOMER_APP=$value" \
    || fail "the Caddyfile does not adapt with CUSTOMER_APP=$value"
done
grep -q "File to import not found: customer_nextjs" <<<"$(validate nextjs)" \
  && pass "validate   a misspelled CUSTOMER_APP fails the whole file, not just /app" \
  || fail "expected a misspelled CUSTOMER_APP to fail config adaptation"

# The same for the marketing site (website/), which PUBLIC_SITE adds or leaves out entirely.
for value in off on; do
  grep -q "Valid configuration" <<<"$(validate spa "$value")" \
    && pass "validate   PUBLIC_SITE=$value" \
    || fail "the Caddyfile does not adapt with PUBLIC_SITE=$value"
done
grep -q "File to import not found: website_yes" <<<"$(validate spa yes)" \
  && pass "validate   a misspelled PUBLIC_SITE fails the whole file, not just the brochure" \
  || fail "expected a misspelled PUBLIC_SITE to fail config adaptation"

# And the same for the issuer. One Caddy serves both public names from the cut-over on (operator,
# 2026-09-19), and `import tls_{$CADDY_TLS}` is one variable for every site block in the file: a
# typo takes the brochure and the customer app down together. Which of the two values may be used
# with PUBLIC_SITE=on is a question about the deployment, not the file — `make prod-config` refuses
# `internal` there, because a live public name on Caddy's own CA warns in every browser.
for value in acme internal; do
  grep -q "Valid configuration" <<<"$(validate spa on "$value")" \
    && pass "validate   CADDY_TLS=$value" \
    || fail "the Caddyfile does not adapt with CADDY_TLS=$value"
done
grep -q "File to import not found: tls_letsencrypt" <<<"$(validate spa on letsencrypt)" \
  && pass "validate   a third value for CADDY_TLS fails the whole file" \
  || fail "expected a third CADDY_TLS value to fail config adaptation"

# The ACME account's contact address is required: `email` with an empty value is not a certificate
# without a warning address, it is a file that does not adapt at all. infra/prod/compose.yaml makes
# the variable required for exactly this reason; assert the failure rather than trust the comment.
grep -q "wrong argument count or unexpected line ending after 'email'" \
  <<<"$(ACME_EMAIL= validate spa off)" \
  && pass "validate   an empty CADDY_ACME_EMAIL fails the whole file" \
  || fail "expected an empty CADDY_ACME_EMAIL to fail config adaptation"

docker network create "$TAG" >/dev/null
docker run -d --name "$TAG-web" --network "$TAG" --network-alias web \
  -v "$WORK/stub.py:/stub.py:ro" "$PYTHON_IMAGE" python /stub.py >/dev/null
docker run -d --name "$TAG-internal" --network "$TAG" --network-alias internal -e UPSTREAM=internal \
  -v "$WORK/stub.py:/stub.py:ro" "$PYTHON_IMAGE" python /stub.py >/dev/null
docker run -d --name "$TAG-customer" --network "$TAG" --network-alias customer -e UPSTREAM=customer \
  -v "$WORK/stub.py:/stub.py:ro" "$PYTHON_IMAGE" python /stub.py >/dev/null
docker run -d --name "$TAG-caddy" --network "$TAG" -p 127.0.0.1::80 -p 127.0.0.1::8081 \
  -e APP_DOMAIN="http://surface.test" -e CADDY_ACME_EMAIL="$ACME_EMAIL" \
  -v "$HERE/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$WORK/spa:/srv/spa:ro" \
  "$CADDY_IMAGE" >/dev/null

PORT="$(docker port "$TAG-caddy" 80/tcp | head -n1 | sed 's/.*://')"
BASE="http://127.0.0.1:$PORT"
PURGE_PORT="$(docker port "$TAG-caddy" 8081/tcp | head -n1 | sed 's/.*://')"
for _ in $(seq 1 50); do
  curl -fsS -o /dev/null -H "Host: surface.test" "$BASE/api/v1/health" 2>/dev/null && break
  sleep 0.2
done

# request PATH [curl args…] -> sets STATUS, HEADERS, BODY
request() {
  local path="$1"
  shift
  local out
  # `|| true`: a refused upload may end curl with a send error after the answer arrived; the status
  # it printed is what counts.
  out="$(curl -sS --path-as-is -D "$WORK/headers" -o "$WORK/body" -w '%{http_code}' \
    -H "Host: surface.test" "$@" "$BASE$path")" || true
  STATUS="$out"
  HEADERS="$(tr -d '\r' <"$WORK/headers")"
  BODY="$(cat "$WORK/body")"
}

expect_upstream() {
  request "$1"
  if [[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: web' <<<"$HEADERS"; then
    pass "forwarded  $1"
  else
    fail "expected $1 to reach the customer API, got $STATUS"
  fi
}

expect_refused() {
  local method
  for method in GET POST; do
    request "$1" -X "$method"
    if [[ "$STATUS" == 404 ]] && ! grep -qi '^x-upstream:' <<<"$HEADERS"; then
      pass "refused    $method $1"
    elif grep -qi '^x-upstream:' <<<"$HEADERS" && [[ "$(jq_path)" == /api/v1/app/* ]]; then
      # A path Caddy considers part of the customer API but Django will judge itself (for example
      # an escaped `..`): acceptable only because marceline/public_surface.py refuses it.
      pass "deferred   $method $1 -> Django sees $(jq_path)"
    else
      fail "expected $method $1 to be refused, got $STATUS"
    fi
  done
}

jq_path() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])' <<<"$BODY" 2>/dev/null || true
}

echo "customer API"
expect_upstream /api/v1/health
expect_upstream /api/v1/app/auth/session
expect_upstream /api/v1/app/me
expect_upstream /api/v1/app/cases/1
# The invite redemption (ADR 0052 §9, slice 2): public since 2026-09-19, because the creator holding
# the link is not on the tailnet. It creates nothing there — the last step fills the outbox.
expect_upstream /api/v1/app/invite/abcdefghijklmnop

echo "staff surface and everything else"
for path in /admin/ /admin/login/ /static/admin/css/base.css \
  /api/v1/docs /api/v1/openapi.json /api/v1/auth/me /api/v1/auth/login \
  /api/v1/review/overview /api/v1/review/safety /api/v1/review/counts \
  /api/v1/media/1/image /api/v1/originals /api/v1/apps /api/v1/app \
  /internal/v1/cache/purge /internal/v1/export/organisations/1 \
  /api/v1/app/../review/overview /api/v1/app/%2e%2e/review/overview \
  /api/v1/app/..%2freview/overview /api/v1//review/overview \
  /references /clusters /safety /candidates /originals /index.html /.env /backend/; do
  expect_refused "$path"
done

echo "request body limits (ADR 0028 write path)"
# 1 MB everywhere on the customer API, 16 MiB for exactly POST /api/v1/app/fingerprints.
head -c $((2 * 1024 * 1024)) /dev/zero >"$WORK/2mib"
head -c $((15 * 1024 * 1024)) /dev/zero >"$WORK/15mib"
head -c $((17 * 1024 * 1024)) /dev/zero >"$WORK/17mib"
expect_body() {
  local expected="$1" path="$2" file="$3"
  request "$path" -X POST -H "Content-Type: application/json" --data-binary "@$WORK/$file"
  if [[ "$STATUS" == "$expected" ]]; then
    pass "body       POST $path with $file -> $STATUS"
  else
    fail "expected POST $path with $file to answer $expected, got $STATUS"
  fi
}
expect_body 200 /api/v1/app/fingerprints 2mib
expect_body 200 /api/v1/app/fingerprints 15mib
expect_body 413 /api/v1/app/fingerprints 17mib
expect_body 413 /api/v1/app/fingerprints/client-events 2mib
expect_body 413 /api/v1/app/fingerprints/ 2mib
expect_body 413 /api/v1/app/me 2mib
expect_body 413 /api/v1/app/auth/login 2mib
request /api/v1/app/fingerprints -X POST --data-binary "@$WORK/15mib"
grep -q "\"received\": $((15 * 1024 * 1024))" <<<"$BODY" \
  && pass "body       the whole 15 MiB upload reached the upstream" \
  || fail "the 15 MiB upload did not reach the upstream whole"

echo "customer SPA"
request /app
[[ "$STATUS" == 200 && "$BODY" == *spa-index-marker* ]] && pass "spa        /app" \
  || fail "expected /app to serve index.html, got $STATUS"
request /app/sign-in
[[ "$STATUS" == 200 && "$BODY" == *spa-index-marker* ]] && pass "spa        /app/sign-in" \
  || fail "expected /app/sign-in to serve index.html, got $STATUS"
request /assets/app.js
[[ "$STATUS" == 200 && "$BODY" == *asset* ]] && pass "asset      /assets/app.js" \
  || fail "expected /assets/app.js, got $STATUS"
request /
[[ "$STATUS" == 302 ]] && grep -qi '^location: /app$' <<<"$HEADERS" && pass "redirect   / -> /app" \
  || fail "expected / to redirect to /app, got $STATUS"

echo "cross-origin"
# The edge adds no CORS headers of its own; Django adds none either (test_cross_origin.py), so a
# browser on another origin can neither read a response nor send a non-simple request.
request /api/v1/app/me -X OPTIONS -H "Origin: https://evil.example" \
  -H "Access-Control-Request-Method: POST"
grep -qi '^access-control-' <<<"$HEADERS" && fail "CORS header on a cross-origin preflight" \
  || pass "cors       no Access-Control-* on a cross-origin preflight"
request /app -H "Origin: https://evil.example"
grep -qi '^access-control-' <<<"$HEADERS" && fail "CORS header on the SPA" \
  || pass "cors       no Access-Control-* on the SPA"

echo "headers"
request /api/v1/app/me -H "X-Forwarded-For: 203.0.113.66" -H "X-Forwarded-Proto: https"
for header in 'strict-transport-security: max-age=' "content-security-policy: default-src 'self'" \
  'x-frame-options: DENY' 'referrer-policy: no-referrer' 'x-content-type-options: nosniff'; do
  grep -qi "^$header" <<<"$HEADERS" && pass "header     $header" || fail "missing header $header"
done
grep -qi '^server:' <<<"$HEADERS" && fail "Server header leaks" || pass "header     no Server"
[[ "$BODY" != *203.0.113.66* ]] && pass "proxy      client X-Forwarded-For not trusted" \
  || fail "a client-supplied X-Forwarded-For reached the upstream"
grep -q '"X-Forwarded-Proto": "http"' <<<"$BODY" && pass "proxy      client X-Forwarded-Proto replaced" \
  || fail "a client-supplied X-Forwarded-Proto reached the upstream"

echo "purge site (Tailscale-only port 8081)"
purge_request() {
  STATUS="$(curl -sS --path-as-is -D "$WORK/headers" -o "$WORK/body" -w '%{http_code}' \
    "$@")" || true
  HEADERS="$(tr -d '\r' <"$WORK/headers")"
}
purge_request -X POST "http://127.0.0.1:$PURGE_PORT/internal/v1/cache/purge"
[[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: internal' <<<"$HEADERS" \
  && pass "forwarded  POST :8081/internal/v1/cache/purge -> internal" \
  || fail "expected the purge to reach the internal listener, got $STATUS"
for outbox_path in /internal/v1/outbox/pull /internal/v1/outbox/ack; do
  purge_request -X POST "http://127.0.0.1:$PURGE_PORT$outbox_path"
  [[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: internal' <<<"$HEADERS" \
    && pass "forwarded  POST :8081$outbox_path -> internal" \
    || fail "expected POST :8081$outbox_path to reach the internal listener, got $STATUS"
done
purge_request -X POST --data-binary "@$WORK/2mib" "http://127.0.0.1:$PURGE_PORT/internal/v1/outbox/ack"
[[ "$STATUS" == 413 ]] && pass "body       :8081 outbox requests stay small" \
  || fail "expected a 2 MiB outbox acknowledgement to be refused, got $STATUS"
for outbox_path in /internal/v1/outbox/pull /internal/v1/outbox/ack; do
  request "$outbox_path" -X POST
  [[ "$STATUS" == 404 ]] && ! grep -qi '^x-upstream:' <<<"$HEADERS" \
    && pass "refused    public POST $outbox_path" \
    || fail "expected the public site to refuse POST $outbox_path, got $STATUS"
done
for probe in "GET /internal/v1/cache/purge" "POST /api/v1/app/me" "GET /api/v1/health" \
  "GET /internal/v1/outbox/pull" "POST /internal/v1/outbox" "POST /api/v1/app/fingerprints" \
  "GET /internal/v1/export/accounts" "POST /internal/v1/cache/purge/../../export/accounts" \
  "GET /admin/"; do
  purge_request -X "${probe%% *}" "http://127.0.0.1:$PURGE_PORT${probe#* }"
  [[ "$STATUS" == 404 ]] && ! grep -qi '^x-upstream:' <<<"$HEADERS" \
    && pass "refused    :8081 $probe" || fail "expected :8081 $probe to be refused, got $STATUS"
done

echo "customer app on the Next.js upstream (CUSTOMER_APP=next, ADR 0036)"
docker run -d --name "$TAG-caddy-next" --network "$TAG" -p 127.0.0.1::80 \
  -e APP_DOMAIN="http://surface.test" -e CUSTOMER_APP=next -e CADDY_ACME_EMAIL="$ACME_EMAIL" \
  -v "$HERE/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$WORK/spa:/srv/spa:ro" \
  "$CADDY_IMAGE" >/dev/null
NEXT_PORT="$(docker port "$TAG-caddy-next" 80/tcp | head -n1 | sed 's/.*://')"
NEXT_BASE="http://127.0.0.1:$NEXT_PORT"
for _ in $(seq 1 50); do
  curl -fsS -o /dev/null -H "Host: surface.test" "$NEXT_BASE/api/v1/health" 2>/dev/null && break
  sleep 0.2
done

next_request() {
  local path="$1"
  shift
  local out
  out="$(curl -sS --path-as-is -D "$WORK/headers" -o "$WORK/body" -w '%{http_code}' \
    -H "Host: surface.test" "$@" "$NEXT_BASE$path")" || true
  STATUS="$out"
  HEADERS="$(tr -d '\r' <"$WORK/headers")"
  BODY="$(cat "$WORK/body")"
}

for path in /app /app/sign-in /app/removals /app/originals/orig_1 /app/_next/static/chunks/main.js; do
  next_request "$path"
  [[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: customer' <<<"$HEADERS" \
    && pass "next       $path -> the customer app" \
    || fail "expected $path to reach the customer app, got $STATUS"
done

# The staff surface is still refused, and the SPA's /assets/* is not served by this upstream.
for path in /admin/ /references /api/v1/review/overview /assets/app.js /index.html; do
  for method in GET POST; do
    next_request "$path" -X "$method"
    [[ "$STATUS" == 404 ]] && ! grep -qi '^x-upstream:' <<<"$HEADERS" \
      && pass "next       refused $method $path" \
      || fail "expected $method $path to be refused with CUSTOMER_APP=next, got $STATUS"
  done
done

# The customer API still goes to Django, unchanged.
next_request /api/v1/app/me
[[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: web' <<<"$HEADERS" \
  && pass "next       /api/v1/app/me still reaches Django" \
  || fail "expected /api/v1/app/me to reach Django with CUSTOMER_APP=next, got $STATUS"

# 16 MiB for exactly POST /app/api/fingerprints; 1 MB for every other /app path.
next_body() {
  local expected="$1" path="$2" file="$3"
  next_request "$path" -X POST -H "Content-Type: application/json" --data-binary "@$WORK/$file"
  [[ "$STATUS" == "$expected" ]] && pass "next       body POST $path with $file -> $STATUS" \
    || fail "expected POST $path with $file to answer $expected, got $STATUS"
}
next_body 200 /app/api/fingerprints 15mib
next_body 413 /app/api/fingerprints 17mib
next_body 413 /app/api/fingerprints/client-events 2mib
next_body 413 /app/api/login 2mib

# A write of ordinary size reaches the app, not just a refusal: the size checks above all end at
# Caddy, so on their own they would still pass if /app/api/* were routed nowhere.
next_request /app/api/login -X POST -H "Content-Type: application/json" --data '{}'
[[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: customer' <<<"$HEADERS" \
  && pass "next       POST /app/api/login reaches the customer app" \
  || fail "expected POST /app/api/login to reach the customer app, got $STATUS"

# Proxy headers on the app's own paths (ADR 0036). This matters more here than on the customer API:
# the BFF forwards X-Forwarded-For to Django **verbatim** so that ADR 0023's login throttle still
# resolves the customer (LOGIN_TRUSTED_PROXIES stays 1). If Caddy honoured a client's own header on
# /app, every customer could pick the address their failed logins are counted against.
for path in /app /app/sign-in /app/api/login; do
  next_request "$path" -H "X-Forwarded-For: 203.0.113.66" -H "X-Forwarded-Proto: https" \
    -H "Content-Type: application/json" --data '{}' -X POST
  if [[ "$BODY" != *203.0.113.66* ]]; then
    pass "next       client X-Forwarded-For not trusted on $path"
  else
    fail "a client-supplied X-Forwarded-For reached the customer app on $path"
  fi
  grep -q '"X-Forwarded-Proto": "http"' <<<"$BODY" \
    && pass "next       client X-Forwarded-Proto replaced on $path" \
    || fail "a client-supplied X-Forwarded-Proto reached the customer app on $path"
done

# The app's own per-request CSP survives: Caddy adds the rest of the security headers but must not
# replace the policy with the static one (a nonce it did not mint would break the page).
next_request /app
grep -qi "^content-security-policy: default-src 'self'; script-src 'nonce-abc'" <<<"$HEADERS" \
  && pass "next       the app's own Content-Security-Policy is not overwritten" \
  || fail "Caddy replaced the customer app's Content-Security-Policy"
for header in 'strict-transport-security: max-age=' 'x-frame-options: DENY' \
  'referrer-policy: no-referrer' 'x-content-type-options: nosniff'; do
  grep -qi "^$header" <<<"$HEADERS" && pass "next       header $header" \
    || fail "missing header $header with CUSTOMER_APP=next"
done
grep -qi '^server:' <<<"$HEADERS" && fail "Server header leaks with CUSTOMER_APP=next" \
  || pass "next       no Server"

# The immutable build output is cacheable; a rendered page is not.
next_request /app/_next/static/chunks/main.js
grep -qi '^cache-control: public, max-age=31536000, immutable' <<<"$HEADERS" \
  && pass "next       /app/_next/static is immutable" \
  || fail "expected an immutable Cache-Control on /app/_next/static"

next_request /app -H "Origin: https://evil.example"
grep -qi '^access-control-' <<<"$HEADERS" && fail "CORS header on the customer app" \
  || pass "next       no Access-Control-* on the customer app"

echo "the public marketing site (PUBLIC_SITE=on, website/)"
# A third site block on its own hostname, serving website/public as static files with no upstream.
# The real directory is mounted, so this also proves the files the operator rsyncs are servable by
# the `try_files` rules website/check.py validates links against.
docker run -d --name "$TAG-caddy-site" --network "$TAG" -p 127.0.0.1::80 \
  -e APP_DOMAIN="http://surface.test" -e CUSTOMER_APP=spa -e PUBLIC_SITE=on \
  -e CADDY_ACME_EMAIL="$ACME_EMAIL" \
  -e SITE_DOMAIN="http://site.test" -e SITE_WWW_DOMAIN="http://www.site.test" \
  -e SITE_CANONICAL_ORIGIN="http://site.test" \
  -v "$HERE/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$WORK/spa:/srv/spa:ro" \
  -v "$HERE/../../website/public:/srv/site:ro" \
  "$CADDY_IMAGE" >/dev/null
SITE_PORT="$(docker port "$TAG-caddy-site" 80/tcp | head -n1 | sed 's/.*://')"
SITE_BASE="http://127.0.0.1:$SITE_PORT"
for _ in $(seq 1 50); do
  curl -fsS -o /dev/null -H "Host: site.test" "$SITE_BASE/" 2>/dev/null && break
  sleep 0.2
done

site_request() {
  local host="$1" path="$2"
  shift 2
  local out
  out="$(curl -sS --path-as-is -D "$WORK/headers" -o "$WORK/body" -w '%{http_code}' \
    -H "Host: $host" "$@" "$SITE_BASE$path")" || true
  STATUS="$out"
  HEADERS="$(tr -d '\r' <"$WORK/headers")"
  # The fonts are binary, and a command substitution cannot hold a null byte; the body matters
  # only for the HTML pages, so strip them rather than warn on every woff2.
  BODY="$(tr -d '\0' <"$WORK/body")"
}

# Every page the sitemap lists, by its extensionless URL, with the right media type.
for path in / /so-funktioniert-es /fuer-studios-und-agenturen /kontakt /impressum /datenschutz \
  /en/ /en/how-it-works /en/for-studios-and-agencies /en/contact /en/imprint /en/privacy; do
  site_request site.test "$path"
  if [[ "$STATUS" == 200 ]] && grep -qi '^content-type: text/html' <<<"$HEADERS"; then
    pass "site       $path"
  else
    fail "expected $path to serve HTML with 200, got $STATUS"
  fi
done

# The legal pages must be indexable: no noindex in the page and no X-Robots-Tag from the edge.
# The customer edge sends `X-Robots-Tag: noindex, nofollow` — right for the app, fatal here, which
# is why this site block does not import security_headers.
for path in /impressum /datenschutz /en/imprint /en/privacy; do
  site_request site.test "$path"
  if grep -qi '^x-robots-tag:' <<<"$HEADERS" || grep -qi 'name="robots"[^>]*noindex' <<<"$BODY"; then
    fail "$path is marked noindex; the brochure exists to be found"
  else
    pass "site       $path is indexable"
  fi
done

site_request site.test /assets/site.css
[[ "$STATUS" == 200 ]] && grep -qi '^content-type: text/css' <<<"$HEADERS" \
  && pass "site       /assets/site.css" || fail "expected the stylesheet, got $STATUS"
site_request site.test /sitemap.xml
[[ "$STATUS" == 200 ]] && pass "site       /sitemap.xml" || fail "expected the sitemap, got $STATUS"
site_request site.test /robots.txt
[[ "$STATUS" == 200 ]] && pass "site       /robots.txt" || fail "expected robots.txt, got $STATUS"

site_request site.test /assets/fonts/instrument-sans-latin.c10e9d3f.woff2
grep -qi '^cache-control: public, max-age=31536000, immutable' <<<"$HEADERS" \
  && pass "site       content-hashed fonts are immutable" \
  || fail "expected an immutable Cache-Control on a content-hashed font"

# Nothing on this hostname may reach an upstream: there is no reverse_proxy in the site block at
# all, so the staff surface, the customer API and the app are simply not there.
for path in /admin/ /api/v1/health /api/v1/app/me /api/v1/review/overview /app /app/sign-in \
  /internal/v1/cache/purge /references /assets/app.js /../Caddyfile; do
  for method in GET POST; do
    site_request site.test "$path" -X "$method"
    if [[ "$STATUS" == 404 ]] && ! grep -qi '^x-upstream:' <<<"$HEADERS"; then
      pass "site       refused $method $path"
    else
      fail "expected $method $path to be refused on the website host, got $STATUS"
    fi
  done
done

site_request site.test /
for header in 'strict-transport-security: max-age=' 'x-frame-options: DENY' \
  'referrer-policy: no-referrer' 'x-content-type-options: nosniff' \
  "content-security-policy: default-src 'none'"; do
  grep -qi "^$header" <<<"$HEADERS" && pass "site       header $header" \
    || fail "missing header $header on the website"
done
grep -qi '^server:' <<<"$HEADERS" && fail "Server header leaks on the website" \
  || pass "site       no Server"

site_request www.site.test /kontakt
[[ "$STATUS" == 301 ]] && grep -qi '^location: http://site.test/kontakt' <<<"$HEADERS" \
  && pass "site       www redirects to the canonical name, path kept" \
  || fail "expected www to redirect to the apex, got $STATUS"

# And the customer edge on the same Caddy is untouched by any of it.
site_request surface.test /api/v1/health
[[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: web' <<<"$HEADERS" \
  && pass "site       the customer API still answers with PUBLIC_SITE=on" \
  || fail "expected the customer API to be unaffected by PUBLIC_SITE=on, got $STATUS"
site_request surface.test /impressum
[[ "$STATUS" == 404 ]] && pass "site       the brochure is not served on the app hostname" \
  || fail "expected /impressum to 404 on the app hostname, got $STATUS"

# A 404 on the website host is the plain text `handle_errors` responds with, not Caddy's own error
# page and not a page of the brochure: website/check.py is supposed to have caught every broken
# internal link before the files were ever copied to the host, so a 404 here is a visitor's typo.
site_request site.test /keine-solche-seite
[[ "$STATUS" == 404 && "$BODY" == "404 Not Found" ]] \
  && pass "site       an unknown path answers the plain 404 of handle_errors" \
  || fail "expected the handle_errors 404 body on the website host, got $STATUS ${BODY:0:40}"

echo "one Caddy, both names (the cut-over shape: PUBLIC_SITE=on, CUSTOMER_APP=next)"
# What the host will really run after docs/runbooks/production.md section 14: the marketing site on
# its own hostname and the Next.js customer app on APP_DOMAIN, in one process. The section above
# proves the brochure beside the *SPA*; this one proves it beside the app that will actually be
# there, because `import customer_next` and `import website_on` are two independent imports and
# nothing else asserts that both are in the file at once.
docker run -d --name "$TAG-caddy-both" --network "$TAG" -p 127.0.0.1::80 \
  -e APP_DOMAIN="http://surface.test" -e CUSTOMER_APP=next -e PUBLIC_SITE=on \
  -e CADDY_ACME_EMAIL="$ACME_EMAIL" \
  -e SITE_DOMAIN="http://site.test" -e SITE_WWW_DOMAIN="http://www.site.test" \
  -e SITE_CANONICAL_ORIGIN="http://site.test" \
  -v "$HERE/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$WORK/spa:/srv/spa:ro" \
  -v "$HERE/../../website/public:/srv/site:ro" \
  "$CADDY_IMAGE" >/dev/null
BOTH_PORT="$(docker port "$TAG-caddy-both" 80/tcp | head -n1 | sed 's/.*://')"
# `site_request` takes a Host header and reads SITE_BASE, so pointing that at this container reuses
# it as it is; the previous section is finished with it.
SITE_BASE="http://127.0.0.1:$BOTH_PORT"
for _ in $(seq 1 50); do
  curl -fsS -o /dev/null -H "Host: site.test" "$SITE_BASE/" 2>/dev/null && break
  sleep 0.2
done

# The brochure, by its clean URLs, with no upstream behind it.
for path in / /datenschutz /impressum /en/privacy; do
  site_request site.test "$path"
  [[ "$STATUS" == 200 ]] && grep -qi '^content-type: text/html' <<<"$HEADERS" \
    && ! grep -qi '^x-upstream:' <<<"$HEADERS" \
    && pass "both       $path is the brochure, no upstream" \
    || fail "expected $path to be served as HTML with no upstream, got $STATUS"
done
site_request site.test /datenschutz
grep -qi '^x-robots-tag:' <<<"$HEADERS" \
  && fail "the brochure is noindex beside the Next.js app" \
  || pass "both       the brochure stays indexable beside the Next.js app"
grep -qi "^content-security-policy: default-src 'none'" <<<"$HEADERS" \
  && pass "both       the brochure keeps its own strict policy" \
  || fail "the brochure's Content-Security-Policy changed beside the Next.js app"
site_request www.site.test /datenschutz
[[ "$STATUS" == 301 ]] && grep -qi '^location: http://site.test/datenschutz' <<<"$HEADERS" \
  && pass "both       www still redirects to the apex, path kept" \
  || fail "expected www to redirect to the apex, got $STATUS"

# The customer app and its API, on the other hostname, unchanged.
site_request surface.test /app/sign-in
[[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: customer' <<<"$HEADERS" \
  && pass "both       /app reaches the customer app" \
  || fail "expected /app to reach the customer app with PUBLIC_SITE=on, got $STATUS"
site_request surface.test /api/v1/app/me
[[ "$STATUS" == 200 ]] && grep -qi '^x-upstream: web' <<<"$HEADERS" \
  && pass "both       the customer API reaches Django" \
  || fail "expected the customer API to reach Django with PUBLIC_SITE=on, got $STATUS"
# Neither name may serve the other's surface.
for path in /app /app/sign-in /api/v1/health /admin/; do
  site_request site.test "$path"
  [[ "$STATUS" == 404 ]] && ! grep -qi '^x-upstream:' <<<"$HEADERS" \
    && pass "both       the website host refuses $path" \
    || fail "expected $path to be refused on the website host, got $STATUS"
done
site_request surface.test /datenschutz
[[ "$STATUS" == 404 ]] && pass "both       the app host does not serve the brochure" \
  || fail "expected /datenschutz to 404 on the app hostname, got $STATUS"

# **No access log.** website/public/datenschutz.html states that the server serving the marketing
# site keeps none, and from the cut-over on this is that server. The Caddyfile has no `log`
# directive (asserted statically in backend/marceline/tests/test_site_edge_config.py); here the
# running container is asked directly, after it has answered requests for both hostnames.
site_request site.test /datenschutz
site_request surface.test /app/sign-in
CADDY_LOG="$(docker logs "$TAG-caddy-both" 2>&1)"
if grep -qE 'handled request|http\.log\.access|/datenschutz' <<<"$CADDY_LOG"; then
  fail "the merged edge wrote an access log line; website/public/datenschutz.html says it keeps none"
else
  pass "both       no access log for either hostname (docker logs is errors only)"
fi

if ((FAILURES > 0)); then
  echo "$FAILURES check(s) failed" >&2
  exit 1
fi
echo "all surface checks passed"
