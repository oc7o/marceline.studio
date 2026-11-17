# 0057 — A deletion journal outside the database, and a replay after every restore

Status: accepted (2026-09-19). Closes the procedure half of issue **#137** (c);
the retention *period* stays operator decision **#140 D3** and is not touched here. Builds on
ADR 0029 (page deactivation, the text scrub and page retention), ADR 0038 (index suppression),
ADR 0045 (customer data deletion), ADR 0047 (the do-not-contact record), ADR 0048 (retention on the
research side) and issue #66 / docs/safety.md (safety-incident retention).

## Context

Six features in this product delete personal data on purpose, and each of them is a promise:

| Promise | Where | What it deletes |
|---|---|---|
| "we stop indexing you" | ADR 0038, docs/suppression.md | page-creator links, breadcrumbs, name-bearing stored text, the discovery trail |
| "a deactivated page stops naming people" | ADR 0029, issue #148 | `title`, `tags`, performer credits — 54,669 pages on 2026-09-19 |
| "we do not keep a page forever" | ADR 0029 amendment | the page row, 24 months after deactivation |
| "a settled safety case is deleted" | docs/safety.md, issue #100 | the incident, its copied URL, its retained hashes |
| "your data goes 30 days after the pilot" | ADR 0045 | originals, rights record, notice drafts, case files, accounts |
| "research nobody acted on is forgotten" | ADR 0048 | prospect verdicts, watchlist entries, catalogue names, query text |

And one feature *adds* a permanent record for the same reason: the do-not-contact record (ADR 0047),
"we will not write to you again".

The database is dumped nightly and the dump is kept for months (`infra/backup/`, restic
`last3 / 7d / 4w / 6m`). **A restore undoes all six promises at once, and silently.** Two cases,
both bad:

- the snapshot is **after** the request: the suppression marker comes back with it, so no *new*
  link is written — but every link and breadcrumb the suppression removed stands again;
- the snapshot is **before** it: the marker is gone too and the suppression simply does not exist.

The do-not-contact record fails in the third, worst direction: it is append-only, so a restore
resurrects nothing — it **loses** every objection given since the dump, and the next contact check
asks `may_contact`, is told yes, and we write to somebody who said no.

Nothing anywhere recorded what had been deleted. A restore had no way back except an operator's
memory.

## Decision

### 1. A journal outside the database's blast radius

`core/deletion_journal.py`. Every irreversible privacy action appends **one line** to a file in
`DELETION_JOURNAL_DIR`:

```json
{"at":"2026-09-19T14:02:11+00:00","kind":"creator_suppression","basis":"objection",
 "creator":412,"identity":"platform_message","requested_on":"2026-09-18"}
```

**Why a file and not a table.** A table is inside the thing being restored: `pg_restore` would roll
the journal back together with the deletions it records, losing exactly the entries that matter. A
file also has two properties a table cannot give us here: it is append-only and monotone, so **any**
copy of it is a prefix of the truth and the newest copy is always the one to replay, whatever the age
of the database; and it can travel with the backups, so "the machine is gone"
(docs/runbooks/backup-restore.md § D) still has it.

**Where.** Next to the nightly dumps — `~/backups/marceline/journal`, mode 0700 — because
`infra/backup/backup-run.sh` then pushes it to the same encrypted off-host repository under its
**own** restic tag (`marceline-deletion-journal`), independently of the dump. Its retention and its
staleness are therefore never mixed with the database's, the same reason the `~/.claude` snapshot has
its own tag. It is pushed even on a night the dump fails: it is the record of what restoring that
dump would undo.

**One directory, several segment files.** The writers have different identities — the host-run
management commands as the operator, `web` and `scheduler` as root inside a container, over one
bind mount. A single shared file would be created by whichever wrote first and be unappendable for
the others, so each process writes `<DELETION_JOURNAL_SEGMENT>.jsonl` and the replay merges the
directory by timestamp. Every append is one `os.write` on a descriptor opened `O_APPEND`, so two
writers never interleave a line.

### 2. What a line may contain — enforced, not promised

A kind, a timestamp, and **only fields the kind declares**, each with a declared shape: an integer
row id, 64 hex characters (a sha256 or an HMAC-SHA256 digest), an ISO date, or a lower-case enum
token of at most 32 characters. `_encode` refuses everything else, so no future caller can put a
performer's name, a page title, a piracy URL, a postal address, a handle, a staff name or a
free-text note into the file — not by accident and not deliberately. A test walks every field of
every kind, and another searches a real suppression run's journal for the name, the alias, the
title, the URL and the operator's name.

**This is affordable because the restored database still holds the name.** A restore produces a
database that is *older* than the deletion, not one with the name missing, so a line only has to say
*which row* and *which rule*. The one exception is the do-not-contact record, whose stored form
**is** a keyed digest: that digest can be carried, because it is not reversible and is useless
without `DO_NOT_CONTACT_HMAC_KEY` — and carrying it is the only way to re-insert a row a restore
lost.

Three kinds carry two or three extra tokens because the row they re-create is a legal record: a
suppression's `basis` (Art. 21 / Art. 17 / operator), its `requested_on` (the Art. 12(3) clock) and
its `identity` check code; a do-not-contact record's `identifier_kind`, `received_on` and `channel`.
The free-text parts of both — the note, the identity note, `decided_by`, `recorded_by` — are **not**
carried, and a replayed row says so in its own note instead of pretending.

### 3. One hook per deletion path, at its single writer

