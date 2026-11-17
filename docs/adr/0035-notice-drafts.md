# 0035 — Notice drafts: customer-sent, never transmitted by Marceline

Status: accepted (2026-09-17). Operator decision the same day: build "Prepare notice draft" now,
customer-sent drafts only — nothing is ever sent to a real recipient by Marceline. Related: issue
#33, ADR 0034 (rights model and
proof vault, which this feature gates on), ADR 0027 (hoster intelligence and provider directory,
which this feature routes to), ADR 0015 amendment (copy extent, which this feature reads).

## Context

A human-reviewed technical match says a page carries the same audiovisual content as a customer's
original. It says nothing about who holds the right to that content, whether the claimant is
authorised, whether the particular use is licensed, or whether it is unlawful — the rights model
(ADR 0034) records the first three; this change adds the fourth gate (copy extent, licensed
partners, availability) and turns a fully-gated finding into a draft notice the customer reviews,
confirms and sends themselves.

The model is draft-only: Marceline prepares a factual
draft from technical findings and the customer's own confirmations; the customer makes every legal
assertion, chooses the recipient, signs and sends. This ADR builds
exactly that draft-only model, locks the sandbox path so no real notice can leave the operator's
test organisation, and leaves production transmission for a later change (issue #46).

## Decision

### Where it lives: the project package

`marceline/notices/` (models registered through `marceline/models.py`, migrations
`marceline/0010_notices`, `0011_notices_append_only`), the same reasoning as ADR 0034's
`marceline/rights/`: a notice draft joins `accounts.Organisation`, `matching.ReferenceMatch` /
`copy_extent`, `catalog.hosting` (the provider directory) and the rights gate together — a
combination no single app's import-linter contract allows, and adding one would need a new
contract for a table that mostly serves the project's own customer API. No import-linter changes.

### The notice gate composes existing helpers

`marceline/notices/gate.py`, pure where the rights gate is pure, read-only where it reads the
database. `finding_blockers(FindingFacts) -> list[str]` checks, in a fixed order:

- **match band `high` + human content-identity confirmation** — `matching.ReferenceMatch` with
  `status=confirmed` and `band=high`; in this codebase a match reaching `confirmed` status *is* the
  human review step (the review queue), so no new "reviewed" flag was needed;
- **a recent availability check with no media download** — `index.Page.availability=playable` and
  `availability_checked_at` within `NOTICE_AVAILABILITY_MAX_AGE_HOURS` (48). The gate never fetches
  a page or its media itself; it reads what `crawl_recheck` already recorded;
- **rights blockers empty** — `marceline.rights.gate.notice_gate_blockers`, unchanged, its codes
  pass through verbatim;
- **licensed-partner check** — `marceline.rights.partners.partner_for_url`; a match on the
  organisation's own licensed partner domain blocks the finding (the use is not unauthorised);
- **copy-extent exception review** — `matching.copy_extent.requires_exception_review`; true for
  anything but a current `full_copy` (an excerpt, a compilation candidate, or not determined). Such
  a finding is blocked until the customer separately ticks the per-finding exception confirmation
  (`NoticeFinding.exception_confirmed`, set only at draft creation from an explicit list of page
  ids the customer ticked — never inferred, never defaulted true);
- **a provider role with a supported v1 channel** — `catalog.hosting.directory.provider_roles`;
  blocked when no role's channel reaches `primary` or `delisting_draft` v1 support (ADR 0027
  amendment).

`review`/`low` band and a rejected match never reach `band=high` + `confirmed`, so they are never
eligible by construction; no separate check was needed for them.

> **Superseded in part by the 2026-09-17 amendment below.** A file has several `Reference` rows, so
> "a rejected match never reaches high + confirmed" is true of one row and not of a page: the match
> facts now come from `gate.file_matches`, which reads every reference of the file the organisation
> owns, needs one confirmed `high` row among them and lets any rejected row veto.

### Data model

