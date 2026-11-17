# Billing

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Billing (ADR 0032, issue #35)
- **Invoices and SEPA bank transfer, no payment processor.** App
  `billing` → `accounts`, `core` only. A processor would be an adapter that records payments against
  issued invoices through `billing.services`; it never numbers or issues documents.
- Money in **integer cents**, VAT in **basis points**, EUR only. `billing/vat.py`, `periods.py`,
  `epc.py`, `pdf.py`, `document.py` are pure; `services.py` is the only writer of invoices
  (`issue`, `cancel`, `mark_paid`/`mark_unpaid`, `issue_due_invoices`, `status_of`).
- **GoBD is enforced by triggers** (`billing/migrations/0002`): an issued invoice (number set)
  refuses every UPDATE except `paid_on`/`payment_note`, its lines are frozen, and
  `InvoiceSequence` never decreases. Never work around them — a wrong invoice gets a Storno
  (`services.cancel`) and a new draft. Numbers (`<prefix><year>-<00001>`) come only from
  `numbering.next_number`, inside the issuing transaction.
- **Retention (ADR 0032 amendment 2026-09-17): eight years, not the ten the ADR used to say.**
  An issued invoice or cancellation is a *Buchungsbeleg*; since 1 January 2025
  (Viertes Bürokratieentlastungsgesetz) that is eight years under § 147 Abs. 3 AO, § 257 Abs. 4
  HGB and § 14b Abs. 1 UStG, counted from the **end of the calendar year of issue**, not the issue
  date — `billing/retention.py` `deletable_from(issued_on)`. `billing/migrations/0004` amended
  the DELETE branch of the same trigger to allow it once that date has passed (the UPDATE branch
  — immutability — is untouched: an issued invoice stays immutable until it is deleted), and gave
  `billing_invoiceline` the same allowance so a parent's own deletion can take its lines with it.
  A cancellation is issued on or after its invoice, so `Invoice.cancels`
  (`on_delete=PROTECT`) keeps the original from being deleted while a still-retained cancellation
  points at it; `apply_retention` deletes the cancellation first once it, too, is past retention.
  Deleting the row takes the stored PDF with it — there is nowhere else it lives. The trigger
  checks a real calendar date, not whatever `today` a caller used, so `apply_retention` sets it
  transaction-locally (`set_config('marceline.retention_today', …, true)`) right before each
  delete — the plumbing that keeps an explicit `--date` consistent with what actually gets
  deleted; a raw SQL DELETE or the admin never sets it, so the trigger falls back to
  `CURRENT_DATE`. CLI `invoice_retention --dry-run --date`; idempotent; scheduled daily
  (`SCHEDULER_INVOICE_RETENTION_HOURS`). Billing has no legal-hold concept today, unlike
  `marceline.rights` (ADR 0034); nothing here checks one.
- **Customer data deletion never touches billing** (ADR 0045, issue #85). `Invoice`, `Subscription`
  and `BillingProfile` all `PROTECT` the organisation, so an ended pilot's organisation row simply
  survives its own deletion pass while any of them stands, and the pass reports it
  (`billing_records_retained`) instead of working around it. `invoice_retention` stays the only
  thing that ever deletes an invoice.
- Seller and buyer are **snapshots** on the invoice and the PDF is **stored at issue time**; never
  re-render or re-read settings for an issued document. Status (`open`/`overdue`/`paid`/
  `cancelled`/`issued`) is derived, never stored.
- VAT: 19 % domestic, reverse charge for EU businesses with a matching VAT ID, § 19 small business
  (`BILLING_SMALL_BUSINESS`); everything else raises `VatNotSupported` with a code (OSS consumers,
  non-EU, missing VAT IDs). Add a case only with a confirmed tax treatment and a test.
- Seller identity only from `BILLING_*` settings (environment), never in code, fixtures or tests:
  tests use `billing/tests/helpers.SELLER` (synthetic, documentation IBAN), and `conftest.py` pins
  every `BILLING_*` value empty. The PDF writer uses standard Helvetica/WinAnsi: no font files, no
  PDF library; `segno` only computes the QR matrix.
- Customer API `marceline/app/invoices.py`: `GET /api/v1/app/invoices` and `/{invoice_id}/pdf`,
  scoped by `organisation_of`; the id is `Invoice.public_id` (`inv_…`, ADR 0033), never the primary
  key or the number; drafts, foreign, unknown, malformed and integer ids share one byte-identical
  404. The public id is frozen by the GoBD trigger like every other column of an issued invoice;
  `billing/0003` filled existing rows through the column's database default (a table rewrite fires
  no row trigger) — never backfill an invoice column with an `UPDATE`. **Development
  only for now:** `public_surface.ROLE_ONLY_PREFIXES["dev"]` makes both 404 on `MARCELINE_ROLE=prod`,
  because the signed cache carries no invoices (ADR 0032). Serving them on production means adding
  them to the export, `store.DOCUMENT_KEY` and `CACHED_ROUTES` together and removing that prefix.