Thirteen kinds, each hooked at the one function that performs the deletion rather than at the
commands that call it, so the crawl worker, the scheduler, a management command and the admin are all
covered by one call:

| Kind | Hook |
|---|---|
| `creator_suppression`, `creator_suppression_lifted` | `marceline.suppression.apply_suppression` / `lift_suppression` |
| `page_text_scrub` | `index.retention.scrub_deactivated_pages` (the deactivation scrub **and** #148's backfill) |
| `page_deleted` | `index.retention.apply_retention` |
| `safety_incident_deleted` | `index.incidents.apply_retention` |
| `search_query_scrubbed`, `candidate_queries_scrubbed` | `crawler.retention.scrub_search_calls` / `apply_query_retention` |
| `prospect_research_forgotten`, `creator_name_deleted` | `marceline.name_retention.apply_prospect_retention` / `apply_name_retention` |
| `organisation_data_deleted`, `original_data_deleted` | `marceline.deletion.retention.delete_organisation` / `delete_original` |
| `case_file_reset` | `marceline.deletion.handover.apply` |
| `do_not_contact_recorded` | `catalog.DoNotContactRecord.save` |

The last one is the model's `save()` and not `catalog.services.record_no_contact`, because that model
has two writers: the service and the admin form, which sets `identifier_hash` itself.

**Written from `transaction.on_commit`.** A transaction that rolls back therefore leaves no entry,
which matters more than it sounds: an entry for a deletion that did not happen would make a replay
delete something nobody asked to delete. The residual gap — a crash between the commit and the
append — leaves a deletion unjournalled, which is exactly today's state and so no regression.

**A failed append never fails a deletion.** An Art. 21 objection is honoured even when the journal's
disk is full; the append logs `CRITICAL` and the deletion proceeds. Likewise an unconfigured journal:
the first deletion in the process logs `CRITICAL` once, saying that it will not survive a restore.

### 4. `manage.py reapply_deletions` — dry run by default

`marceline/deletion_replay.py` walks the journal oldest first and re-applies each deletion **through
the same function that performed it**, so there is no second implementation of what a suppression
removes. Per kind it reports: journal entries, already in effect, re-applied, could not be applied
(with the key), needs a human. `--since` takes the dump's timestamp; `--journal` reads a copy
restored out of the off-host repository. Every kind asks about the current state first, so a second
run reports `already` and writes nothing.

**Why a replay may be trusted to delete.** The restored database is a strictly older state of the
same database, and every journal line is newer than every row in it, so nothing it is about to delete
can have acquired a reason to be kept since. Where that argument is not quite enough, the entry is
**listed for a human** rather than acted on.

**What it will not do, and lists instead:**

- **lift a suppression** (`creator_suppression_lifted`). The reason is free text, which never enters
  the journal; and a restore that loses a lift leaves the name suppressed, which is the safe side.
- **suppress a name whose `catalog.Creator` row the restore predates.** There is nothing to attach
  the marker to and no name in the journal to create one from. Reported as could-not-apply, by id.
- **invent an ADR 0045 mark.** Both customer scopes run off a date **a human set**; with no
  `OrganisationEnding` / `OriginalRemoval` in the restored database the entry says to re-mark it with
  `end_pilot` / `remove_original`.
- **settle a safety case, or delete a page an open or reported case is attached to.** A human closes
  a case; a replay does not.
- **delete a page whose URL digest does not match the journal's.** The digest travels for exactly
  this reason.

It also refuses to run at all when no journal is configured: answering "nothing to do" there would be
the most dangerous possible answer.

### 5. The restore is not finished until the replay has run

`docs/runbooks/backup-restore.md` § C and `docs/runbooks/restore-drill.md` put
`reapply_deletions --apply` **before** anything is started against the restored database, and
`infra/backup/pg-restore-check.sh` proves weekly, with nobody present, that a replay of the current
journal onto a freshly restored scratch database leaves **zero** entries un-applied. It runs there
with `--apply` — safe in that one place, because the target is `marceline_restore_check` and it is
dropped moments later. `backup-check.sh` additionally alerts when the journal is unconfigured or its
off-host push is overdue.

## Consequences

- A restore is repairable.
- The journal is one more thing that has to be configured, and a container that does not receive
  `DELETION_JOURNAL_DIR` journals nothing. Hence: `web` and `scheduler` get it in
  `infra/compose.yaml` (the two containers that run a journalled deletion), a bind mount at **the
  same path** inside the container so one `.env` value serves the host commands and both containers,
  and `marceline/tests/test_compose_env.py` pins every half of that — the seventh instance of the
  silently-empty-setting bug of 2026-09-18.
- **Crawler and media containers deliberately do not get it.** They parse untrusted HTML and must not
  hold a writable mount into the operator's backup directory, and they run no journalled deletion
  path. If one ever does, the `CRITICAL` line is what says so.
- The journal is a new artefact holding a record about people, so it is minimised the way everything
  else here is: ids, digests, dates and enum codes, mode 0600 in a 0700 directory, encrypted before
  it leaves the machine, and inside the same off-host retention regime as the dumps. It is **not** a
  second suppression list and cannot be read as one: nothing in it names anybody.
- Re-applying a suppression writes a marker whose note says the original note and decider are in no
  backup. That is a small, honest loss of accountability metadata, taken deliberately over carrying
  free text out of the database.
- The retention **period** is untouched. If #140 D3 shortens the snapshot policy to 90 days, the
  journal's own tag can keep its longer policy — a shorter dump retention makes the replay window
  shorter, never the journal less useful.