| Table | What |
|---|---|
| `NoticeDraft` | canonical per (organisation, recipient provider, channel): status, sandbox snapshot, work/claimant/legal-input fields **snapshotted at drafting time** from the rights record and the confirmed finding, the frozen `approved_text` + its sha256, every stage's timestamp and actor |
| `NoticeFinding` | one reported URL (`reported_material[]`), snapshotted (first seen, last checked, availability, extent) at drafting time; `included` lets a URL be dropped before approval; `exception_confirmed` the per-finding tick |
| `NoticeConfirmation` | one row per required confirmation kind, the **exact text and version confirmed**, replaced (not appended) if a draft is re-approved |
| `NoticeEvent` | append-only (migration `0011_notices_append_only`, same trigger technique as the rights logs): every status change, with the rendered text's sha256 on `approved` |

Status machine: `draft -> customer_approved -> marked_sent_by_customer -> provider_responded`
(with `outcome` = `removed`/`refused`/`no_response`), `withdrawn`/`corrected` reachable from any
open status. `correct` closes the draft and opens a fresh `draft` copy of its findings — nothing
carries over except the facts; every confirmation has to be given again.

### Renderers

`marceline/notices/renderers.py`: one canonical `NoticeContext`, four renderers —
`dsa_art16`, `dmca_512c`, `dmca_512d` (search delisting), `provider_policy` (a provider's own
copyright channel, deliberately never claiming a statute). Every renderer states content identity
as a technical finding, carries the customer's own legal paragraph (rights + authorisation +
exceptions declarations, joined) rather than Marceline's, and attaches no image, frame, thumbnail,
PDQ hash, band or distance. **The two DMCA variants always render in English**, regardless of the draft's working
language: a US recipient expects the statutory §512 wording, not a translation of it; DSA and the
provider-policy channel render in the customer's chosen language, because either can go to an
EU or non-EU provider that accepts German or English.

### Confirmations, versioned and verbatim

`marceline/notices/declarations.py`, the same discipline as `rights/declarations.py`: rights,
identity, authorisation, exceptions, recipient, accuracy, and a forwarding/publication warning
(required only when the chosen channel is flagged `forwards_to_site_operator` or
`publishes_to_transparency_db`, or that is unknown). A new wording is a new version; a stored `NoticeConfirmation` keeps the version
and the exact text shown, never today's copy. `approve()` refuses (`confirmation_incomplete`) unless
every required kind is present, ticked, and confirmed against the *current* version.

### Sandbox transmission

`NOTICE_SANDBOX_RECIPIENTS` (env, default empty) is the only allowlist Marceline may ever send an
email copy of an approved draft to, and only for a draft whose organisation has `is_sandbox=True`
(`accounts.Organisation.is_sandbox`, a new field, default `False`, set explicitly by staff — never
inferred). `services.sandbox_send` refuses any other address (`recipient_not_allowlisted`) and
refuses for a non-sandbox organisation (`not_sandbox`); the ordinary `mark_sent` ("I sent this
myself") is refused for a sandbox organisation (`sandbox_cannot_mark_real_send`) — a sandbox draft
can only ever be marked sent through the allowlisted email path, never claimed as a real send.
Provider responses recorded on a sandbox draft never create a `catalog.HosterContact` row
(`services._record_cooperation` returns `None` for `draft.is_sandbox`), so a sandbox notice can
never move the rounded provider response history a real customer sees (ADR 0027 amendment).

### Response history: no change needed

`catalog.hosting.services.notices_by_provider` already reads every `HosterContact` row for a
provider id, with no per-organisation scoping (the response history is a cross-customer aggregate
by design). Since sandbox drafts never create a `HosterContact` row at all, wiring this feature in
was exactly `services.record_provider_response` creating one (`provider` from
`HostingRecord.objects.filter(hostname=recipient_hostname).first().provider_id`, silently skipped
if that host has no hosting record or no linked `HostingProvider`) — no change to
`catalog/hosting/history.py` or `services.py` themselves.

### The case-file timeline is wired to real data

`marceline/app/cases.py`'s three notice steps (`notice_prepared`, `notice_sent`,
`notice_acknowledged`) were `unavailable` placeholders naming issues #33/#46. They now read
`notice_stamps_of(original)` — the earliest `approved_at` / `marked_sent_at` / `responded_at`
across every `NoticeDraft` ever created for that original, scoped only by original's own public id
(unique across organisations, so no explicit organisation filter is needed) — a **historical**
fact, so a later withdrawal never un-happens it. `notice_preparation` and `cooperation_history`
are removed from `NOT_AVAILABLE`: they are real capabilities now. `notice_delivery` (Marceline's
own transmission) stays `unavailable` with `legal_review_pending`, blocked on #46.

