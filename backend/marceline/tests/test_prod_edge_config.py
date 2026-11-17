"""The production Caddyfile agrees with Django about bodies and internal paths (ADR 0028 and its
write-path amendment).

A static reading of `infra/prod/Caddyfile` — no docker. What Caddy really does with the file is
exercised by `make prod-surface-check` (on demand, needs docker); this test keeps the two numbers
and path lists that must match the Python side from drifting in `make check`.
"""

import re
from datetime import UTC, datetime

from django.conf import settings

from delivery.envelope import (
    FORMAT_OUTBOX_ACK,
    SCHEMA_VERSION,
    generate_private_key,
    seal,
)
from delivery.outbox import MAX_ACK_RESULTS
from marceline.app.fingerprints import MAX_BODY_BYTES
from marceline.app.uploads import CHUNK_BODY_SLACK
from marceline.public_surface import OUTBOX_PATHS, PURGE_PATH

CADDYFILE = settings.REPO_ROOT / "infra" / "prod" / "Caddyfile"
UNITS = {"KB": 1000, "MB": 1000**2, "KiB": 1024, "MiB": 1024**2}
#: One upload chunk plus the slack Django allows for it (ADR 0044 §9): the edge cap and the
#: application cap are the same number, rounded up to the MiB the Caddyfile spells. The chunk size
#: is a setting, so the code default is pinned here rather than read — a bigger chunk on
#: development needs this number and the Caddyfile raised together, deliberately.
CLOUD_UPLOAD_CHUNK_BYTES = 8 * 1024**2
CHUNK_BODY_BYTES = 9 * 1024**2


def _text() -> str:
    return "\n".join(line.split("#", 1)[0] for line in CADDYFILE.read_text().splitlines())


def _block(text: str, opener: str) -> tuple[int, str]:
    """(position, body) of the brace block that follows `opener`."""
    start = text.index(opener)
    depth, index = 0, text.index("{", start)
    for index in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            break
    return start, text[text.index("{", start) + 1 : index]


def _size(value: str) -> int:
    match = re.fullmatch(r"(\d+)([KM]i?B)", value)
    assert match is not None, value
    return int(match.group(1)) * UNITS[match.group(2)]


def _max_size(body: str) -> int:
    [value] = re.findall(r"max_size (\S+)", body)
    return _size(value)


def _directives(body: str) -> set[str]:
    return {" ".join(line.split()) for line in body.splitlines() if line.strip()}


def test_only_the_fingerprint_upload_may_send_a_large_body() -> None:
    """The 16 MiB exception, on the customer API and on nothing else.

    Django's customer API keeps the exception it always had. Since ADR 0036 the customer app is
    also an upstream of its own, and the browser posts the same fingerprint document to that app's
    own path (`POST /app/api/fingerprints`) — so the file carries exactly **two** large-body
    exceptions, one per upstream, and Caddy uses whichever `CUSTOMER_APP` selects. Any third one is
    a hole.
    """
    text = _text()
    _, matcher = _block(text, "@fingerprint_upload {")
    upload_at, upload = _block(text, "handle @fingerprint_upload")
    customer_at, customer = _block(text, "handle @customer_api")

    assert _directives(matcher) == {"method POST", "path /api/v1/app/fingerprints"}
    assert _max_size(upload) == MAX_BODY_BYTES
    # Named matchers are tried in file order: the exception must come before the general rule.
    assert upload_at < customer_at
    assert _max_size(customer) == 1000**2

    # The customer app's own upload route (ADR 0036), inside the `customer_next` snippet.
    _, app_matcher = _block(text, "@customer_fingerprints {")
    app_upload_at, app_upload = _block(text, "handle @customer_fingerprints")
    app_at, app = _block(text, "handle @customer_app")
    assert _directives(app_matcher) == {"method POST", "path /app/api/fingerprints"}
    assert _max_size(app_upload) == MAX_BODY_BYTES
    assert app_upload_at < app_at, "the exception must come before the general /app rule"
    assert _max_size(app) == 1000**2

    sizes = sorted(_size(value) for value in re.findall(r"max_size (\S+)", text))
    large = [size for size in sizes if size > 1000**2]
    assert large == [
        CHUNK_BODY_BYTES,
        CHUNK_BODY_BYTES,
        MAX_BODY_BYTES,
        MAX_BODY_BYTES,
    ], "four exceptions: the fingerprint document and one upload chunk, per customer upstream"


