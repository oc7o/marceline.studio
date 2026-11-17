#!/usr/bin/env python3
"""Print the `ONBOARDING_DOCUMENTS` entry for one published legal document.

    python3 website/publish_document.py --id pilot_agreement --version 2026-10-01 \
        website/public/dokumente/pilotvereinbarung-2026-10-01.html

prints exactly one line on stdout:

    pilot_agreement:2026-10-01:<sha256>:https://marceline.studio/dokumente/pilotvereinbarung-2026-10-01

and everything else on stderr, so the line can be pasted into `.env` or captured
in a shell variable. `docs/runbooks/onboarding-documents.md` is the procedure this
is one step of; `--check` re-verifies an entry later.

**What the digest covers.** The bytes of the file, exactly as the server hands
them to a browser — no normalisation of line endings, no stripping, no HTML
parsing. `infra/prod/site/` serves `website/public/` with Caddy's `file_server`,
so the file on disk *is* the response body.

**Why the digest is worth anything.** `backend/accounts/onboarding.py` parses this
entry and `accounts.invites.record_acceptances` stores id, version, digest and URL
on the row that says a creator accepted the document (ADR 0052 § 3). Nothing
fetches the URL and re-hashes it: the digest is a promise made here. Which is the
point — a stored digest that matches nothing published any more is itself the
finding, and this script is the only place that digest is ever computed.

Standard library only, no network, like `check.py` next to it.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC = ROOT / "public"

#: The origin the site is served on. Same constant as `check.py`'s `OWN_ORIGIN`;
#: a document URL on any other origin is refused.
OWN_ORIGIN = "https://marceline.studio"

#: The document ids the invite flow knows, in its own order. This list must equal
#: `REQUIRED_DOCUMENTS` in `backend/accounts/onboarding.py` — a test asserts it
#: (`backend/marceline/tests/test_publish_document.py`). A new document is a new
#: id there first, never a rename of one of these: a stored acceptance names one.
KNOWN_DOCUMENT_IDS = ("pilot_agreement", "service_privacy_notice")

#: A version is one field of a colon-separated entry, so it may hold neither a
#: colon nor a comma (the entry separator) nor whitespace. Dates are what the
#: drafts use, and a date sorts.
VERSION_SHAPE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")

#: Markers that say the file is still working material. Any of them in a file
#: refuses the publish — they are the difference between the drafts under
#: `website/drafts/` and a document somebody may be asked to accept.
DRAFT_MARKERS: tuple[tuple[str, str], ...] = (
    ("{{", "an unfilled {{PLACEHOLDER}}"),
    ("[ANWALT", "an open question for counsel ([ANWALT])"),
    ("PLATZHALTER", "a PLATZHALTER note"),
    ("TODO(", "a TODO marker"),
    ("Entwurf — nicht anwaltlich", "the draft banner"),
    ("Draft — not reviewed", "the draft banner"),
    ("RENDERN", "a RENDERN marker from the source draft"),
    ("nicht gerendert", "working material that is not meant to be rendered"),
)

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class Refused(Exception):
    """The file or the arguments are not publishable. The message says why."""


def digest_of(path: pathlib.Path) -> str:
    """SHA-256 of the file's bytes, lower-case hex. Binary read on purpose."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def url_for(path: pathlib.Path) -> str:
    """The URL this file is served at — the three rules of `check.py`'s `url_for`."""
    relative = path.resolve().relative_to(PUBLIC).as_posix()
    if relative == "index.html":
        return "/"
    if relative.endswith("/index.html"):
        return "/" + relative[: -len("index.html")]
    if relative.endswith(".html"):
        return "/" + relative[: -len(".html")]
    return "/" + relative