### Customer API and screen

`/api/v1/app/notices` (`marceline/app/notices.py`): candidates per record (findings with their
blockers and suggested provider/channel), create, detail, approve, mark-sent, sandbox-send,
response, withdraw, correct. Same rules as every other customer router: scoped by `ownership`,
public ids only (`notice_`, plus the existing `orig_`/`page_`), a malformed, foreign or unknown id
is one byte-identical 404, CSRF on every write, no media, no confidence, no band, no hash.
**Development only** (`public_surface.ROLE_ONLY_PREFIXES["dev"]`): nothing here is ever sent to a
real recipient, and the production outbox for a future real transmission is a separate, later
change this ADR does not touch or wire toward.

`/app/originals/{originalId}/notices` (`frontend/src/app/NoticeDraftScreen.tsx`): a "Prepare notice
draft" link from the case file (`CaseFileScreen`'s options section, previously a disabled
placeholder) opens the screen for the record. Wording follows `frontend/CLAUDE.md` throughout —
"matching copy", "human-reviewed match", never "leak", "confirm", "case", "verified" or "we send
it for you"; the actual confirmation and rendered-notice texts are server data shown verbatim, not
i18n copy, so the wording test does not (and must not) scan them.

### Staff side

Django admin (*Marceline → Notice drafts / Notice events*), read-only: every write goes through
`marceline.notices.services`, driven by the customer screen. Inlines show findings, confirmations
and the append-only event log per draft.

## Amendment 2026-09-17 — a safety-flagged page hides its draft, and three facts the caller does not state

A security review of this feature found four holes, all of the same shape: a draft keeps its own
snapshot of facts, and something that was true at drafting time was never re-read.

**A draft whose reported page becomes safety-flagged is never served, never approved, never sent —
and never deleted.** `NoticeFinding.url_snapshot` froze the URL, `approved_text` froze it again,
and nothing re-read the page, so a page flagged *after* a draft was prepared (a re-crawl, a
changed term list, `safety_scan`) stayed visible in `GET /app/notices` and `/app/notices/{id}`,
could still be approved, and on the sandbox path could still be emailed. The notice gate did not
catch it either: `ReferenceMatch` rows survive a block, so `no_high_confirmed_match` still passed.
That contradicts ADR 0011 and ADR 0018 — a customer response never mentions a safety-flagged page,
not even as a count — and the page in question is exactly the kind the media gate exists for.

`services.visible(draft)` re-reads every reported page through `marceline.app.safety.page_visible`
and is the single chokepoint: `_draft_for` answers the same 404 as an unknown id, so the detail
view *and every action on the draft* do; the list leaves it out; `create_draft`, `approve`,
`sandbox_send` and `correct` refuse it in the writer as well. The record itself is **kept**: it is
the audit trail of what the customer confirmed, and only staff read it from then on. The rule is
deliberately narrower than "any finding that stopped being visible":

> A draft whose match a reviewer later **rejects** is still shown. Rejection means "we no longer
> think this is your work" — the customer already chose that URL, and the draft is their own record
> of what they did, unlike the case file, which is *our* record of findings and therefore deletes
> it. Safety is different: the harm is in the page, not in who knows about it, so the safety filter
> hides the draft whatever the customer already saw.

