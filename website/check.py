#!/usr/bin/env python3
"""Validate the public marketing site. Run it with `make website-check`.

No dependencies: the standard library only, and no network. The site has no build step, so this
script *is* the build — it is the only thing standing between an edit and a published page.

Seven things it checks, in the order they matter:

0. **No publishing blocker.** A fact only the operator can supply — the one in the list today is
   the hosting provider's registered seat in the privacy notice — stands in the page as a literal
   marker, and this check refuses to pass while it does. A legal page that looks finished and is
   not is exactly the failure this site cannot afford.
1. **No external origin, anywhere.** The site's pitch is that it stores nothing about the reader
   and asks nobody else for anything, so a single foreign URL in a page, a stylesheet or an SVG
   makes the claim false. This check reads every file the browser parses and fails on any URL that
   is not on our own origin. It is the reason there can honestly be no cookie banner.
2. **No banned wording, in either language.** The forbidden patterns are *ported from*
   `frontend/src/i18n/wording.test.ts` — the same list the product enforces on every customer
   string — so the brochure cannot promise what the app carefully does not. Both languages' lists
   are applied to every page: there is no legitimate reason for an English banned word to appear
   on a German page, and a single list is one fewer thing to keep in step.
3. **The HTML holds together**: nesting, one `<h1>`, real landmarks, a title and a description,
   `lang`, unique ids, no script/frame/inline handler, and an `<img>` (there are none today) that
   lazy-loads. Inline SVG *is* allowed — the icons are the site's only pictures — but only as
   drawing: `<image>`, `<foreignObject>`, `<use>` and `xlink:` are refused, so an icon cannot fetch
   anything or reopen HTML inside itself.
4. **The links resolve**, by the same three `try_files` rules the site block in
   `infra/prod/Caddyfile` gives the real server, so a link that passes here is a link the server
   can serve. Fragments must name an id that exists.
5. **The forbidden-content list never appears** (issue #128): no site name (a bare domain in
   prose — site names live in `catalog.Site`, never in this repository), no quoted title of a
   work, no testimonial (blocked structurally too — see `FORBIDDEN_ELEMENTS`). Purely
   prospective: the site is already past this bar and the check only keeps it there.
6. **"Dein Video bleibt bei dir." / "Your video stays with you." stays tied to
   `CLOUD_FINGERPRINTING_ENABLED`** (issue #124): every place that flag's *default* is written —
   `backend/marceline/settings.py`, `infra/compose.yaml`, `infra/prod/compose.yaml` — must still say
   false while either page states the unconditional claim. ADR 0044's Consequences already require
   the conditional wording "from the moment the flag can be switched on"; this is what enforces it.

7. **No registered-trademark claim while there is no registration** (issue #145): the character `®`
   and the phrases *eingetragene Marke*, *registered trademark*, *markenrechtlich geschützt* and
   their neighbours are refused. The product name is not registered (issue #55), so the site does
   not claim it is. `™`
   asserts nothing and is allowed. This check comes out again on the day a registration exists,
   with the registration number in the commit message.

It also prints the measured weight of each page, because "fast" is a claim like any other.
"""

from __future__ import annotations

import html.parser
import pathlib
import re
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC = ROOT / "public"

OWN_ORIGIN = "https://marceline.studio"

# XML namespace URIs are identifiers, not fetches: no browser ever requests them. They are allowed
# only as the value of an xmlns attribute, which is checked below, never as a link target.
NAMESPACE_URIS = {
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xhtml",
    "http://www.sitemaps.org/schemas/sitemap/0.9",
}

# The one file exempt from the origin scan, and why: the SIL OFL requires its notice to travel
# with the font files, and the notice names the upstream projects' URLs. The Impressum links to it
# (that is where the fonts are credited since 2026-09-18), but it is served as plain text: a
# browser displays those URLs, it never fetches one. Removing them would breach the licence;
# dropping the file would breach it twice.
ORIGIN_SCAN_EXEMPT = {"assets/fonts/LICENSES.txt"}

SCANNED_SUFFIXES = {".html", ".css", ".svg", ".xml", ".txt"}

# Publishing guards. A marker here is a fact nobody in a coding session can supply, standing in a
# page that would otherwise look finished — the Impressum and the Datenschutzerklärung are the
# reason this exists. The site must not be capable of being declared green while one stands, so
# each marker fails the check and says what has to be filled in and by whom.
#
# The rule for adding one: the marker is the literal string the page carries, it is written into
# the page's own visible text (not only a comment, which an edit can leave behind), and the
# message names the single fact that resolves it. Delete the entry in the same change that fills
# the fact in.
PUBLISHING_BLOCKERS: dict[str, str] = {
    "TODO(hoster)": (
        "the hosting provider's registered seat is still a placeholder in the privacy notice. "
        "Only the operator can supply it (it is not documented anywhere in this repository): "
        "take it from the provider's own Impressum or the commercial register, replace the marker "
        "on datenschutz.html and en/privacy.html, and delete this entry from PUBLISHING_BLOCKERS"
    ),
}