def test_exactly_the_two_chunk_paths_carry_a_chunk() -> None:
    """Cloud fingerprinting's relay (issue #59, ADR 0044 §9, amendment 2026-09-19).

    The bytes cross this host and are never written on it, so the edge does need a rule — but only
    for the one method and path shape that carries a chunk, on each of the two upstreams. The
    declare, complete, list, status and cancel calls beside it stay at 1 MB, and so does everything
    else under `/api/v1/app/*` and `/app/*`.

    `request_body` only *limits* a body: Caddy's reverse proxy streams request bodies, and buffering
    would need `request_buffers`, which this file never sets — asserted below, because a
    `request_buffers` line anywhere here would spool a customer's video to the public host's disk,
    which is the one thing this whole design exists to avoid.
    """
    text = _text()
    _, matcher = _block(text, "@upload_chunk {")
    chunk_at, chunk = _block(text, "handle @upload_chunk")
    customer_at, _ = _block(text, "handle @customer_api")
    assert _directives(matcher) == {"method POST", "path /api/v1/app/uploads/*/chunks/*"}
    assert _max_size(chunk) == CHUNK_BODY_BYTES
    assert chunk_at < customer_at, "the exception must come before the general customer API rule"

    _, app_matcher = _block(text, "@customer_upload_chunks {")
    app_chunk_at, app_chunk = _block(text, "handle @customer_upload_chunks")
    app_at, _ = _block(text, "handle @customer_app")
    assert _directives(app_matcher) == {"method POST", "path /app/api/uploads/*/chunks/*"}
    assert _max_size(app_chunk) == CHUNK_BODY_BYTES
    assert app_chunk_at < app_at, "the exception must come before the general /app rule"

    assert "request_buffers" not in text, "the edge must stream a request body, never spool it"
    assert "/api/v1/app/uploads " not in text, "no rule for the whole upload prefix"
    # The edge must not refuse a body the application would have accepted, or a resumed upload
    # would fail at the edge with no code the app could explain.
    assert CHUNK_BODY_BYTES >= CLOUD_UPLOAD_CHUNK_BYTES + CHUNK_BODY_SLACK


def test_the_customer_app_upstream_is_selected_by_one_variable() -> None:
    """`CUSTOMER_APP` is the cut-over and the rollback (ADR 0036), and it defaults to the SPA.

    Both snippets must exist and exactly one of them must be imported, by that variable: a
    hard-coded upstream would leave no way back, and two imported snippets would give the same path
    to two handlers.
    """
    text = _text()
    for snippet in ("(customer_next)", "(customer_spa)"):
        assert snippet in text, snippet
    imports = re.findall(r"import customer_\S+", text)
    assert imports == ["import customer_{$CUSTOMER_APP:spa}"], imports
    # Whichever is selected, the staff SPA's own asset path belongs to the phase-1 snippet only.
    _, spa = _block(text, "(customer_spa)")
    _, nxt = _block(text, "(customer_next)")
    assert "/assets/*" in spa and "/assets/*" not in nxt
    assert "customer:3000" in nxt and "customer:3000" not in spa


def test_the_next_app_keeps_its_own_content_security_policy() -> None:
    """Caddy must not apply the static policy to the Next.js upstream (ADR 0036).

    The app mints a per-request nonce for the inline bootstrap the App Router streams
    (`frontend/customer/proxy.ts`), and the `header` operations in `(static_csp)` are `defer`red, so
    importing that snippet anywhere inside `(customer_next)` would *replace* the nonce policy with
    one that has no nonce — and every page would render blank in the browser while every test that
    does not run a browser stayed green.

    `make prod-surface-check` proves the live behaviour, but it is on demand; this keeps the file
    from drifting in `make check`, where a regression would otherwise be invisible.
    """
    text = _text()
    _, nxt = _block(text, "(customer_next)")
    assert "static_csp" not in nxt, (
        "the Next.js app sets its own Content-Security-Policy per request; a policy imported here "
        "would overwrite the nonce (see infra/prod/Caddyfile and ADR 0036)"
    )
    # The SPA has no upstream of its own, so it is the one that needs the static policy.
    _, spa = _block(text, "(customer_spa)")
    assert spa.count("import static_csp") == 2, "both SPA handlers keep the static policy"
    # Its client chunks are the app's own path, and only the app can serve them.
    assert "path /app/_next/static/*" in nxt


def test_the_tailscale_site_forwards_exactly_the_internal_paths_django_serves() -> None:
    text = _text()
    _, site = _block(text, "http://:8081")
    _, purge = _block(site, "@purge {")
    _, outbox = _block(site, "@outbox {")

    assert _directives(purge) == {"method POST", f"path {PURGE_PATH}"}
    method, paths = sorted(_directives(outbox))
    assert method == "method POST"
    assert set(paths.split()[1:]) == OUTBOX_PATHS


