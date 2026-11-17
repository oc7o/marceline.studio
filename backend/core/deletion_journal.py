"""The deletion journal (ADR 0057, DPIA risk R24, issues #137 / #140 D3).

Every irreversible privacy action in this codebase deletes rows from Postgres — a suppressed
performer's page links (ADR 0038), a deactivated page's stored text (ADR 0029), a forgotten
prospect (ADR 0048), a customer's case file (ADR 0045), a settled safety case, an expired page.
Postgres is dumped nightly and the dump is kept for months (`infra/backup/`), so **a restore rolls
every one of those deletions back** and nothing notices: the suppression marker comes back with the
snapshot, but so do the links it removed, and if the snapshot predates the request even the marker
is gone.

This module is the other side of that. Each of those writers appends **one line** here, and the
line holds only what a replay needs:

- **a kind** — which deletion path it was;
- **a stable key** — a database id, a keyed HMAC digest (a do-not-contact record's stored form) or
  a file's sha256;
- **a timestamp**, and for a few kinds two or three enum tokens and a date.

**Never the deleted content, and never a name in any recoverable form.** Not a performer or cast
name, not a page title, not a URL, not an address, not a handle, not a free-text note, not a staff
name. `_encode` refuses anything that is not a declared field of that kind with a declared shape
(an integer, 64 hex characters, an ISO date, or a short lower-case token), so a future caller
cannot put a title in by accident. The replay works because the **restored database still holds the
name**: it is a database older than the deletion, not a database with the name missing. A journal
line only has to say *which row* and *which rule*.

**Where it lives, and why.** `settings.DELETION_JOURNAL_DIR`, a directory next to the nightly
dumps (`~/backups/marceline/journal`), and `infra/backup/backup-run.sh` pushes it to the encrypted
off-host repository under its own restic tag. Three reasons for a file rather than a table:

1. a table is inside the thing being restored, so it would be rolled back with everything else —
   the journal would lose exactly the entries that matter;
2. the file is append-only and monotone, so any copy of it is a prefix of the truth: the newest
   copy is always the one to replay, whatever the age of the database;
3. it travels with the backups, so "the machine is gone" (docs/runbooks/backup-restore.md § D)
   still has it.

**One directory, several segment files.** Deletions are written by processes with different
identities — the host-run management commands as the operator, `web` and `scheduler` as root inside
a container — onto one bind-mounted host directory. A single shared file would be created by
whichever wrote first and be unappendable for the others, so each process writes its own
`<segment>.jsonl` (`DELETION_JOURNAL_SEGMENT`) and `read_journal` merges the directory by
timestamp. Every append is one `os.write` on a file opened `O_APPEND`, so concurrent writers never
interleave a line.

**A failed append never fails a deletion.** An objection under Art. 21 GDPR is honoured even if the
journal's disk is full; the append logs `CRITICAL` instead. The line is written from
`transaction.on_commit`, so a rolled-back deletion leaves no entry and a replay can never delete
something that was never deleted.

Replaying: `marceline.deletion_replay` and `manage.py reapply_deletions`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The shapes a journal field may have. Anything else is refused, which is what keeps a name, a
#: title, a URL or an address out of the file.
INT = "int"
#: A sha256 or an HMAC-SHA256 digest, lower-case hex. Not reversible into an identifier.
HEX64 = "hex64"
#: A calendar date, stored as `YYYY-MM-DD`.
DATE = "date"
#: A short lower-case enum token from a `choices` tuple (a basis, a channel, an identifier kind).
TOKEN = "token"

#: Every kind, and the fields its line carries. A field name ending in `?` may be `None`.
#: The **order** here is the order `marceline.deletion_replay` replays kinds in.
KIND_FIELDS: dict[str, dict[str, str]] = {
    # ADR 0038: a performer told us to stop indexing them. The basis, the request date and the
    # identity check are enum tokens and a date, so the re-created marker is the same record; the
    # note and `decided_by` are free text and stay out (the replay says so in the row it writes).
    "creator_suppression": {
        "creator": INT,
        "basis": TOKEN,
        "requested_on": DATE,
        "identity": TOKEN,
    },
    # The lift of one. Nothing to re-apply — see `replay.py`; it is listed for a human.
    "creator_suppression_lifted": {"creator": INT},
    # ADR 0029: the three name-bearing fields of a deactivated page.
    "page_text_scrub": {"page": INT},
    # ADR 0029 amendment: the page row itself, once its retention is up. The URL digest is the
    # replay's proof that it is about to delete the row the journal means.
    "page_deleted": {"page": INT, "url_sha256": HEX64},
    # docs/safety.md, issue #100: a settled safety case past its retention.
    "safety_incident_deleted": {"incident": INT},
    # ADR 0048: the query text of one discovery call (the row stays — it is the spend ledger).
    "search_query_scrubbed": {"call": INT},
    # ADR 0048: the query samples inside one source candidate's evidence.
    "candidate_queries_scrubbed": {"candidate": INT},
    # ADR 0048: one prospect's research (verdicts, links, breadcrumbs, watchlist entry).
    "prospect_research_forgotten": {"creator": INT},
    # ADR 0048: a catalogue name nothing uses any more.
    "creator_name_deleted": {"creator": INT},
    # ADR 0045: everything an organisation whose pilot ended left behind.
    "organisation_data_deleted": {"organisation": INT},
    # ADR 0045: one original an organisation lost.
    "original_data_deleted": {"organisation": INT, "sha256": HEX64},
    # ADR 0045: the case file of a file that changed hands — deleted so the new owner inherits no
    # case history.
    "case_file_reset": {"organisation": INT, "sha256": HEX64},
    # ADR 0047: a "no". Not a deletion at all, and that is the point: the record is append-only, so
    # a restore does not resurrect anything — it **loses** the newest objections. The stored form
    # is already a keyed digest, so the journal can carry it and the replay can re-insert the row.
    "do_not_contact_recorded": {
        "identifier_hash": HEX64,
        "identifier_kind": TOKEN,
        "received_on": DATE,
        "channel": TOKEN,
        "creator?": INT,
    },
}

_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

#: Written once per process when a deletion happens with no journal configured, so the gap is loud
#: in a log rather than silent forever.
_warned = False


class JournalRefused(ValueError):
    """A caller tried to journal something the format does not allow. A bug, never an input."""


def _encode(kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The validated payload of one line, or `JournalRefused`. Pure; no I/O, no database.

    This is the guard the "never a name, a title, a URL or an address" rule rests on: a field this
    kind does not declare is refused, and a declared one has to have its declared shape.
    """
    declared = KIND_FIELDS.get(kind)
    if declared is None:
        raise JournalRefused(f"unknown deletion journal kind {kind!r}")
    optional = {name.rstrip("?") for name in declared if name.endswith("?")}
    shapes = {name.rstrip("?"): shape for name, shape in declared.items()}
    unexpected = sorted(set(fields) - set(shapes))
    if unexpected:
        raise JournalRefused(f"{kind}: fields {unexpected} are not declared for this kind")
    missing = sorted(set(shapes) - set(fields) - optional)
    if missing:
        raise JournalRefused(f"{kind}: fields {missing} are missing")
    payload: dict[str, Any] = {}
    for name, shape in shapes.items():
        value = fields.get(name)
        if value is None:
            if name not in optional:
                raise JournalRefused(f"{kind}.{name} may not be empty")
            continue
        payload[name] = _value(f"{kind}.{name}", shape, value)
    return payload


