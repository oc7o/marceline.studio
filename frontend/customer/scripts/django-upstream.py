#!/usr/bin/env python3
"""A real Django, on WSGI, as the upstream for the BFF relay test.

Used by `lib/relayDjango.test.ts` (`make customer-check`, so `make check`). It is deliberately
**Django on WSGI** and not a Node handler or an HTTP stub:

    Django's `WSGIRequest` builds its body stream as `LimitedStream(environ["wsgi.input"],
    int(environ.get("CONTENT_LENGTH") or 0))`.

No `CONTENT_LENGTH`, no body — whatever bytes are actually on the socket. That is the whole of the
2026-09-18 defect: `forwardedHeaders` rebuilds the header set from scratch, `Content-Length` was
not on the allowlist, undici therefore sent the relayed body with chunked transfer-encoding, and
**every** write through the BFF reached Django empty (`422 field required`) — sign-in included. A
stub that consumes whatever arrives accepts the empty body happily and reports success, which is
exactly why the existing `make customer-smoke` stub did not catch it.

No database, no apps, no migrations: `settings.configure()` in this process, one catch-all view
that echoes what arrived, and `CsrfViewMiddleware` so the forwarded `Origin`, `Cookie` and
`X-CSRFToken` are judged by Django's own code rather than by an assertion we wrote ourselves.

    RELAY_CSRF_TRUSTED_ORIGINS=https://customer.test python3 scripts/django-upstream.py

Prints one line of JSON with the port it bound (`{"port": 39417}`) and then serves until killed.
Binds 127.0.0.1 on an ephemeral port: it takes no fixed port, so it can never collide with the
operator's dev server, and it is not reachable from off the machine.
"""

import hashlib
import json
import os
import socketserver
import sys
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import django
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.urls import re_path

# The origins this Django trusts for an unsafe request (`CSRF_TRUSTED_ORIGINS`). The test starts
# one upstream with the public origin and one with nothing, because "the setting was never passed
# into the container" is the other half of the same evening's failure.
TRUSTED_ORIGINS = [o for o in os.environ.get("RELAY_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

CHUNK = 64 * 1024
# The fingerprint upload is the one large body (issue #29): 16 MiB in Django, and Caddy caps
# exactly that path. Room above it, so the cap is Django's answer and not this server's.
MAX_BODY = 24 * 1024 * 1024
# Kept verbatim in the answer only up to here; above it the sha256 and the byte count are the proof.
ECHO_TEXT_LIMIT = 64 * 1024

settings.configure(
    DEBUG=False,
    SECRET_KEY="relay-upstream-test-key-not-a-secret",
    ALLOWED_HOSTS=["*"],
    ROOT_URLCONF=__name__,
    INSTALLED_APPS=[],
    DATABASES={},
    MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware"],
    CSRF_TRUSTED_ORIGINS=TRUSTED_ORIGINS,
    DATA_UPLOAD_MAX_MEMORY_SIZE=MAX_BODY,
    USE_TZ=True,
    DEFAULT_CHARSET="utf-8",
    LOGGING_CONFIG=None,
)
django.setup()


# The token the test sends in the cookie and in `X-CSRFToken`. 32 characters from Django's own
# allowed alphabet, so `CsrfViewMiddleware` treats it as a secret and compares it with itself.
CSRF_TOKEN = "relaytestcsrftoken0123456789abcd"


def read_body(request) -> tuple[int, str, bytes]:
    """Read the body the way Django reads it, in chunks, and never hold more than the limit."""
    digest = hashlib.sha256()
    total = 0
    head = b""
    while True:
        chunk = request.read(CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        if len(head) < ECHO_TEXT_LIMIT:
            head += chunk[: ECHO_TEXT_LIMIT - len(head)]
    return total, digest.hexdigest(), head


def parsed_multipart(request) -> JsonResponse:
    """Let Django's own multipart parser have the body (ADR 0034's proof upload).

    A boundary that did not travel with the body, or a length Django never saw, shows up here as a
    parser error rather than as a field the test forgot to assert.
    """
    files = {
        name: {"name": handle.name, "size": handle.size}
        for name, handle in request.FILES.items()
    }
    return JsonResponse(
        {
            "path": request.path,
            "content_type": request.META.get("CONTENT_TYPE"),
            "content_length": request.META.get("CONTENT_LENGTH"),
            "fields": {name: request.POST[name] for name in request.POST},
            "files": files,
        }
    )


def echo(request, path: str) -> HttpResponse:
    """Say exactly what arrived: the bytes, their length, their type and the headers."""
    if request.path == "/api/v1/app/rights/proofs":
        return parsed_multipart(request)
    total, sha256, head = read_body(request)
    payload = {
        "path": request.path,
        "method": request.method,
        # What the WSGI environ carried, which is what decides whether Django sees a body at all.
        "content_length": request.META.get("CONTENT_LENGTH"),
        "transfer_encoding": request.META.get("HTTP_TRANSFER_ENCODING"),
        "content_type": request.META.get("CONTENT_TYPE"),
        "body_bytes": total,
        "body_sha256": sha256,
        "body_text": head.decode("utf-8", "replace") if total <= ECHO_TEXT_LIMIT else None,
        # Everything Django saw, so the test can assert what is missing as well as what is there.
        "headers": dict(request.headers),
    }

    if request.path.endswith("/pdf"):
        # A stored invoice PDF (ADR 0032): synthetic bytes, to prove the answer's body and its
        # Content-Disposition come back through the relay unchanged.
        body = b"%PDF-1.4\n" + bytes(range(256)) * 4
        response = HttpResponse(body, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="invoice-2026-0001.pdf"'
        # What `marceline/app/invoices.py` really sets. The relay used to drop both on the way
        # out, so the document a customer downloads arrived with no store directive at all.
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
    elif request.path == "/api/v1/app/auth/login" and '"locked@example.test"' in (
        payload["body_text"] or ""
    ):
        # ADR 0023's lockout, so the relay's Retry-After passthrough is exercised for real.
        response = JsonResponse({"detail": "locked_out"}, status=429)
        response["Retry-After"] = "900"
    else:
        response = JsonResponse(payload)

    if request.path == "/api/v1/app/auth/session":
        response.set_cookie("csrftoken", CSRF_TOKEN, path="/", samesite="Lax")
    if request.path == "/api/v1/app/auth/login" and response.status_code == 200:
        # Two cookies at once: `getSetCookie()` has to carry both, not join them into one header.
        response.set_cookie("sessionid", "relay-test-session", path="/", httponly=True)
        response.set_cookie("csrftoken", CSRF_TOKEN, path="/", samesite="Lax")
    if request.path == "/api/v1/app/auth/logout":
        response.delete_cookie("sessionid", path="/")

    # A header of the private side. The relay forwards a fixed list back and must drop this one.
    response["X-Upstream-Note"] = "private side"
    return response


urlpatterns = [re_path(r"^(?P<path>.*)$", echo)]


class Quiet(WSGIRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


class Threaded(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main() -> None:
    from django.core.wsgi import get_wsgi_application

    httpd = make_server(
        "127.0.0.1", 0, get_wsgi_application(), server_class=Threaded, handler_class=Quiet
    )
    print(json.dumps({"port": httpd.server_port}), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
