"""The two copies of the marketing site's Caddy configuration stay the same (2026-09-18).

The site block exists twice on purpose:

* `infra/prod/Caddyfile`, snippet `(website_on)` — the customer stack's own Caddy, behind
  `PUBLIC_SITE`. It is **off** until the cut-over that merges the two edges into one process, which
  is decided and planned (operator, 2026-09-19): one host, one public IPv4, one pair of ports 80/443
  and two names — docs/runbooks/production.md §14, docs/plans/first-beta-release.md §9.
* `infra/prod/site/Caddyfile` — the standalone edge that actually serves marceline.studio today,
  because that Caddy binds 80/443 to the host's Tailscale address and issues internal
  certificates for every site block in its file (docs/runbooks/production.md §12).

Two copies drift. A shared snippet file imported by both was the alternative and was rejected: it
would have to be mounted into the customer stack's Caddy as well, and a missing import file is
not a degraded brochure but a Caddy that does not start — the whole customer edge with it. A test
cannot break production at three in the morning; a missing mount can.

So the copies are compared here, directive by directive, with exactly one intended difference:
the customer stack picks its issuer with `import tls_{$CADDY_TLS}` (one variable for every site
in that file, which is why the brochure cannot use it), while the standalone edge has no `tls`
directive at all and takes Caddy's default — public ACME, with the account e-mail from the global
options block.

A static reading; what Caddy really does with either file is `make prod-surface-check` (the
customer one) and one `caddy validate` run against the image the host holds (the standalone one,
infra/prod/site/README.md).
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

MAIN_CADDYFILE = settings.REPO_ROOT / "infra" / "prod" / "Caddyfile"
SITE_CADDYFILE = settings.REPO_ROOT / "infra" / "prod" / "site" / "Caddyfile"
# `{$SITE_DOMAIN}` and `{$CADDY_HSTS:…}` carry braces that are not block braces, so they are
# folded into an opaque token before the braces are counted.
PLACEHOLDER = re.compile(r"\{\$[^{}]*\}")
OPEN, CLOSE = "\x01", "\x02"
# The one intended difference (see the module docstring).
ISSUER_DIRECTIVE = "import tls_"


def _prepared(path: Path) -> str:
    text = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())
    return PLACEHOLDER.sub(lambda m: f"{OPEN}{m.group(0)[2:-1]}{CLOSE}", text)


def _block(text: str, opener: str) -> str:
    """The body of the brace block that follows `opener`."""
    start = text.index(opener)
    first = text.index("{", start)
    depth, index = 0, first
    for index in range(first, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            break
    return text[first + 1 : index]


def _directives(body: str) -> list[str]:
    return [
        " ".join(line.split())
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith(ISSUER_DIRECTIVE)
    ]


def _site_blocks(text: str) -> tuple[list[str], list[str]]:
    apex = _directives(_block(text, f"{OPEN}SITE_DOMAIN{CLOSE}"))
    www = _directives(_block(text, f"{OPEN}SITE_WWW_DOMAIN"))
    return apex, www


def test_the_two_site_blocks_serve_the_same_site() -> None:
    """Every directive of the brochure, in both files, in the same order."""
    main_apex, main_www = _site_blocks(_block(_prepared(MAIN_CADDYFILE), "(website_on)"))
    site_apex, site_www = _site_blocks(_prepared(SITE_CADDYFILE))

    assert main_apex, "the (website_on) snippet has no apex site block any more"
    for label, left, right in (("apex", main_apex, site_apex), ("www", main_www, site_www)):
        difference = [pair for pair in zip(left, right, strict=False) if pair[0] != pair[1]]
        assert not difference and len(left) == len(right), (
            f"the {label} block of infra/prod/Caddyfile and infra/prod/site/Caddyfile have "
            f"drifted apart: {difference or (left, right)}. Change both, or merge them back into "
            "one edge (docs/runbooks/production.md §14)."
        )


def test_the_standalone_edge_has_no_upstream() -> None:
    """It shares a machine with gunicorn and Postgres and must have no way to reach either."""
    text = _prepared(SITE_CADDYFILE)
    for directive in ("reverse_proxy", "php_fastcgi", "forward_auth", "import "):
        assert directive not in text, f"the standalone site edge grew {directive!r}"


def test_the_brochure_is_indexable_on_both_paths() -> None:
    """`X-Robots-Tag: noindex` is right for the customer app and fatal for a brochure."""
    main = _block(_prepared(MAIN_CADDYFILE), "(website_on)")
    for path, text in ((MAIN_CADDYFILE, main), (SITE_CADDYFILE, _prepared(SITE_CADDYFILE))):
        apex = _block(text, f"{OPEN}SITE_DOMAIN{CLOSE}")
        assert "X-Robots-Tag" not in apex, f"{path.name} marks the brochure noindex"


def test_the_standalone_edge_keeps_no_access_log() -> None:
    """website/public/datenschutz.html states that this server writes none."""
    text = _prepared(SITE_CADDYFILE)
    assert not [line for line in text.splitlines() if line.split()[:1] == ["log"]]


def test_the_standalone_edge_takes_its_acme_contact_from_the_environment() -> None:
    """A personal address must never be baked into the repository, and Let's Encrypt needs one
    to warn about a renewal that keeps failing."""
    text = _prepared(SITE_CADDYFILE)
    assert f"email {OPEN}SITE_ACME_EMAIL{CLOSE}" in text
    spelled_out = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert not spelled_out, f"an e-mail address is spelled out: {spelled_out}"


def test_the_standalone_edge_exposes_no_admin_endpoint() -> None:
    assert "admin off" in _prepared(SITE_CADDYFILE)


def test_neither_edge_keeps_an_access_log() -> None:
    """The privacy notice published at marceline.studio promises no access log, and from the
    cut-over on (operator, 2026-09-19) the customer stack's Caddy is the server that has to keep
    that promise — for the brochure's two hostnames as much as for the standalone edge today.

    `log` in Caddy's global options block covers **every** site in the file, so the guard is the
    whole file rather than the site block: an access log for the customer app would have to sit
    inside the `{$APP_DOMAIN}` block and narrow this test on purpose. Nothing else on production
    records a request either — gunicorn runs with access logs off (`infra/prod/web.Dockerfile`).
    """
    for path in (MAIN_CADDYFILE, SITE_CADDYFILE):
        logged = [
            line
            for line in _prepared(path).splitlines()
            if line.split()[:1] == ["log"] or line.split()[:2] == ["log", "{"]
        ]
        assert not logged, (
            f"{path.name} configures an access log ({logged}); website/public/datenschutz.html "
            "states that the server serving the marketing site keeps none"
        )


def test_both_edges_take_their_acme_contact_from_the_environment() -> None:
    """Let's Encrypt needs a contact to warn about renewals that keep failing, and the merged edge
    orders the certificates for both public names — so it needs the account too.

    An empty value is not a missing warning but a dead edge: `email` with no argument fails config
    adaptation ("wrong argument count", measured 2026-09-19), which is why `infra/prod/compose.yaml`
    makes `CADDY_ACME_EMAIL` required and `infra/prod/check_config.py` refuses an empty one.
    """
    for path, variable in (
        (MAIN_CADDYFILE, "CADDY_ACME_EMAIL"),
        (SITE_CADDYFILE, "SITE_ACME_EMAIL"),
    ):
        text = _prepared(path)
        assert f"email {OPEN}{variable}{CLOSE}" in text, path.name
        spelled_out = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
        assert not spelled_out, f"{path.name} spells out an e-mail address: {spelled_out}"