**Three facts are read from our own data, never from the request.**

- The chosen channel's *forwarding and transparency-publication practice*. These two flags decide
  whether the `forwarding_warning` declaration must be confirmed before approval — the warning
  that the notice, with the customer's name and address in it, reaches the site's operator or a
  public transparency database. They arrived in `RecipientIn`, so a caller sending `false, false`
  waived their own warning and the `NoticeConfirmation` that records it. This ADR already said
  "required only when **the chosen channel is flagged**", and `catalog.ComplaintChannel` insists on
  a source URL for each flag: they are facts staff sourced. `services._channel_facts` now reads
  them from the adopted `complaint_channel_id`, and the two fields are gone from the API.
- *Which channel* was adopted. `complaint_channel_id` went straight into a foreign key, so an
  unknown id raised `IntegrityError` at commit — a 500, and an existence oracle for the staff
  directory's primary keys. It must now be a current channel the directory offers for the chosen
  host (409 `unknown_channel`).
- *Which host* the draft is addressed to. `recipient.hostname` was free text, and
  `record_provider_response` writes `catalog.HosterContact`, the rounded response history other
  organisations are shown next to their own findings (ADR 0027 amendment). A customer with one
  eligible finding could therefore point drafts at any provider in the directory and feed its
  statistics. The recipient must be a host the draft actually reports — the hosts `candidates`
  offers roles for (409 `unknown_recipient`). Widen it together with what `candidates` offers.
  *Still an operator decision:* a customer can report responses about the providers of their own
  findings, with self-chosen outcome and date, and those self-reports move a shared aggregate.

**One match lookup for the whole file, with a rejection veto.** `candidates`, `create_draft` and
`approve` each looked matches up differently — the representative's rows, or `reference__sha256`
collapsed to whichever row came last — so the screen, the draft and its approval could disagree
about one copy. `gate.file_matches(organisation, sha256, page_ids)` is now the one lookup all three
share, and it reduces every match row of the file to one `FileMatch`:

- every reference of the file **this organisation owns** (`ownership.owned_references`) counts. A
  re-fingerprint is a second `Reference` (ADR 0022) and `confirmed_findings_of` already counts a
  page confirmed on any member as one confirmed copy, so refusing a notice for it was wrong. A
  reference the organisation does not own is not evidence for its notice, and a file it no longer
  owns has no entry at all;
- eligible needs **one confirmed `high` row and no rejected one**. An explicit human rejection
  vetoes whatever another sampling grid says: `no_high_confirmed_match` is this gate's only human
  review step, and a second row must not stand in for a decision taken against it;
- the extent is the **most cautious** of the rows (`copy_extent.combine_extents`), never row order,
  so the stored `extent_snapshot` and the exception review match what the customer was shown.

## Amendment 2026-09-18 — the cooperation row opens when the notice goes out, not when a reply comes

The path this ADR built stopped one step short of the loop the operator asked for: "I sent a
notice and we noticed it came down."

`catalog.HosterContact` — the row `marceline.hosting.observe_notice_removals` reads, and the row
the rounded provider response history (ADR 0027 amendment) is aggregated from — was created only
in `record_provider_response`. But the ordinary outcome of a takedown notice is that the copy
disappears and **nobody writes back**. Such a notice produced no row, so the pages it named were
linked to nothing, `observe_notice_removals` had nothing to stamp, and neither the customer nor
the provider directory ever learnt that it had worked. The one case that closed the loop was the
case where the customer had already been told the answer.

`services.mark_sent` now opens the row itself, at send time, with `outcome=awaiting` and the
included findings' pages linked; `record_provider_response` settles that same row instead of
creating a second one (and still opens one for a notice sent before this change). Nothing else
needed to move: `catalog.hosting.speed` and `.cooperation` already read `awaiting` exactly right —
a young open notice is neither a success nor a failure, it counts as "never" only once it is older
than `OPEN_NOTICE_WINDOW`, and `catalog.hosting.history.response_history` leaves it out until its
removal question is settled. A sandbox draft still never gets a row, so a test notice still cannot
move a real provider's statistics.