# Weight budgets. The fonts are cached for a year and shared by every page, so the number that
# matters for a second page view is HTML + nothing.
MAX_HTML_BYTES = 26_000
MAX_CSS_BYTES = 26_000
MAX_FIRST_VIEW_BYTES = 150_000

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
}
FORBIDDEN_ELEMENTS = {
    "script", "iframe", "frame", "object", "embed", "video", "audio", "canvas", "applet",
    "picture", "form", "input", "textarea", "select", "button",
    # Inline SVG is the site's only picture medium (the icons), and these are the three ways an
    # SVG reaches back out of it: <image> fetches a bitmap, <foreignObject> reopens HTML inside the
    # drawing, <use> pulls a fragment from a document. The tag names arrive lowercased from the
    # parser. `xlink:` attributes are refused below for the same reason.
    "image", "foreignobject", "use",
    # Issue #128: "no testimonial, no client logo, no 'trusted by'" (website/README.md)
    # — a testimonial is conventionally marked up with one of these three, so the markup is refused
    # structurally rather than trusted to never appear. The wording half (no quoting) is in
    # QUOTE_CHARACTERS below.
    "blockquote", "cite", "q",
}

# Ported verbatim from frontend/src/i18n/wording.test.ts (FORBIDDEN). Marceline states technical
# facts; the customer makes the legal assertion. A hit here is not a pattern to loosen — rephrase
# the sentence, in both languages, exactly as the product's own rule says.
FORBIDDEN_WORDING: dict[str, list[str]] = {
    "en": [
        r"\bleak",
        r"infring",
        r"\billegal",
        r"\bunlawful",
        r"\bpira(te|cy)",
        r"\bverified\b",
        r"\bconfirm(ed|s)?\b",
        r"\bcases?\b",
        r"case file",
        r"\bescalat",
        r"on (your|the customer's) behalf",
        r"\bsend(ing|s)? (it |them |the (draft|notice) )?for you\b|\bsending by us\b"
        r"|\bwe (will )?send (the |a |your )?(notices?|drafts?|requests?)\b",
        r"\bhandled\b",
        r"\b(DSA|DMCA) applies\b",
        r"\bEU host\b",
        r"\bwe (will )?remove\b",
        r"\bguarantee",
        r"\bchance\b",
        r"\blikel(y|ihood)\b",
        r"\bexpect",
        r"\bforecast|\bpredict",
        r"\bwithin \d+ (hours?|days?)\b",
    ],
    "de": [
        r"leak",
        r"rechtswidrig",
        r"\billegal",
        r"urheberrechtsverletz|verletzung",
        r"pirat",
        r"verifiziert",
        r"\bbestätigt",
        r"\bFall\b|\bFälle\b|Fallakte",
        r"eskal",
        r"in Ihrem Namen|für Sie (ver)?send|Versand durch uns"
        r"|wir (ver)?senden (die |eine |Ihre )?(Meldung|Entwurf|Entwürfe|Antr)",
        r"\berledigt\b",
        r"\b(DSA|DMCA) gilt\b",
        r"wir entfernen",
        r"garant",
        r"\bchance|aussicht auf",
        r"wahrscheinlich",
        r"erwarte",
        r"prognose|vorhersage",
        r"innerhalb (von )?\d+ (Stunden?|Tagen?)",
    ],
}

# The site's own sentences that must keep working, so a future edit cannot quietly water the list
# down: each of these has to be caught by the list above. Same idea as the second test in
# wording.test.ts — a check nobody can pass by deleting a pattern.
WORDING_CANARIES = [
    "Confirmed leak",
    "we remove it on your behalf",
    "Sending it for you:",
    "We expect a takedown within 24 hours",
    "Bestätigter Leak — Fallakte",
    "Versand in Ihrem Namen:",
    "Wir senden die Meldung für Sie",
    "Die Entfernung ist garantiert",
]

# The same device for the picture guard. Inline SVG is allowed here — the icons are the site's only
# pictures — so the rule that keeps it harmless has to stay sharp: an icon draws with paths and
# links to nothing. Each snippet below has to produce at least one problem.
SVG_CANARIES = [
    '<svg><image href="/assets/favicon.svg"></image></svg>',
    "<svg><foreignObject><p>html inside the drawing</p></foreignObject></svg>",
    '<svg><use xlink:href="/sprite.svg#icon"></use></svg>',
    "<svg><script>alert(1)</script></svg>",
]

# ---------------------------------------------------------- forbidden content (issue #128)
#
# What the public page never shows: no image or thumbnail (already enforced above — no <img>
# exists and check_no_external_origin refuses a foreign fetch), no site name, no quoted title of a
# work, no creator name and no testimonial naming one. Purely prospective: the site is already
# past this bar; this keeps it that way. "No creator name" outside a testimonial is not checked
# here — it would need a name list, and this file must never hold one; that half stays an
# editorial rule, not a build check.