def _value(where: str, shape: str, value: Any) -> Any:
    if shape == INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise JournalRefused(f"{where} must be an integer id, got {type(value).__name__}")
        return value
    if shape == HEX64:
        text = str(value).lower()
        if not _HEX64.fullmatch(text):
            raise JournalRefused(f"{where} must be 64 hex characters")
        return text
    if shape == DATE:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value)
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as error:
            raise JournalRefused(f"{where} must be an ISO date") from error
    if shape == TOKEN:
        text = str(value)
        if not _TOKEN.fullmatch(text):
            raise JournalRefused(
                f"{where} must be a short lower-case enum token (got {len(text)} characters); "
                "free text never goes into the journal"
            )
        return text
    raise JournalRefused(f"{where}: unknown field shape {shape!r}")


# --- Where the file is ------------------------------------------------------------------------


def journal_dir() -> Path | None:
    """The configured journal directory, or `None` while the journal is off."""
    configured = str(getattr(settings, "DELETION_JOURNAL_DIR", "") or "").strip()
    return Path(configured) if configured else None


def segment() -> str:
    """This process's segment name (`DELETION_JOURNAL_SEGMENT`, default `deletions`)."""
    name = str(getattr(settings, "DELETION_JOURNAL_SEGMENT", "") or "").strip() or "deletions"
    if not _SEGMENT.fullmatch(name):
        raise JournalRefused(f"DELETION_JOURNAL_SEGMENT {name!r} is not a plain lower-case name")
    return name


def journal_path() -> Path | None:
    """The file this process appends to, or `None` while the journal is off."""
    directory = journal_dir()
    return None if directory is None else directory / f"{segment()}.jsonl"


def segment_files(directory: Path | None = None) -> list[Path]:
    """Every segment file of the journal, in a stable order."""
    where = directory if directory is not None else journal_dir()
    if where is None or not where.is_dir():
        return []
    return sorted(path for path in where.glob("*.jsonl") if path.is_file())