**A withdrawn notice stops counting.** With the row opened at send time, a notice the customer
took back after sending would sit in `HosterContact` and, once older than `OPEN_NOTICE_WINDOW`,
count as "never removed" against a provider they had told to disregard it — `mistaken_match` is
the customer's error, not the provider's. `withdraw` deletes that row. `HosterContact` is the
statistics feed, not the audit trail: the append-only `NoticeEvent` log and the draft itself keep
what happened, and both are untouched.

`removed_observed_at` reaches the customer as `NoticeOut.removal_observed_at` — "our availability
checks first found every page named here gone on <date>". It is stated as our own observation,
because that is what it is: a check that runs about once a day, never a provider's confirmation
and never a promise. It is null while any named page is still up, and null for a sandbox notice.

**The Next.js customer app could not reach this feature at all.** ADR 0036 ported
`NoticeDraftScreen` and its BFF routes, but not the link: the record screen rendered "Prepare
notice draft" as the generic disabled affordance for an option with no endpoint, so on the app
production serves (`CUSTOMER_APP=next`) the notice path was unreachable while being fully built
underneath. It is a link now, as it has been in the SPA since this ADR, and
`screens.test.tsx` asserts the href rather than only the label.

## Amendment 2026-09-18 — the customer can take the notice away as a file

The notice flow names three ways the customer sends the draft themselves: copy the text, open it
in their own mail program, download it. Copy and a `mailto:` link shipped with the screen; the
download did not, so the last step of the flow existed only on paper.

`GET /api/v1/app/notices/{id}/export/{fmt}` (`marceline/notices/export.py`, pure;
`services.export_notice` is the writer) answers with a file, in three formats chosen from what the
provider directory's channels (ADR 0027 amendment) actually need, not from what is possible:

- **`txt`** — the approved text, byte for byte. A web-form channel is filled by pasting, and these
  are the bytes whose sha256 the draft froze at approval; it must stay byte-identical, or the
  checksum in the record stops meaning anything.