# Site names live in `catalog.Site` in the database, never in this repository, so this is written
# against the *shape* of a domain in prose rather than a list of domains, and must stay that way.
# An email address is not a site mention — this page legitimately prints two (ours, and the
# supervisory authority's in the Art. 13 notice) — so email-shaped text is stripped first.
EMAIL_SHAPE = re.compile(r"[\w.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+")
DOMAIN_SHAPE = re.compile(r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.[a-zA-Z]{2,24}\b")
# No site this product watches ends in one of these — they name one of this site's own files in
# prose (the font licence link text is the one example today), never a domain.
OWN_FILE_EXTENSIONS = {"html", "css", "svg", "xml", "txt", "json", "woff2", "ico", "js"}
OWN_DOMAIN = OWN_ORIGIN.removeprefix("https://")

# "No quoted title of a work", and half of "no testimonial naming a creator" — a testimonial is
# always a quotation. Only the paired quoting marks are checked: the plain apostrophe (') and the
# curly right single quote (’) are excluded because both languages' prose uses them as ordinary
# possessives ("the server's") today, never to open a quotation.
QUOTE_CHARACTERS = re.compile(r'["„“”«»]')

# Same device as WORDING_CANARIES: proof that a future edit narrowing these patterns is caught.
FORBIDDEN_CONTENT_CANARIES: dict[str, str] = {
    "a bare domain in prose": "please visit videos.example for more",
    "a quoted title": 'she called it "the video" in the review',
}

# ------------------------------------------------------------- issue #124: the cloud-flag claim
#
# "Dein Video bleibt bei dir." / "Your video stays with you." is a headline promise on the home
# page in both languages, and sits in the <meta name="description"> of both too. ADR 0044's
# Consequences already require the conditional wording "from the moment the flag can be switched
# on" and name the marketing site. This does not read a running container's environment (no
# network here) — it reads every place `CLOUD_FINGERPRINTING_ENABLED`'s *default* is written, so an
# edit that flips one is caught before it ships, not after. Do not change the website wording here:
# that is the operator's call, and moving it is what unblocks the flag, not the other way round.
CLOUD_FLAG_NAME = "CLOUD_FINGERPRINTING_ENABLED"
UNCONDITIONAL_CLAIM: dict[str, str] = {
    "de": "Dein Video bleibt bei dir",
    "en": "Your video stays with you",
}

# -------------------------------------------- issue #145: no registered-trademark claim
#
# The product name is not registered (issue #55), so no page may claim a registration. This is
# prospective: a sweep of website/public/, frontend/src/i18n/ and frontend/customer/ on
# 2026-09-19 found no such claim anywhere, and this keeps it that way.
#
# `™` is deliberately **not** here: it asserts nothing and is the mark to use meanwhile. The check
# comes out again on the day a registration exists — **with the registration number in the commit
# message**, which is the only evidence that removing it was allowed.
#
# The "any" key is for the symbol, which is not a language. check_wording()'s sibling applies every
# language's patterns to every page anyway; this one keeps the distinction only for the message.
REGISTRATION_CLAIM_WORDING: dict[str, list[str]] = {
    "any": [r"®"],
    "en": [
        r"\bregistered\s+trade\s?mark",
        r"\bregistered\s+mark\b",
        # "a registered brand", "our brand is registered"
        r"\bregistered\s+brand\b",
        r"\bbrand\s+is\s+registered\b",
    ],
    "de": [
        # "eingetragene Marke", "eingetragener Wort-/Bildmarke", "als Marke eingetragen"
        r"eingetragene\w*\s+(?:Wort-?\s*/?\s*Bild-?)?Marke",
        r"als\s+Marke\s+eingetragen",
        r"eingetragene\w*\s+Warenzeichen",
        r"markenrechtlich\s+geschützt",
    ],
}

# Same device as WORDING_CANARIES and FORBIDDEN_CONTENT_CANARIES: a future edit that narrows a
# pattern is caught by the check itself. Each of these has to produce at least one problem.
REGISTRATION_CLAIM_CANARIES: dict[str, str] = {
    "the symbol": "Marceline® findet Kopien",
    "an English registration claim": "a registered trademark of the provider",
    "an English brand claim": "our brand is registered in the EU",
    "a German registration claim": "der Name ist eine eingetragene Marke",
    "a German Warenzeichen claim": "ein eingetragenes Warenzeichen des Anbieters",
    "a German protection claim": "der Name ist markenrechtlich geschützt",
}

# What the check must **not** refuse, so that a legally required sentence stays writable. The
# Impressum has to be able to say that the provider is a sole trader who is not in the commercial
# register (§ 5 DDG), and that has nothing to do with a trademark.
REGISTRATION_CLAIM_ALLOWED = [
    "Nicht eingetragener Einzelunternehmer",
    "eine eingetragene Gesellschaft besteht nicht",
    "registered in the commercial register",
    "a registered UG",
]


@dataclass
class Page:
    path: pathlib.Path
    url: str
    lang: str
    title: str = ""
    description: str = ""
    text: str = ""
    ids: set[str] = field(default_factory=set)
    links: list[tuple[str, str]] = field(default_factory=list)  # (attribute, value)
    fragments: list[str] = field(default_factory=list)
    headings: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    noindex: bool = False


class Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: object, message: str) -> None:
        self.items.append(f"{where}: {message}")

    def __bool__(self) -> bool:
        return bool(self.items)


