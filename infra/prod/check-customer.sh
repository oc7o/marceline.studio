#!/usr/bin/env bash
# Exercise the customer app's production image against a stub Django (ADR 0036).
#
# What this answers, and what no unit test can: the standalone Next server starts, `basePath`
# serves `/app`, the session gate redirects a signed-out visitor, a signed-in visitor gets the
# overview **rendered in the HTML** (not after JavaScript runs), the language comes from the
# request, the middleware's CSP nonce is on the inline bootstrap, and the write path reaches the
# stub with the browser's cookie, CSRF header **and its body** while a cross-site write is refused.
# The upstream is a stub, so it reads the body the way Django does (CONTENT_LENGTH bytes) and
# refuses an empty one; `frontend/customer/lib/relayDjango.test.ts` runs the same relay against a
# real Django, in `make check`, because a stub that accepts anything is what missed the
# chunked-body defect of 2026-09-18. It also
# walks the whole ADR 0023 sign-in through the BFF with a cookie jar, which is the one flow no unit
# test can reach.
#
#   infra/prod/check-customer.sh          # needs docker; builds infra/prod/customer.Dockerfile
#
# On demand only (`make customer-smoke`), not part of `make check`: it builds an image. Exits
# non-zero on the first wrong answer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND="$(cd "$HERE/../../frontend" && pwd)"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.13-slim}"
IMAGE="${CUSTOMER_IMAGE:-marceline-prod-customer:smoke}"
TAG="marceline-customer-check-$$"
WORK="$(mktemp -d)"
FAILURES=0

