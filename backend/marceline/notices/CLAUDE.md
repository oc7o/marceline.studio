# Notice drafts

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Notice drafts (ADR 0035, issue #33)
- **Customer-sent drafts only — Marceline never transmits a real notice.** `marceline/notices/`
  composes the rights gate (ADR 0034), copy extent (`matching.copy_extent`) and the provider
  directory (ADR 0027) into one notice gate, renders a draft from the answer's canonical schema,
  and records what the customer does with it. Tables via `marceline/models.py`: `NoticeDraft`
  (canonical per organisation × recipient provider × channel, work/claimant/legal-input fields
  **snapshotted at drafting time**), `NoticeFinding` (one reported URL, `exception_confirmed` the
  per-finding tick), `NoticeConfirmation` (one row per kind, the exact text and version), `NoticeEvent`
  (**append-only**, migration `marceline/0011_notices_append_only`, same trigger technique as the
  rights logs).
- **Gate:** `notices/gate.py` `finding_blockers(FindingFacts)` is pure; `finding_gate_blockers`
  reads a snapshot and prepends the rights blockers unchanged. Codes: `no_high_confirmed_match`
  (band `high` + `status=confirmed` — a confirmed match *is* the human review step here),
  `page_not_available` / `availability_not_recent` (`NOTICE_AVAILABILITY_MAX_AGE_HOURS`, 48, no
  media fetch — reads what `crawl_recheck` already recorded), `licensed_partner_match`
  (`rights.partners.partner_for_url`), `exception_review_required`
  (`matching.copy_extent.requires_exception_review`; satisfied only by the per-finding
  `exception_confirmed` tick threaded from `CreateNoticeIn.exception_confirmed_page_ids`, never a
  batch-level default), `no_supported_channel` (`catalog.hosting.directory.provider_roles`, no
  role reaching `primary`/`delisting_draft` v1 support).
- **`gate.file_matches(organisation, sha256, page_ids)` is the one match lookup** `candidates`,
  `create_draft` and `approve` share, so the screen, the draft and its approval can never judge
  one copy differently (ADR 0035 amendment 2026-09-17). It reduces every match row of the file to
  one `FileMatch`: every reference **this organisation owns** counts (ADR 0022 — a re-fingerprint
  is a second `Reference`, and `confirmed_findings_of` already counts a page confirmed on any
  member as one confirmed copy), eligible needs one confirmed `high` row **and no rejected one**
  (an explicit human rejection vetoes — this is the gate's only human review step), and the extent
  is the most cautious of the rows (`copy_extent.combine_extents`), never row order. Never pass a
  single `ReferenceMatch` to `finding_gate_facts` again.
- **`notices/services.py` is the only writer.** `create_draft` re-checks every selected finding's
  gate and refuses the whole draft (`finding_not_eligible`, blockers per page id) rather than
  silently dropping the blocked ones. `approve` re-checks eligibility again (the world can have
  changed since creation), requires every `required_confirmations` kind confirmed at its
  **current** version (`confirmation_incomplete`), and freezes the exact rendered text + its
  sha256 on the draft and in the `approved` event. `mark_sent` (the customer's own "I sent this
  myself") is refused for a sandbox organisation (`sandbox_cannot_mark_real_send`); `sandbox_send`
  is the **only** place Marceline may email a draft, and only to a `NOTICE_SANDBOX_RECIPIENTS`
  address for a sandbox organisation's draft (`accounts.Organisation.is_sandbox`, staff-set,
  default `False`) — any other address or organisation is refused outright.
  `record_provider_response` feeds `catalog.HosterContact` (the response-history aggregate,
  ADR 0027) **except for a sandbox draft**, which never creates one — no code change was needed in
  `catalog/hosting/` itself, since that table already has no per-organisation scoping.
  `withdraw` / `correct` close a draft (`withdrawn` reachable from any open status;
  `correct` opens a fresh `draft` copy of the findings needing every confirmation again).
- **A safety-flagged page hides its whole draft** (ADR 0035 amendment 2026-09-17, ADR 0011/0018).
  A draft snapshots each reported URL, so `services.visible(draft)` re-reads every reported page
  through `marceline.app.safety.page_visible`: `_draft_for` answers the byte-identical 404 of an
  unknown id (so every action on the draft does too), the list leaves it out, and `create_draft`,
  `approve`, `sandbox_send` and `correct` refuse it in the writer. The row is **never deleted** —
  it is the audit trail of what the customer confirmed. A *rejected* match is still shown on
  purpose: the customer chose that URL and the draft is their own record; safety is not about who
  already knows.
- **Three facts come from our data, never the request** (same amendment): the chosen channel's
  `forwards_to_site_operator` / `publishes_to_transparency_db` (`services._channel_facts` reads
  the adopted `complaint_channel_id` from the directory — they decide whether the forwarding
  warning must be confirmed, and `catalog.ComplaintChannel` insists on a source URL for each);
  that the channel id is a current channel the directory offers for the chosen host (409
  `unknown_channel`, and no customer integer reaches the foreign key); and that
  `recipient.hostname` is a host the draft actually reports (409 `unknown_recipient`, so a
  customer cannot feed `catalog.HosterContact` for a provider unrelated to their findings).
  Widen the last one together with what `candidates` offers.
- **The human-review date is a snapshot, and it is in the evidence paragraph** (issue #142).
  That sentence used to open with a literal `[review date]` nothing ever substituted; the opening
  was then dropped, and this puts the date back from our own record.
  `matching.ReferenceMatch.decided_at` is the source — **not `updated_at`**, which is `auto_now` and
  moves when an extent classification touches the row months later, which in this sentence would
  claim a review later than it happened. `gate.FileMatch.confirmed_at` is the **earliest** confirming
  decision over the file's references, `NoticeFinding.reviewed_at` snapshots it at `create_draft`
  time (and `correct` copies it), and `_review_phrase` renders one date, a range when the reported
  locations were reviewed on different days, or **nothing at all** — never a placeholder, which
  `test_notices_renderers.py` enforces. The annex prints each row's own date. The gate already
  refuses a finding with no confirmed high row, so a missing date can only mean a decision taken
  before the column existed.
- **Renderers** (`notices/renderers.py`): one `NoticeContext`, four renderers — `dsa_art16`,
  `dmca_512c`, `dmca_512d` (search delisting), `provider_policy` (never claims a statute). No
  image, frame, thumbnail, PDQ hash, band or distance in any of them; the customer's own legal
  paragraph (rights + authorisation + exceptions declarations, joined) replaces Marceline's
  conclusion. **The two DMCA variants are always rendered in English**, whatever the draft's
  working language — `render()` converts the context before dispatch.
- **Export** (`notices/export.py`, ADR 0035 amendment 2026-09-18):
  `GET /api/v1/app/notices/{id}/export/{txt|eml|annex}` → `services.export_notice`. `txt` is
  `approved_text` **byte for byte** (the bytes the frozen sha256 names — never reformat it);
  `eml` is an unsent RFC 5322 message with `To`/`Subject` (`renderers.split_subject`) and
  `X-Unsent: 1` and **no `From`, `Date` or `Message-ID`** — the customer is the sender; `annex` is
  the evidence annex (per URL: first detected, last found available, extent, the same
  comparison-plus-human-review sentence; no hash, band, distance or frame count). No PDF: no
  channel asks for one and `billing/pdf.py` encodes to Windows-1252, which would mangle a URL.
  `export.py` is **pure and imports no transport** — `sandbox_send` stays the only path to mail.
  Refused before approval (`not_approved`), for a closed draft (`already_closed`) and for a
  safety-hidden one (the byte-identical 404, through `_draft_for`); an unknown format is that same
  404. Each download appends one `exported` `NoticeEvent` naming format, language, variant and the
  approved text's sha256 — **never a second copy of the text**. Both apps link to it as plain
  `<a download>`; the Next app through `app/api/notices/[noticeId]/export/[format]`.
- **Declarations** (`notices/declarations.py`, same discipline as `rights/declarations.py`): rights,
  identity, authorisation, exceptions, recipient, accuracy, forwarding_warning (required only when
  the channel is flagged `forwards_to_site_operator`/`publishes_to_transparency_db`, or unknown —
  `services.required_confirmations`). A new wording is a new version, never an edit; a stored
  `NoticeConfirmation` keeps the exact text shown.
- Customer API `marceline/app/notices.py` (`/api/v1/app/notices`: candidates, create, detail,
  approve, export, mark-sent, sandbox-send, response, withdraw, correct), same rules as every
  customer router (public ids, byte-identical 404, CSRF, no media/confidence).
- **The flow is served on production too since ADR 0046** — that was the separate ADR the note
  here used to promise, and the reason it was needed still holds: the gate reads live rights
  records, match rows, copy extent and the provider directory, none of which exist there, and
  every step writes. So neither half is answered locally on the public host.
  - **Reads come from the signed bundle.** `marceline/export.py` renders `notices`,
    `notices/{id}` for every draft `app.notices.visible_draft_ids` names,
    `notices/candidates/{original}`, the two `*` 404 documents and the three downloads per draft;
    `customer_cache.CACHED_ROUTES` serves the JSON ones. A draft the safety filter hides has **no
    documents at all**, and the notice documents' `page_id` fields feed the bundle's `page_ids`,
    so a safety `delete` purge drops the bundle that names the page. A draft that cannot be
    exported carries the very 409 development would answer (`store.DocumentIn` admits 200/404/409).
  - **The download is the one cached read a view serves** (`_export_from_cache`): recording it is
    a write (the `exported` event), and a write does not belong in a middleware. It takes the
    document, answers it with the headers development sets (`export.filename_for`) and enqueues
    `notice_exported`. **Rendering a bundle is not a download** — `export.render` marks the request
    and the view then uses `services.export_payload`, the same formatter without the event, and
    never reads the cache. Never make that view read the cache on a bundle render: it would loop.
  - **Writes** are six outbox kinds plus `notice_exported` (`delivery.OutboxItem.Kind`), with
    handlers in `marceline/customer_writes.py` that call the functions these endpoints call and
    resolve the draft through `app.notices.draft_for` — so the tenancy rule, the safety filter and
    the gate are applied **on development**, against today's data. A write production accepted can
    still be refused (`notice_not_found` for a draft hidden meanwhile). `GET /app/notices/submissions`
    is the production-local read that reports it, and both customer apps render it
    (`NoticeSubmissionsBlock`, issue #86, ADR 0046 §7): `finding_not_eligible` /
    `finding_no_longer_eligible` fold every blocker of every page into one code, so
    `customer_writes.py _submission_code_for` narrows it — all rights-side blockers to
    `finding_rights_blocked`, all availability-side to `finding_unavailable`, anything mixed
    unchanged — from the same blocker codes the live create/approve screen already shows; every
    other refusal a customer can cause reuses that screen's own sentence. Production checks only session,
    CSRF, `upload_throttle.Kind.NOTICE_WRITE` and the schema, then answers 202
    `NoticeWriteAcceptedOut`; a still-pending duplicate is one write (`OutboxItem.dedupe_key`: the
    draft's id, or `original_id|hostname|channel_kind` for a create).
  - **`sandbox-send` stands before the id in the path** (`/api/v1/app/notices/sandbox-send/{id}`)
    because `ROLE_ONLY_PREFIXES` is a prefix allowlist and this is the one path in the product
    that may hand a draft to a mail transport. Never move it back, and never add a second mail
    path: `test_notices_on_production.py` walks every read and write of the flow, applies every
    outbox item and asserts the mail outbox stays empty.
- `marceline/app/cases.py`'s `notice_prepared`/`notice_sent`/`notice_acknowledged` timeline steps
  read `notice_stamps_of(original)` — the earliest `approved_at`/`marked_sent_at`/`responded_at`
  across every `NoticeDraft` of that original (scoped by the original's own globally-unique public
  id) — a historical fact a later withdrawal never erases. `notice_delivery` (Marceline's own
  transmission) stays `unavailable`, blocked on #46.
- Settings: `NOTICE_AVAILABILITY_MAX_AGE_HOURS` (48), `NOTICE_SANDBOX_RECIPIENTS` (comma-separated,
  empty by default — the sandbox path always refuses until set), `NOTICE_SANDBOX_FROM`.
- Admin: *Marceline → Notice drafts / Notice events*, read-only; every write goes through
  `services`, driven by the customer screen.
- **The one thing that deletes a draft is `customer_data_retention`** (ADR 0045, issue #85): an
  organisation whose pilot ended, or one of its works marked for removal (which an owner switch
  does by itself), `CUSTOMER_DATA_RETENTION_DAYS` after a date staff set. A draft is otherwise
  never deleted — `withdraw`/`correct` close it, and `visible()` hides a safety-flagged one. The
  export written before the deletion carries every draft with its confirmations and its event log,
  because that audit trail is the customer's own and is what they lose.