- **`eml`** — an unsent RFC 5322 message for an email channel, `To` and `Subject` pre-filled
  (`renderers.split_subject` moves the notice's own first line into the header) and `X-Unsent: 1`
  so a mail client opens it as a draft. It carries **no `From`, `Date` or `Message-ID`**: those
  are the sender's, the customer is the sender, and a Marceline address in a `From` header would
  be precisely the claim this ADR exists to avoid. A `mailto:` link cannot replace it — clients
  and browsers truncate a long `mailto:` body, and a notice listing several URLs is long.
- **`annex`** — the evidence annex: per reported location the URL, when it was first detected and
  last found available, the extent of the copy, and the same "perceptual comparison plus a human
  review" sentence the notice body uses, with the exception review stated only where one was
  required. For a form's "additional information" field and for the customer's own file. It makes
  no legal assessment and discloses **no fingerprint value, band, distance or frame count**.

**No PDF.** No channel in the directory asks for one, and the repository's only dependency-free
PDF writer (`billing/pdf.py`) encodes to Windows-1252, which would silently write `?` into a
reported URL or a title. A mangled URL in a document the customer then sends is worse than no PDF.
Revisit if a real recipient channel ever asks for a signed document.

**Nothing is transmitted.** `export.py` builds bytes and imports no transport; `sandbox_send`
remains the one place a draft may be handed to a mail transport, and a test asserts the mail
outbox stays empty across all three downloads.

**The export is not a way around the confirmations.** It is refused before approval
(`not_approved`), for a withdrawn or corrected draft (`already_closed` — such a draft keeps its
text on screen as the customer's own record, but it is not a notice to send), and, like every
other action on a draft, for one whose page has become safety-flagged (`_draft_for` →
`services.visible`, so the answer is the byte-identical 404 of an unknown id). An unknown format
gets that same 404.

**The audit trail stores nothing new.** Each download appends one `NoticeEvent` of the new kind
`exported` (migration `marceline/0016_notice_event_exported`, choices only) whose `detail` names
the format, the language, the variant and the **approved text's sha256** — never a second copy of
the text, which the draft already holds exactly once.

Both customer apps show the three links next to "copy" and the `mailto:` link, as plain
same-origin `<a download>` links (the invoice PDF's pattern): the SPA points at the customer API,
the Next app at its own BFF route `app/api/notices/[noticeId]/export/[format]`, which relays it
and passes `Content-Disposition` through.

**Still development-only.** `/api/v1/app/notices` stays in `ROLE_ONLY_PREFIXES["dev"]`, so the
export is 404 on the public production host like the rest of the feature. Making draft preparation
and export work there is not a matter of removing the prefix: the notice gate reads live rights
records, match rows, copy extent and the provider directory — none of which enter a signed bundle
(ADR 0028) — and every step of the flow writes (draft, confirmations, approval, and now the
`exported` event), which on production may only happen through the outbox development pulls and
applies. That is a separate change with its own ADR, as this ADR's consequences already say.

## Consequences

- Two new migrations in `marceline` (`0010_notices`, `0011_notices_append_only`) plus one in
  `accounts` (`0004_organisation_is_sandbox`). Apply from the main checkout, restart workers.
- New settings: `NOTICE_AVAILABILITY_MAX_AGE_HOURS` (default 48), `NOTICE_SANDBOX_RECIPIENTS`
  (comma-separated, default empty — the sandbox path always refuses until the operator sets it),
  `NOTICE_SANDBOX_FROM` (empty uses Django's default from-address).
- `accounts.Organisation.is_sandbox` must be set to `True` for the operator's test organisation by
  hand (admin) before the sandbox path can be exercised end to end; it is `False` for every
  existing and new organisation by default, so nothing changes for real customers.
- The rendered notice text is customer-facing copy. Nothing here sends a real notice; a wording
  change is a new declaration/renderer version, not an edit in place.
- `delivery/` (the production read-through cache and its outbox) is untouched by this change. A
  parallel effort is wiring production's customer-facing outbox; this feature's write endpoints
  stay development-only (`ROLE_ONLY_PREFIXES`) specifically so the two efforts cannot collide —
  when a production sending/outbox path is built, it is a new ADR, not an extension of this one.

## Alternatives rejected

- **Marceline sends the notice.** Out of scope by design: the customer makes every legal assertion
  and sends. Not built at all in this change — there is no code path that can transmit a real
  notice, sandbox included.
- **One universal notice template for every recipient.** DSA Art. 16
  and DMCA §512(c)/(d) are different statutory schemes with different mandatory elements and
  declarations; a superset template would either omit a required DMCA element or attach an
  unnecessary US perjury declaration to an EU-only notice.
- **Infer the notice channel from the recipient's country.** Rejected per ADR 0027's amendment
  already in place: location/nexus is a hint, function decides the channel, and the customer
  adopts the specific recipient and channel — never "DSA applies" from geography.
- **Skip the per-finding exception tick for excerpts/compilations.** "Duration
  never excludes a match" is a matching rule that must not become a takedown rule; an excerpt or
  compilation candidate needs the customer's explicit, separate consideration before it can be
  drafted, in addition to the batch-level exceptions confirmation.

## Open operator decisions

- Set `is_sandbox=True` on the operator's test organisation and choose the
  `NOTICE_SANDBOX_RECIPIENTS` test mailbox address(es).
- Production outbox wiring (customer-authenticated sending, or Marceline sending on the customer's
  behalf) is out of scope here and is being designed separately; this ADR's endpoints stay
  development-only until that design lands and a new ADR extends the public surface deliberately.