# --- Writing ---------------------------------------------------------------------------------


def record(kind: str, *, now: datetime | None = None, **fields: Any) -> dict[str, Any]:
    """Journal one irreversible privacy action. Returns the line's payload (for tests and logs).

    The append happens **on commit**, so a transaction that rolls back leaves no entry: a replay
    must never delete something that was never deleted. Outside a transaction Django runs the hook
    at once. The payload is validated here, synchronously, so a bad call fails in the caller's
    stack rather than in a commit hook.
    """
    entry = {
        "at": (now or timezone.now()).isoformat(),
        "kind": kind,
        **_encode(kind, fields),
    }
    transaction.on_commit(lambda: append(entry))
    return entry


def record_many(kind: str, rows: Iterable[dict[str, Any]], *, now: datetime | None = None) -> int:
    """`record` for a batch of one kind. Returns how many lines were queued."""
    written = 0
    for fields in rows:
        record(kind, now=now, **fields)
        written += 1
    return written


def append(entry: dict[str, Any]) -> bool:
    """Append one already-validated line. Never raises: a deletion is honoured either way.

    Returns whether the line reached the file, so a caller that wants to alarm on it can.
    """
    global _warned
    path = journal_path()
    if path is None:
        if not _warned:
            _warned = True
            logger.critical(
                "DELETION JOURNAL is off (DELETION_JOURNAL_DIR is empty): irreversible privacy "
                "actions in this process are not journalled, so a database restore would silently "
                "roll them back (ADR 0057, DPIA R24)"
            )
        return False
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True, ensure_ascii=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(handle, line.encode("ascii"))
        finally:
            os.close(handle)
    except OSError as error:
        # The deletion has happened or is about to; refusing it because a file is unwritable would
        # be worse. Loud, and with no payload in the message.
        logger.critical(
            "DELETION JOURNAL could not append a %s line to %s (%s): that deletion will not "
            "survive a database restore (ADR 0057)",
            entry.get("kind"),
            path,
            error.__class__.__name__,
        )
        return False
    return True


# --- Reading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One journal line, parsed. `fields` are exactly the declared fields of its kind."""

    at: datetime
    kind: str
    fields: dict[str, Any]
    #: Which segment file and which line it came from, so a problem can be pointed at.
    source: str
    line_no: int

    @property
    def key(self) -> str:
        """The stable key, printable: `creator=41`, `organisation=3 sha256=1a2b…`."""
        return " ".join(f"{name}={self.fields[name]}" for name in sorted(self.fields))

    def get(self, name: str) -> Any:
        return self.fields.get(name)


@dataclass
class Journal:
    """Every entry the journal holds, oldest first, plus the lines that could not be read."""

    directory: Path | None
    entries: list[Entry] = field(default_factory=list)
    #: `file:line reason` for each unreadable line. Never the line itself.
    unreadable: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    @property
    def kinds(self) -> dict[str, int]:
        counted: dict[str, int] = {}
        for entry in self.entries:
            counted[entry.kind] = counted.get(entry.kind, 0) + 1
        return dict(sorted(counted.items()))


def _parse(raw: str, source: str, line_no: int) -> Entry:
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("not an object")
    kind = str(document.get("kind", ""))
    if kind not in KIND_FIELDS:
        raise ValueError(f"unknown kind {kind!r}")
    moment = datetime.fromisoformat(str(document["at"]))
    fields = {name: value for name, value in document.items() if name not in {"at", "kind"}}
    # Validate on the way in as well: a line that would not be accepted today is not replayed.
    return Entry(
        at=moment,
        kind=kind,
        fields=_encode(kind, fields),
        source=source,
        line_no=line_no,
    )


def read_journal(directory: Path | None = None, *, since: datetime | None = None) -> Journal:
    """Read every segment file, oldest entry first. Unreadable lines are reported, never skipped
    silently — a journal nobody can parse is the failure mode this whole feature exists against."""
    where = directory if directory is not None else journal_dir()
    journal = Journal(directory=where, files=segment_files(where))
    for path in journal.files:
        for line_no, raw in enumerate(_lines(path), start=1):
            if not raw.strip():
                continue
            try:
                entry = _parse(raw, path.name, line_no)
            except (ValueError, KeyError, TypeError, JournalRefused) as error:
                journal.unreadable.append(f"{path.name}:{line_no} {error.__class__.__name__}")
                continue
            if since is not None and entry.at < since:
                continue
            journal.entries.append(entry)
    journal.entries.sort(key=lambda item: (item.at, item.source, item.line_no))
    return journal


def _lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="ascii", errors="replace") as handle:
        yield from handle