def check_publishable(path: pathlib.Path, document_id: str, version: str, url: str) -> None:
    """Every refusal, in the order a reader would ask about it."""
    if document_id not in KNOWN_DOCUMENT_IDS:
        raise Refused(
            f"unknown document id {document_id!r}; the invite flow knows "
            f"{', '.join(KNOWN_DOCUMENT_IDS)} (backend/accounts/onboarding.py)"
        )
    if not VERSION_SHAPE.match(version):
        raise Refused(
            f"version {version!r} must match {VERSION_SHAPE.pattern} — an entry is "
            "'id:version:sha256:url' and entries are comma-separated, so a colon, a comma or "
            "a space in a version would split the wrong field"
        )
    if not url.startswith(OWN_ORIGIN + "/"):
        raise Refused(f"url {url!r} is not on {OWN_ORIGIN}")
    if not path.is_file():
        raise Refused(f"{path} is not a file")

    text = path.read_text(encoding="utf-8")
    for marker, what in DRAFT_MARKERS:
        if marker in text:
            raise Refused(
                f"{path.name} still contains {what} ({marker!r}) — a draft is never published as "
                "the document somebody accepts"
            )
    if version not in text:
        raise Refused(
            f"{path.name} does not name the version {version!r} anywhere. The page has to say "
            "which version it is, or a reader cannot tell what they accepted"
        )


def entry_for(path: pathlib.Path, document_id: str, version: str, url: str) -> str:
    check_publishable(path, document_id, version, url)
    return f"{document_id}:{version}:{digest_of(path)}:{url}"


def parse_entry(entry: str) -> tuple[str, str, str, str]:
    """Split an entry the way `accounts.onboarding._parse_entry` does: url last."""
    parts = [part.strip() for part in entry.strip().split(":", 3)]
    if len(parts) != 4:
        raise Refused(f"{entry!r} is not 'id:version:sha256:url'")
    document_id, version, digest, url = parts
    if not SHA256_HEX.match(digest.lower()):
        raise Refused(f"{digest!r} is not a sha256 digest")
    return document_id, version, digest.lower(), url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the ONBOARDING_DOCUMENTS entry for a published legal document.",
    )
    parser.add_argument("file", help="the published HTML file, normally under website/public/")
    parser.add_argument("--id", dest="document_id", help="; ".join(KNOWN_DOCUMENT_IDS))
    parser.add_argument("--version", help="the version this page names, e.g. a date")
    parser.add_argument(
        "--url",
        default=None,
        help=f"override the derived URL; must be on {OWN_ORIGIN}",
    )
    parser.add_argument(
        "--check",
        dest="entry",
        default=None,
        help="verify an existing entry against the file instead of printing a new one",
    )
    args = parser.parse_args(argv)

    path = pathlib.Path(args.file)
    try:
        if args.entry is not None:
            document_id, version, digest, url = parse_entry(args.entry)
        else:
            if not args.document_id or not args.version:
                parser.error("--id and --version are required unless --check is given")
            document_id, version, url = args.document_id, args.version, args.url or ""
            digest = ""

        if not url:
            resolved = path.resolve()
            if not resolved.is_relative_to(PUBLIC):
                raise Refused(
                    f"{path} is not under {PUBLIC}, so its URL cannot be derived — publish it "
                    "there first, or pass --url"
                )
            url = OWN_ORIGIN + url_for(resolved)

        computed = entry_for(path, document_id, version, url)
        if args.entry is not None:
            if computed != f"{document_id}:{version}:{digest}:{url}":
                raise Refused(
                    "the entry does not match the file any more. Computed:\n"
                    f"  {computed}\ngiven:\n  {args.entry.strip()}\n"
                    "Either the page changed after it was published — then it is a new version, "
                    "with a new entry and a fresh acceptance — or the entry is stale."
                )
            print(f"{path}: the entry still matches the published bytes", file=sys.stderr)
            return 0
    except Refused as refusal:
        print(f"publish_document: refused — {refusal}", file=sys.stderr)
        return 1

    size = path.stat().st_size
    print(f"{path}: {size} bytes, sha256 of exactly those bytes", file=sys.stderr)
    print(computed)
    print(
        "Paste this into ONBOARDING_DOCUMENTS (both entries, comma-separated), then recreate the "
        "containers that read it — a container keeps the environment it was created with.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