class PageParser(html.parser.HTMLParser):
    """Structure, text and links in one pass, plus a nesting stack.

    `convert_charrefs` stays on so the extracted text is what a reader sees — the wording scan has
    to look at `&nbsp;` as a space, not as an entity it would never match against.
    """

    def __init__(self, page: Page, problems: Problems) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self.problems = problems
        self.stack: list[tuple[str, int]] = []
        self.suppress_text = 0
        self.in_svg = 0
        self.in_title = False

    def _where(self) -> str:
        line, _ = self.getpos()
        return f"{self.page.path.relative_to(ROOT)}:{line}"

    def handle_decl(self, decl: str) -> None:
        if self.getpos()[0] == 1 and decl.lower() != "doctype html":
            self.problems.add(self._where(), f"unexpected doctype {decl!r}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._element(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, self.getpos()[0]))
        if tag in ("style", "script"):
            self.suppress_text += 1
        if tag == "svg":
            self.in_svg += 1
        # An SVG carries its own <title> for accessibility; only the document's counts as the
        # page title (its text still reaches the wording scan through handle_data).
        if tag == "title" and not self.in_svg:
            self.in_title = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._element(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if tag in ("style", "script"):
            self.suppress_text = max(0, self.suppress_text - 1)
        if tag == "svg":
            self.in_svg = max(0, self.in_svg - 1)
        if tag == "title":
            self.in_title = False
        if not self.stack:
            self.problems.add(self._where(), f"</{tag}> closes nothing")
            return
        if self.stack[-1][0] != tag:
            open_tag, open_line = self.stack[-1]
            self.problems.add(
                self._where(), f"</{tag}> closes <{open_tag}> opened on line {open_line}"
            )
            # Unwind to the matching tag if there is one, so one mistake is one message.
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    return
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.suppress_text:
            return
        if self.in_title:
            self.page.title += data.strip()
        self.page.text += data

    def _element(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        self.page.counts[tag] = self.page.counts.get(tag, 0) + 1

        if tag in FORBIDDEN_ELEMENTS:
            self.problems.add(
                self._where(),
                f"<{tag}> is not allowed: this site runs no script, embeds nothing and takes no "
                "input (the Content-Security-Policy in infra/prod/Caddyfile forbids it too)",
            )

        for name, value in attributes.items():
            if name.startswith("on"):
                self.problems.add(self._where(), f"inline handler {name}= on <{tag}>")
            if name.startswith("xlink:"):
                self.problems.add(
                    self._where(),
                    f"{name}= on <{tag}>: an inline SVG on this site draws with paths only and "
                    "links to nothing",
                )
            if name == "style":
                self.problems.add(
                    self._where(),
                    f"inline style= on <{tag}>: put it in assets/site.css, where the tokens are",
                )
            if name == "xmlns" and value not in NAMESPACE_URIS:
                self.problems.add(self._where(), f"unknown namespace {value!r}")

        if "id" in attributes:
            identifier = attributes["id"]
            if identifier in self.page.ids:
                self.problems.add(self._where(), f"duplicate id {identifier!r}")
            self.page.ids.add(identifier)

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.page.headings.append(int(tag[1]))

        if tag == "html":
            self.page.lang = attributes.get("lang", "")

        if tag == "meta":
            if attributes.get("name") == "description":
                self.page.description = attributes.get("content", "")
            if attributes.get("name") == "robots" and "noindex" in attributes.get("content", ""):
                self.page.noindex = True

        if tag == "img":
            for required, expected in (("loading", "lazy"), ("decoding", "async")):
                if attributes.get(required) != expected:
                    self.problems.add(
                        self._where(), f'<img> needs {required}="{expected}" (project rule)'
                    )
            if not attributes.get("alt") and attributes.get("alt") != "":
                self.problems.add(self._where(), "<img> needs an alt attribute")
            if not (attributes.get("width") and attributes.get("height")):
                self.problems.add(
                    self._where(), "<img> needs width and height so its box is sized before load"
                )

        if tag == "link" and attributes.get("rel") in ("preconnect", "dns-prefetch", "prefetch"):
            self.problems.add(self._where(), f'<link rel="{attributes["rel"]}"> reaches outwards')

        for name in ("href", "src", "srcset", "poster", "action", "data"):
            if name in attributes and attributes[name]:
                self.page.links.append((f"{tag}[{name}]", attributes[name]))

        # Attribute text a reader or a screen reader sees, so the wording scan covers it too.
        for name in ("alt", "aria-label", "title", "content", "placeholder"):
            if name in attributes and tag != "meta":
                self.page.text += " " + attributes[name] + " "

    def close(self) -> None:  # noqa: D102
        super().close()
        for tag, line in self.stack:
            self.problems.add(
                f"{self.page.path.relative_to(ROOT)}:{line}", f"<{tag}> is never closed"
            )


# ------------------------------------------------------------------ resolution


def url_for(path: pathlib.Path) -> str:
    relative = path.relative_to(PUBLIC).as_posix()
    if relative == "index.html":
        return "/"
    if relative.endswith("/index.html"):
        return "/" + relative[: -len("index.html")]
    if relative.endswith(".html"):
        return "/" + relative[: -len(".html")]
    return "/" + relative


def resolve(url_path: str) -> pathlib.Path | None:
    """The three try_files rules of the site block in infra/prod/Caddyfile, in the same order."""
    trimmed = url_path.lstrip("/")
    candidates = [
        PUBLIC / trimmed if trimmed else None,
        PUBLIC / trimmed / "index.html",
        PUBLIC / (trimmed + ".html") if trimmed else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------- checks


def check_no_external_origin(problems: Problems) -> int:
    """Any URL that is not our own origin, in any file the browser parses."""
    url_pattern = re.compile(r"""(?:https?:)?//[^\s"'<>)\]]+""")
    scanned = 0
    for path in sorted(PUBLIC.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        relative = path.relative_to(PUBLIC).as_posix()
        if relative in ORIGIN_SCAN_EXEMPT:
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in url_pattern.finditer(line):
                url = match.group(0).rstrip(".,;:")
                if url.startswith(OWN_ORIGIN + "/") or url == OWN_ORIGIN:
                    continue
                if url in NAMESPACE_URIS:
                    # Only as a namespace declaration, never as something to fetch.
                    before = line[: match.start()]
                    if re.search(r"xmlns(:\w+)?\s*=\s*[\"']$", before):
                        continue
                problems.add(
                    f"{path.relative_to(ROOT)}:{line_number}",
                    f"external origin {url!r} — this site fetches from nobody",
                )
    return scanned


def check_publishing_blockers(problems: Problems) -> None:
    """Refuse to pass while a marked placeholder stands in a served file.

    The markers are in PUBLISHING_BLOCKERS. This reads the files on disk rather than the parsed
    pages, so a marker hiding in a comment, an attribute or the sitemap is caught too.
    """
    for path in sorted(PUBLIC.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for marker, explanation in PUBLISHING_BLOCKERS.items():
                if marker in line:
                    problems.add(
                        f"{path.relative_to(ROOT)}:{line_number}",
                        f"{marker} — not publishable: {explanation}",
                    )


def cloud_flag_defaults(repo_root: pathlib.Path) -> list[tuple[str, bool | None]]:
    """Every place `CLOUD_FINGERPRINTING_ENABLED`'s *default* is written, and what it says.

    Three files decide it: the Django setting's own default, and the two compose files' fallback
    for the environment variable (`${CLOUD_FINGERPRINTING_ENABLED:-false}` in the development
    compose file, twice, and a hardcoded `"false"` in the production one). `None` means this could
    not find or parse the default at that location at all — a change to one of these files' shape,
    not to the value — and issue #124's check treats that as a failure too, never a silent skip.
    """
    findings: list[tuple[str, bool | None]] = []

    settings_path = repo_root / "backend" / "marceline" / "settings.py"
    match = re.search(
        rf'{CLOUD_FLAG_NAME}"\s*,\s*default=(True|False)\s*\)', settings_path.read_text()
    )
    findings.append(
        (
            f"{settings_path.relative_to(repo_root)}",
            None if match is None else match.group(1) == "True",
        )
    )

    compose_path = repo_root / "infra" / "compose.yaml"
    compose_text = compose_path.read_text()
    compose_matches = list(
        re.finditer(rf"{CLOUD_FLAG_NAME}:\s*\$\{{{CLOUD_FLAG_NAME}:-(\w+)\}}", compose_text)
    )
    if not compose_matches:
        findings.append((f"{compose_path.relative_to(repo_root)}", None))
    for index, item in enumerate(compose_matches, start=1):
        findings.append(
            (
                f"{compose_path.relative_to(repo_root)} (occurrence {index})",
                item.group(1).lower() == "true",
            )
        )

    # The production file writes it more than once since ADR 0044's amendment of 2026-09-19: the
    # shared anchor states the refusal (`"false"`) and the `web` service, which may relay an upload,
    # takes it from the environment with a default (`${CLOUD_FINGERPRINTING_ENABLED:-false}`). Both
    # shapes are read, and **every** occurrence is reported: a second service that quietly defaulted
    # to true would otherwise be invisible behind the first one's `re.search`.
    prod_compose_path = repo_root / "infra" / "prod" / "compose.yaml"
    prod_matches = list(
        re.finditer(
            rf'{CLOUD_FLAG_NAME}:\s*(?:"(?P<quoted>\w+)"'
            rf"|\$\{{{CLOUD_FLAG_NAME}:-(?P<fallback>\w*)\}}"
            rf"|(?P<bare>\w+))",
            prod_compose_path.read_text(),
        )
    )
    if not prod_matches:
        findings.append((f"{prod_compose_path.relative_to(repo_root)}", None))
    for index, item in enumerate(prod_matches, start=1):
        written = item.group("quoted") or item.group("bare") or item.group("fallback") or ""
        findings.append(
            (
                f"{prod_compose_path.relative_to(repo_root)} (occurrence {index})",
                written.lower() == "true",
            )
        )

    return findings


def check_cloud_flag_tied_to_claim(
    pages: list[Page], defaults: list[tuple[str, bool | None]], problems: Problems
) -> None:
    """Issue #124: the unconditional claim and a true `CLOUD_FINGERPRINTING_ENABLED` default must
    never coexist. Does not change the website wording — that is the operator's call, and it has
    to happen *before* any of these defaults moves (ADR 0044's Consequences), not after.
    """
    claim_pages: list[str] = []
    for page in pages:
        haystack = re.sub(r"\s+", " ", f"{page.title} {page.description} {page.text}")
        if any(claim in haystack for claim in UNCONDITIONAL_CLAIM.values()):
            claim_pages.append(str(page.path.relative_to(ROOT)))

    for location, default in defaults:
        if default is None:
            problems.add(
                location,
                f"could not find {CLOUD_FLAG_NAME}'s default here — issue #124's guard depends on "
                "parsing it; update cloud_flag_defaults() in website/check.py to match",
            )
            continue
        if default and claim_pages:
            problems.add(
                location,
                f"{CLOUD_FLAG_NAME} defaults to true here while {', '.join(claim_pages)} still "
                "states the unconditional claim (\"Dein Video bleibt bei dir.\" / \"Your video "
                "stays with you.\") — change the website wording to the conditional version first "
                "(ADR 0044 Consequences), then this default; issue #124",
            )


def check_wording(pages: list[Page], problems: Problems) -> None:
    compiled = {
        language: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        for language, patterns in FORBIDDEN_WORDING.items()
    }

    for sample in WORDING_CANARIES:
        if not any(
            pattern.search(sample) for patterns in compiled.values() for pattern in patterns
        ):
            problems.add(
                "website/check.py",
                f"the forbidden-wording list no longer catches {sample!r} — a pattern was "
                "weakened; restore it from frontend/src/i18n/wording.test.ts",
            )

    for page in pages:
        haystack = re.sub(r"\s+", " ", f"{page.title} {page.description} {page.text}")
        for language, patterns in compiled.items():
            for pattern in patterns:
                found = pattern.search(haystack)
                if found:
                    start = max(0, found.start() - 40)
                    context = haystack[start : found.end() + 40].strip()
                    problems.add(
                        page.path.relative_to(ROOT),
                        f"banned {language} wording {pattern.pattern!r} in “…{context}…” — "
                        "rephrase as a technical fact, never loosen the pattern",
                    )


def check_forbidden_content(pages: list[Page], problems: Problems) -> None:
    """The forbidden-content list (issue #128).

    No image/thumbnail is enforced elsewhere already. This covers the rest that a page's parsed
    text can show: no site name (a bare domain in prose), no quoted title. The other half of "no testimonial" — the markup a testimonial needs — is in
    FORBIDDEN_ELEMENTS (blockquote/cite/q), not here.
    """
    for page in pages:
        where = page.path.relative_to(ROOT)
        haystack = re.sub(r"\s+", " ", f"{page.title} {page.description} {page.text}")

        quote = QUOTE_CHARACTERS.search(haystack)
        if quote:
            start = max(0, quote.start() - 40)
            context = haystack[start : quote.end() + 40].strip()
            problems.add(
                where,
                f"quotation mark in “…{context}…” — no quoted title of a work and no testimonial "
                "may appear on this page (issue #128); rephrase without quoting",
            )

        without_emails = EMAIL_SHAPE.sub(" ", haystack)
        for match in DOMAIN_SHAPE.finditer(without_emails):
            domain = match.group(0)
            if domain.lower() == OWN_DOMAIN.lower():
                continue
            extension = domain.rsplit(".", 1)[-1].lower()
            if extension in OWN_FILE_EXTENSIONS:
                continue
            problems.add(
                where,
                f"domain-shaped text {domain!r} — no site this product watches is ever named on "
                "the public page (issue #128); site names live in catalog.Site, not here",
            )


def check_forbidden_content_canaries(problems: Problems) -> None:
    """Same idea as WORDING_CANARIES: a future edit cannot quietly narrow these patterns."""
    for description, sample in FORBIDDEN_CONTENT_CANARIES.items():
        caught = bool(DOMAIN_SHAPE.search(EMAIL_SHAPE.sub(" ", sample))) or bool(
            QUOTE_CHARACTERS.search(sample)
        )
        if not caught:
            problems.add(
                "website/check.py",
                f"the forbidden-content check no longer catches {description} ({sample!r}) — a "
                "pattern was weakened; restore it (issue #128)",
            )


def check_registration_claim(pages: list[Page], problems: Problems) -> None:
    """No page claims a registered trademark while no registration exists (issue #145).

    Removal is allowed on exactly one condition: a registration exists, and the commit message that
    removes this carries its number. Rephrasing the sentence is always the answer to a hit; `™`
    asserts nothing and is available.
    """
    compiled = {
        language: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        for language, patterns in REGISTRATION_CLAIM_WORDING.items()
    }
    for page in pages:
        haystack = re.sub(r"\s+", " ", f"{page.title} {page.description} {page.text}")
        for language, patterns in compiled.items():
            for pattern in patterns:
                found = pattern.search(haystack)
                if found:
                    start = max(0, found.start() - 40)
                    context = haystack[start : found.end() + 40].strip()
                    problems.add(
                        page.path.relative_to(ROOT),
                        f"registered-trademark claim ({language} {pattern.pattern!r}) in "
                        f"“…{context}…” — the name is not registered (issues #55, #145); "
                        "use “™” or nothing",
                    )


def check_registration_claim_canaries(problems: Problems) -> None:
    """The patterns still catch every claim, and still leave the Impressum's own sentences alone."""
    compiled = [
        re.compile(pattern, re.IGNORECASE)
        for patterns in REGISTRATION_CLAIM_WORDING.values()
        for pattern in patterns
    ]
    for description, sample in REGISTRATION_CLAIM_CANARIES.items():
        if not any(pattern.search(sample) for pattern in compiled):
            problems.add(
                "website/check.py",
                f"the registered-trademark check no longer catches {description} ({sample!r}) — a "
                "pattern was weakened; restore it (issue #145). It comes out only on the day a "
                "registration exists, with the number in the commit message",
            )
    for sample in REGISTRATION_CLAIM_ALLOWED:
        hit = next((pattern for pattern in compiled if pattern.search(sample)), None)
        if hit is not None:
            problems.add(
                "website/check.py",
                f"the registered-trademark check now also refuses {sample!r} (pattern "
                f"{hit.pattern!r}) — that is a company-register sentence the Impressum needs, not a "
                "trademark claim; narrow the pattern (issue #145)",
            )


def check_svg_guard(problems: Problems) -> None:
    """Every SVG_CANARIES snippet still has to be refused by the element and attribute rules."""
    for snippet in SVG_CANARIES:
        probe = Problems()
        parser = PageParser(Page(path=PUBLIC / "index.html", url="/", lang=""), probe)
        parser.feed(snippet)
        parser.close()
        if not probe:
            problems.add(
                "website/check.py",
                f"inline SVG no longer refuses {snippet!r} — an icon draws with paths and links "
                "to nothing; restore the rule instead of widening it",
            )


def check_structure(page: Page, problems: Problems) -> None:
    where = page.path.relative_to(ROOT)
    expected_language = "en" if page.path.is_relative_to(PUBLIC / "en") else "de"
    if page.lang != expected_language:
        problems.add(where, f'<html lang> is {page.lang!r}, expected "{expected_language}"')

    if not page.title:
        problems.add(where, "no <title>")
    elif "Marceline" not in page.title:
        problems.add(where, f"<title> {page.title!r} does not name the company")
    elif len(page.title) > 70:
        problems.add(where, f"<title> is {len(page.title)} characters; keep it under 70")

    if not page.description:
        problems.add(where, 'no <meta name="description">')
    elif not 50 <= len(page.description) <= 200:
        problems.add(
            where, f"meta description is {len(page.description)} characters; aim for 50–200"
        )

    if page.counts.get("h1", 0) != 1:
        problems.add(where, f"{page.counts.get('h1', 0)} <h1> elements, expected exactly 1")
    for element, expected in (("main", 1), ("header", 1), ("footer", 1), ("nav", 1)):
        if page.counts.get(element, 0) != expected:
            problems.add(
                where, f"{page.counts.get(element, 0)} <{element}> landmarks, expected {expected}"
            )

    previous = 0
    for level in page.headings:
        if previous and level > previous + 1:
            problems.add(where, f"heading jumps from h{previous} to h{level}")
        previous = level

    if 'charset="utf-8"' not in page.path.read_text(encoding="utf-8").lower():
        problems.add(where, "no <meta charset=utf-8>")
    if "width=device-width" not in page.path.read_text(encoding="utf-8"):
        problems.add(where, "no responsive viewport meta")


def check_links(pages: list[Page], problems: Problems) -> None:
    by_url = {page.url: page for page in pages}

    for page in pages:
        where = page.path.relative_to(ROOT)
        for attribute, value in page.links:
            target = value.strip()
            if target.startswith("mailto:"):
                if not target.startswith("mailto:contact@marceline.studio"):
                    problems.add(where, f"{attribute} names another address: {target!r}")
                continue
            if target.startswith(OWN_ORIGIN):
                target = target[len(OWN_ORIGIN) :] or "/"
            elif target.startswith(("http://", "https://", "//")):
                continue  # already reported by the origin scan
            if target.startswith("#"):
                if target[1:] not in page.ids:
                    problems.add(where, f"{attribute} points at missing id {target!r}")
                continue

            url_path, _, fragment = target.partition("#")
            if not url_path.startswith("/"):
                base = page.path.parent.relative_to(PUBLIC).as_posix()
                url_path = "/" + (f"{base}/{url_path}" if base != "." else url_path)

            resolved = resolve(url_path)
            if resolved is None:
                problems.add(where, f"{attribute} → {value!r} resolves to no file")
                continue
            if fragment:
                other = by_url.get(url_for(resolved))
                if other is None:
                    problems.add(where, f"{attribute} → {value!r} has a fragment on a non-page")
                elif fragment not in other.ids:
                    problems.add(where, f"{attribute} → {value!r}: no such id on the target page")

    # The stylesheet's own url() references, resolved against the stylesheet's directory.
    stylesheet = PUBLIC / "assets" / "site.css"
    for match in re.finditer(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", stylesheet.read_text()):
        reference = match.group(1)
        if reference.startswith(("http://", "https://", "//", "data:")):
            continue
        if not (stylesheet.parent / reference).is_file():
            problems.add(stylesheet.relative_to(ROOT), f"url({reference!r}) resolves to no file")


def check_sitemap(pages: list[Page], problems: Problems) -> None:
    sitemap = PUBLIC / "sitemap.xml"
    where = sitemap.relative_to(ROOT)
    text = sitemap.read_text(encoding="utf-8")
    listed = set(re.findall(r"<loc>([^<]+)</loc>", text))

    for url in sorted(listed):
        if not url.startswith(OWN_ORIGIN):
            problems.add(where, f"<loc> on another origin: {url!r}")
            continue
        if resolve(url[len(OWN_ORIGIN) :]) is None:
            problems.add(where, f"<loc> {url!r} resolves to no file")

    for page in pages:
        expected = OWN_ORIGIN + page.url
        if page.noindex:
            if expected in listed:
                problems.add(where, f"{page.url} is noindex but listed in the sitemap")
        elif expected not in listed:
            problems.add(where, f"{page.url} is missing from the sitemap")

    robots = (PUBLIC / "robots.txt").read_text(encoding="utf-8")
    if f"Sitemap: {OWN_ORIGIN}/sitemap.xml" not in robots:
        problems.add("website/public/robots.txt", "does not point at the sitemap")


def check_weight(pages: list[Page], problems: Problems) -> list[tuple[str, int, int]]:
    stylesheet = PUBLIC / "assets" / "site.css"
    css_bytes = stylesheet.stat().st_size
    if css_bytes > MAX_CSS_BYTES:
        problems.add(stylesheet.relative_to(ROOT), f"{css_bytes} bytes, budget {MAX_CSS_BYTES}")

    # What a first view actually fetches: the page, the stylesheet, the favicon, and the latin
    # subset of each family. The latin-ext files are fetched only for a character neither German
    # nor English needs, so they are not in the first-view figure.
    favicon_bytes = (PUBLIC / "assets" / "favicon.svg").stat().st_size
    font_bytes = sum(
        (PUBLIC / "assets" / "fonts" / name).stat().st_size
        for name in (
            "instrument-sans-latin.c10e9d3f.woff2",
            "instrument-serif-latin.6ecf1043.woff2",
            "jetbrains-mono-latin.ef5e5368.woff2",
        )
    )

    measured = []
    for page in sorted(pages, key=lambda item: item.url):
        page_bytes = page.path.stat().st_size
        if page_bytes > MAX_HTML_BYTES:
            problems.add(
                page.path.relative_to(ROOT), f"{page_bytes} bytes, budget {MAX_HTML_BYTES}"
            )
        first_view = page_bytes + css_bytes + favicon_bytes + font_bytes
        if first_view > MAX_FIRST_VIEW_BYTES:
            problems.add(
                page.path.relative_to(ROOT),
                f"first view {first_view} bytes, budget {MAX_FIRST_VIEW_BYTES}",
            )
        measured.append((page.url, page_bytes, first_view))
    return measured


# ------------------------------------------------------------------------ main


def main() -> int:
    problems = Problems()

    html_files = sorted(PUBLIC.rglob("*.html"))
    if not html_files:
        print(f"no pages under {PUBLIC}", file=sys.stderr)
        return 1

    pages: list[Page] = []
    for path in html_files:
        page = Page(path=path, url=url_for(path), lang="")
        parser = PageParser(page, problems)
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
        check_structure(page, problems)
        pages.append(page)

    scanned = check_no_external_origin(problems)
    check_svg_guard(problems)
    check_publishing_blockers(problems)
    check_wording(pages, problems)
    check_forbidden_content_canaries(problems)
    check_forbidden_content(pages, problems)
    check_registration_claim_canaries(problems)
    check_registration_claim(pages, problems)
    check_cloud_flag_tied_to_claim(pages, cloud_flag_defaults(ROOT.parent), problems)
    check_links(pages, problems)
    check_sitemap(pages, problems)
    measured = check_weight(pages, problems)

    if problems:
        print(f"website: {len(problems.items)} problem(s)\n", file=sys.stderr)
        for item in problems.items:
            print(f"  {item}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1

    print(f"website: {len(pages)} pages, {scanned} files scanned for a foreign origin — none")
    print(
        f"website: no publishing blocker "
        f"({len(PUBLISHING_BLOCKERS)} marker(s) watched for: "
        f"{', '.join(sorted(PUBLISHING_BLOCKERS)) or 'none'})"
    )
    print("website: no banned wording (both lists applied to both languages), all links resolve")
    print(f"{'page':<34} {'html':>8} {'first view':>12}")
    for url, page_bytes, first_view in measured:
        print(f"{url:<34} {page_bytes / 1024:>7.1f}K {first_view / 1024:>11.1f}K")
    return 0


if __name__ == "__main__":
    sys.exit(main())