cleanup() {
  docker rm -f "$TAG-app" "$TAG-web" >/dev/null 2>&1 || true
  docker network rm "$TAG" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# A stub of Django's customer API: one organisation, one original, no findings. `SIGNED_IN`
# switches the session on and off, which is all the gate reads.
cat >"$WORK/stub.py" <<'PY'
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

SESSION = {"on": os.environ.get("SIGNED_IN") == "1"}
ACCOUNT = {
    "username": "smoke@marceline.studio",
    "organisation": {
        "id": "org_" + "1" * 32,
        "name": "Smoke Studio",
        "segment": "professional",
        "role": "owner",
        "created_at": "2026-09-16T10:00:00Z",
    },
    "monitored_sources": "10_plus",
    "can_add_originals": True,
}
ORIGINALS = [
    {
        "id": "orig_" + "7".rjust(32, "0"),
        "title": "Smoke original",
        "duration_s": 600,
        "created_at": "2026-08-01T09:00:00Z",
        "state": "nothing_found",
        "counts": {"confirmed": 0, "removed": 0},
    }
]
def bodies() -> dict[str, tuple[int, object]]:
    on = SESSION["on"]
    return {
        "/api/v1/app/me": (200, ACCOUNT) if on else (401, {"detail": "not authenticated"}),
        "/api/v1/app/originals": (200, ORIGINALS),
        "/api/v1/app/findings": (200, []),
        "/api/v1/app/removals": (200, []),
        "/api/v1/app/fingerprints/submissions": (200, []),
        "/api/v1/app/auth/session": (200, {"authenticated": on, "username": ACCOUNT["username"]}),
    }


class Stub(BaseHTTPRequestHandler):
    def _write(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if self.path == "/api/v1/app/auth/session":
            self.send_header("Set-Cookie", "csrftoken=stub-token; Path=/")
        if self.path == "/api/v1/app/auth/login" and status == 200:
            self.send_header("Set-Cookie", "sessionid=stub-session; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        status, body = bodies().get(self.path, (404, {"detail": "not found"}))
        self._write(status, body)

    def do_POST(self) -> None:
        # Read the body the way Django does on WSGI — CONTENT_LENGTH bytes off the socket, never
        # "whatever arrives" — and refuse an empty one exactly as Django Ninja does. This used to
        # read the body and throw it away, which is why the chunked-transfer-encoding defect of
        # 2026-09-18 passed this check: a stub that accepts an empty body reports success for a
        # relay that delivers nothing. The real proof is `frontend/customer/lib/relayDjango.test.ts`
        # (Django itself, in `make check`); this keeps the image-level check honest too.
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/v1/app/auth/login":
            if body == b"":
                self._write(
                    422,
                    {"detail": [{"type": "missing", "loc": ["body", "payload"], "msg": "Field required"}]},
                )
                return
            # What ADR 0023 does: refuse without the CSRF token, otherwise create the session.
            if self.headers.get("X-CSRFToken") != "stub-token":
                self._write(403, {"detail": "CSRF token missing or incorrect"})
                return
            SESSION["on"] = True
            self._write(200, ACCOUNT)
            return
        self._write(
            200,
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            },
        )

    do_PUT = do_DELETE = do_POST

    def log_message(self, *args: object) -> None:
        pass


HTTPServer(("0.0.0.0", 8000), Stub).serve_forever()
PY
chmod -R a+rX "$WORK"

echo "building $IMAGE"
docker build -q -f "$HERE/customer.Dockerfile" -t "$IMAGE" "$FRONTEND" >/dev/null

pass() { printf 'ok    %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1" >&2
  FAILURES=$((FAILURES + 1))
}

# start_app SIGNED_IN(0|1) -> sets BASE
start_app() {
  docker rm -f "$TAG-app" "$TAG-web" >/dev/null 2>&1 || true
  docker run -d --name "$TAG-web" --network "$TAG" --network-alias web -e SIGNED_IN="$1" \
    -v "$WORK/stub.py:/stub.py:ro" "$PYTHON_IMAGE" python /stub.py >/dev/null
  docker run -d --name "$TAG-app" --network "$TAG" -p 127.0.0.1::3000 \
    -e CUSTOMER_DJANGO_BASE_URL=http://web:8000 \
    -e CUSTOMER_PUBLIC_ORIGIN=https://smoke.test \
    "$IMAGE" >/dev/null
  local port
  port="$(docker port "$TAG-app" 3000/tcp | head -n1 | sed 's/.*://')"
  BASE="http://127.0.0.1:$port"
  local i
  for i in $(seq 1 100); do
    curl -fsS -o /dev/null "$BASE/app/sign-in" 2>/dev/null && return 0
    sleep 0.2
  done
  fail "the customer app did not come up"
  docker logs "$TAG-app" 2>&1 | tail -20 >&2
  return 1
}

request() {
  local path="$1"
  shift
  local out
  out="$(curl -sS --path-as-is -D "$WORK/headers" -o "$WORK/body" -w '%{http_code}' "$@" \
    "$BASE$path")" || true
  STATUS="$out"
  HEADERS="$(tr -d '\r' <"$WORK/headers")"
  BODY="$(cat "$WORK/body")"
}

docker network create "$TAG" >/dev/null

echo "signed out"
start_app 0
request /app/sign-in
[[ "$STATUS" == 200 && "$BODY" == *"Sign in"* ]] && pass "sign-in renders in the HTML" \
  || fail "expected the sign-in form in the HTML, got $STATUS"
request /app
[[ "$STATUS" == 307 || "$STATUS" == 308 ]] && grep -qi '^location: .*/app/sign-in' <<<"$HEADERS" \
  && pass "the gate redirects /app to /app/sign-in" \
  || fail "expected /app to redirect a signed-out visitor to sign-in, got $STATUS"
request /app/removals
[[ "$STATUS" == 307 || "$STATUS" == 308 ]] && pass "the gate redirects every screen" \
  || fail "expected /app/removals to redirect a signed-out visitor, got $STATUS"
# German from the request alone: no cookie, no JavaScript.
request /app/sign-in -H "Accept-Language: de-AT,de;q=0.9,en;q=0.8"
[[ "$BODY" == *'lang="de"'* && "$BODY" == *"Anmelden"* ]] && pass "Accept-Language decides the language" \
  || fail "expected German from Accept-Language alone"
request /app/sign-in -H "Cookie: marceline.language=en" -H "Accept-Language: de-DE"
[[ "$BODY" == *'lang="en"'* ]] && pass "the remembered choice wins over the browser" \
  || fail "expected the marceline.language cookie to win"
grep -qi "^content-security-policy: .*nonce-" <<<"$HEADERS" \
  && pass "the response carries a CSP nonce" || fail "no CSP nonce on the response"
NONCE="$(grep -io "nonce-[0-9a-f]*" <<<"$HEADERS" | head -n1 | cut -d- -f2)"
[[ -n "$NONCE" && "$BODY" == *"nonce=\"$NONCE\""* ]] \
  && pass "the inline bootstrap carries that same nonce" \
  || fail "the page's inline scripts do not carry the CSP nonce"
[[ "$BODY" != *"<img"* && "$BODY" != *"<video"* ]] && pass "no image element in the HTML" \
  || fail "an image element reached the customer HTML"

echo "signed in"
start_app 1
request /app
[[ "$STATUS" == 200 && "$BODY" == *"Smoke original"* ]] \
  && pass "the overview is rendered on the server" \
  || fail "expected the overview's data in the HTML, got $STATUS"
[[ "$BODY" == *"Smoke Studio"* ]] && pass "the organisation is in the HTML" \
  || fail "expected the organisation in the HTML"
# Coverage is the bucket, never the sites (operator 2026-09-16).
[[ "$BODY" != *"site01.example"* ]] && pass "no site of ours is named" || fail "a monitored site leaked into the page"
request /app/originals/not-a-public-id
[[ "$STATUS" == 404 ]] && pass "an id that is not a public id is not found" \
  || fail "expected 404 for a malformed id, got $STATUS"

echo "the write path"
request /app/api/session
[[ "$STATUS" == 200 ]] && grep -qi '^set-cookie: csrftoken=' <<<"$HEADERS" \
  && pass "the session GET passes Django's Set-Cookie back" \
  || fail "expected the csrftoken cookie to reach the browser, got $STATUS"
request /app/api/logout -X POST -H "Cookie: sessionid=abc; csrftoken=stub-token" \
  -H "X-CSRFToken: stub-token" -H "Origin: https://smoke.test" -H "Sec-Fetch-Site: same-origin" \
  -H "X-Forwarded-For: 203.0.113.7" -H "X-Forwarded-Proto: https"
if [[ "$STATUS" == 200 ]] \
  && grep -q '"Cookie": "sessionid=abc; csrftoken=stub-token"' <<<"$BODY" \
  && grep -qi '"X-CSRFToken": "stub-token"' <<<"$BODY" \
  && grep -q '"Origin": "https://smoke.test"' <<<"$BODY"; then
  pass "a write reaches Django with the browser's cookie, token and the public origin"
else
  fail "the forwarded write did not carry what Django judges (status $STATUS): $BODY"
fi
# The login throttle (ADR 0023) reads X-Forwarded-For from the right with LOGIN_TRUSTED_PROXIES=1,
# so this hop must not add an entry of its own — otherwise every attempt counts against this server.
if grep -q '"X-Forwarded-For": "203.0.113.7"' <<<"$BODY"; then
  pass "X-Forwarded-For reaches Django unchanged (one entry: the client)"
else
  fail "X-Forwarded-For was rewritten on the way to Django: $BODY"
fi
grep -q '"X-Forwarded-Proto": "https"' <<<"$BODY" \
  && pass "X-Forwarded-Proto is forwarded, so Django still treats the request as secure" \
  || fail "X-Forwarded-Proto did not reach Django as https: $BODY"
# The body, through the real standalone server and the real BFF. Without Content-Length on the
# forwarded request undici sends the relay's body chunked, the upstream reads CONTENT_LENGTH bytes
# and gets none of it, and every write arrives empty (2026-09-18).
PAYLOAD='{"site":"example"}'
request /app/api/notices -X POST -H "Content-Type: application/json" \
  -H "Cookie: csrftoken=stub-token" -H "X-CSRFToken: stub-token" \
  -H "Origin: https://smoke.test" -H "Sec-Fetch-Site: same-origin" --data "$PAYLOAD"
EXPECTED_SHA="$(printf '%s' "$PAYLOAD" | sha256sum | cut -d' ' -f1)"
if [[ "$STATUS" == 200 ]] && grep -q "\"body_bytes\": ${#PAYLOAD}" <<<"$BODY" \
  && grep -q "\"body_sha256\": \"$EXPECTED_SHA\"" <<<"$BODY"; then
  pass "a relayed write arrives with its body, byte for byte"
else
  fail "the relayed body did not arrive intact (status $STATUS): $BODY"
fi
# `Host` cannot be forwarded (a forbidden header for `fetch`), which is why production's
# DJANGO_ALLOWED_HOSTS names the internal service. Assert the fact rather than wish it away.
grep -q '"host": "web:8000"' <<<"$BODY" \
  && pass "Django sees the internal Host, as DJANGO_ALLOWED_HOSTS expects" \
  || fail "the Host Django saw is not the internal service name: $BODY"
request /app/api/logout -X POST -H "Sec-Fetch-Site: cross-site" -H "Origin: https://evil.example"
[[ "$STATUS" == 403 ]] && pass "a cross-site write is refused before Django" \
  || fail "expected a cross-site write to be refused, got $STATUS"
request /app/api/logout -X POST -H "Origin: https://evil.example"
[[ "$STATUS" == 403 ]] && pass "a foreign Origin is refused" \
  || fail "expected a foreign Origin to be refused, got $STATUS"
request /app/api/rights/partners/not%2Fa%2Fpartner -X DELETE -H "Sec-Fetch-Site: same-origin"
[[ "$STATUS" == 404 ]] && pass "a suspicious path parameter is not forwarded" \
  || fail "expected a suspicious partner id to be refused, got $STATUS"
grep -qi '^access-control-' <<<"$HEADERS" && fail "a CORS header on the app" \
  || pass "no Access-Control-* anywhere"

echo "signing in through the BFF (ADR 0023, with a cookie jar)"
start_app 0
JAR="$WORK/jar"
rm -f "$JAR"
jar_request() {
  local path="$1"
  shift
  local out
  out="$(curl -sS --path-as-is -b "$JAR" -c "$JAR" -D "$WORK/headers" -o "$WORK/body" \
    -w '%{http_code}' "$@" "$BASE$path")" || true
  STATUS="$out"
  HEADERS="$(tr -d '\r' <"$WORK/headers")"
  BODY="$(cat "$WORK/body")"
}
jar_request /app/api/session
grep -q "csrftoken" "$JAR" && pass "the CSRF cookie reached the jar" \
  || fail "no csrftoken cookie after the session GET"
TOKEN="$(awk '/csrftoken/ {print $NF}' "$JAR" | tail -n1)"
# Without the token: Django refuses, and the refusal reaches the browser as a 403 (the form's
# "that form expired" branch, ADR 0023).
jar_request /app/api/login -X POST -H "Content-Type: application/json" \
  -H "Sec-Fetch-Site: same-origin" -H "Origin: https://smoke.test" \
  --data '{"username":"smoke@marceline.studio","password":"x"}'
[[ "$STATUS" == 403 ]] && pass "a login without the CSRF token is refused, as Django refuses it" \
  || fail "expected 403 for a login without the CSRF token, got $STATUS"
jar_request /app/api/login -X POST -H "Content-Type: application/json" \
  -H "X-CSRFToken: $TOKEN" -H "Sec-Fetch-Site: same-origin" -H "Origin: https://smoke.test" \
  --data '{"username":"smoke@marceline.studio","password":"x"}'
[[ "$STATUS" == 200 ]] && pass "the login reaches Django and answers 200" \
  || fail "expected the login to answer 200, got $STATUS: $BODY"
grep -q "sessionid" "$JAR" && pass "Django's session cookie reached the browser" \
  || fail "the session cookie did not reach the browser"
jar_request /app
[[ "$STATUS" == 200 && "$BODY" == *"Smoke original"* ]] \
  && pass "the overview renders for the session the login created" \
  || fail "expected the overview after signing in, got $STATUS"
jar_request /app/api/logout -X POST -H "X-CSRFToken: $TOKEN" -H "Sec-Fetch-Site: same-origin" \
  -H "Origin: https://smoke.test"
[[ "$STATUS" == 200 ]] && pass "the logout reaches Django" || fail "expected the logout to answer 200, got $STATUS"

if ((FAILURES > 0)); then
  echo "$FAILURES check(s) failed" >&2
  exit 1
fi
echo "all customer app checks passed"