def test_a_full_acknowledgement_fits_the_bodies_the_outbox_site_accepts() -> None:
    """`MAX_ACK_RESULTS` outcomes must fit Caddy's body cap on `/internal/v1/outbox/ack`.

    A 413 there is not something an acknowledgement recovers from: `inbox.acknowledge` always
    sends the *oldest* settled, unacknowledged outcomes, so an acknowledgement too large for the
    edge is too large on every run for ever — nothing on production would ever settle again, its
    writes would sit pending until the outbox paused and no customer could add anything. The
    settled-unacknowledged set grows by a whole batch whenever an acknowledgement's answer is
    lost after production applied it, so the cap is reachable, not theoretical.

    Measured against the worst case a result can be: the longest submission id, a 64-character
    code, an original's public id and a full timestamp, base64'd inside the envelope.
    """
    text = _text()
    _, site = _block(text, "http://:8081")
    _, outbox = _block(site, "handle @outbox {")
    now = datetime.now(UTC)
    payload = {
        "format": FORMAT_OUTBOX_ACK,
        "schema_version": SCHEMA_VERSION,
        "issued_at": now,
        "nonce": "n" * 64,
        "results": [
            {
                "id": "sub_" + "a" * 32,
                "organisation_id": 2**63 - 1,
                "outcome": "refused",
                "code": "c" * 64,
                "original_id": "orig_" + "b" * 32,
                "applied_at": now,
            }
        ]
        * MAX_ACK_RESULTS,
    }

    wire = seal(payload, generate_private_key())

    assert len(wire) <= _max_size(outbox), (
        f"an acknowledgement of {MAX_ACK_RESULTS} outcomes is {len(wire)} bytes, over the "
        f"{_max_size(outbox)} bytes infra/prod/Caddyfile accepts on the outbox site"
    )


#: Every upstream the production edge may forward to. `web` is the customer API (ADR 0028),
#: `customer` the Next.js app (ADR 0036) and `internal` the Tailscale-only listener.
ALLOWED_UPSTREAMS = frozenset({"web:8000", "customer:3000", "internal:8000"})


def test_the_edge_forwards_to_three_upstreams_and_no_other() -> None:
    """Nothing this file serves may reach a fourth thing.

    The Caddyfile is gaining site blocks that are not the customer edge — the public marketing
    site is a directory of static files on its own hostname, and the whole reason it is safe to
    serve it from this same process is that its block has no upstream at all. That is a property
    of the file, so it is asserted rather than read: a `reverse_proxy` anywhere in it must name one
    of the three services ADR 0028 and ADR 0036 put behind this edge, and a new destination — a
    brochure block that proxies "just the contact form" to Django, a second app, a collector —
    fails here before it is deployed.
    """
    destinations = set(re.findall(r"reverse_proxy\s+(\S+)", _text()))

    assert destinations <= ALLOWED_UPSTREAMS, sorted(destinations - ALLOWED_UPSTREAMS)
    # And every one of the three is still wired up, so this never passes by emptiness.
    assert destinations == ALLOWED_UPSTREAMS


def test_the_web_healthcheck_sends_a_host_django_will_accept() -> None:
    """The health probe's Host header must pass `ALLOWED_HOSTS`, or nothing on production starts.

    `DJANGO_ALLOWED_HOSTS` is `"${APP_DOMAIN},web"` since ADR 0036, and Django validates a Host
    header against `host_validation_re`, which a comma fails: `split_domain_port` answers
    `('', '')`, `validate_host` is False, and `CommonMiddleware` raises `DisallowedHost` before the
    health route runs. The container then never turns healthy and both `customer` and `caddy` wait
    on `service_healthy` for ever — a production stack with no edge and no app, from one header.

    So the probe takes the **first** entry. This test reads the compose file, replays exactly what
    the probe computes, and asks Django itself whether the result is acceptable.
    """
    from django.http.request import split_domain_port, validate_host

    compose = (settings.REPO_ROOT / "infra" / "prod" / "compose.yaml").read_text()

    allowed_hosts = re.search(r"DJANGO_ALLOWED_HOSTS: \$\{APP_DOMAIN[^}]*\}(.*)", compose)
    assert allowed_hosts is not None, "the web service no longer sets DJANGO_ALLOWED_HOSTS"
    # What the variable expands to on a real host, with a plausible domain.
    expanded = f"dash.example.test{allowed_hosts.group(1).strip()}"

    probe = re.search(r"headers=\{'Host': ([^}]+)\}", compose)
    assert probe is not None, "the web healthcheck no longer sends a Host header"
    expression = probe.group(1).strip()
    assert expression != "os.environ['DJANGO_ALLOWED_HOSTS']", (
        "the healthcheck sends the whole DJANGO_ALLOWED_HOSTS value as one Host header; with more "
        "than one entry that is a comma, which Django rejects — see this test's docstring"
    )

    # Replay the probe's own expression against the expanded value, with a stand-in `os` whose
    # environ holds what the container would really see.
    fake_os = type("_os", (), {"environ": {"DJANGO_ALLOWED_HOSTS": expanded}})
    host = eval(expression, {"os": fake_os})
    domain, _port = split_domain_port(host)
    assert domain, f"the probe would send Host: {host!r}, which Django cannot parse"
    assert validate_host(domain, expanded.split(",")), (
        f"the probe would send Host: {host!r}, which is not in ALLOWED_HOSTS ({expanded!r})"
    )
