# 0032 — Billing: invoices and SEPA bank transfer, provider-agnostic

Status: accepted (2026-09-17). Issue #35.

Operator decision, 2026-09-17: **no card or payment processor to begin with.** Billing starts with
**invoices and SEPA bank transfer**, built so a card or direct-debit processor could be added later
as a thin adapter.

## Context

The MVP exit (roadmap M4) needs a customer to pay. B2B customers in Germany are used to paying
invoices by bank transfer, which needs no paid invoicing service and no processor fees. German law
still applies to every invoice: § 14 UStG (mandatory content), § 14a (reverse charge), § 19 (small
businesses) and the GoBD (numbers sequential and never reused, issued documents unchanged, retained
ten years).

## Decision

A new Django app **`billing`**, depending on `accounts` (the organisation it bills) and `core`
only. Every other app is forbidden to import it (import-linter). The customer endpoints live in the
project package (`marceline/app/invoices.py`) like every other customer view.

### Model
- `Plan` (code, name printed on the invoice, `month`/`year`, net price in cents), `BillingProfile`
  (one per organisation: legal name, address, country, business or consumer, VAT ID),
  `Subscription` (organisation × plan, `starts_on`, optional `ends_on`; one open subscription per
  organisation), `Invoice` + `InvoiceLine`, `InvoiceSequence`.
- Money is **integer euro cents**, VAT rates **basis points**, currency always EUR.
- Periods are anchored at `starts_on` and billed **in advance**; no proration.
- Status is **derived**, never stored: `draft`, `open`, `overdue` (past `due_on`, unpaid),
  `paid` (`paid_on` set by hand), `cancelled` (a cancellation document exists), and `issued` for a
  cancellation document, which asks for no payment.

### GoBD in the database
- An invoice gets its **number when it is issued**: `<prefix><year>-<00001>`, one series per
  calendar year shared by invoices and cancellations (`BILLING_INVOICE_NUMBER_PREFIX`, default
  empty). The year's `InvoiceSequence` row is locked inside the issuing transaction, so a rollback
  gives the number back (no gaps) and concurrent issues wait (no duplicates).
- **Issued invoices are immutable**, enforced by triggers (migration `billing/0002`): an issued
  row refuses every UPDATE except `paid_on`/`payment_note` and every DELETE; lines of an issued
  invoice cannot be inserted, changed or deleted; a sequence never goes down and is never deleted.
  Errors carry SQLSTATE 23000 (`IntegrityError`). Check constraints make a half-issued row
  impossible. This is the one place the project uses triggers on purpose: the rule must hold for
  the admin, a shell and raw SQL alike.
- A wrong invoice is **cancelled by a Storno** (`kind=cancellation`, `cancels` → the invoice,
  negated lines and totals, same buyer and VAT treatment as the original). The original row is
  never touched. A corrected invoice is then drafted by hand; `issue_invoices` never re-bills a
  period that was invoiced once, cancelled or not.
- **Seller and buyer are snapshots** on the invoice (`seller`, `buyer` JSON), and the **PDF is
  rendered once at issue time and stored** with its sha256. A later address, bank or code change
  never alters a document that was sent.

### VAT (`billing/vat.py`, pure)
| Seller | Buyer | Treatment |
|---|---|---|
| regular | in Germany, business or consumer | `domestic`, 19 % |
| regular | EU business outside Germany with a VAT ID of its country | `reverse_charge`, 0 %, both VAT IDs and the reverse-charge note (§ 14a (5) UStG) on the invoice |
| small business (`BILLING_SMALL_BUSINESS=true`) | in Germany | `small_business`, 0 %, § 19 note |

Refused with a code: consumers outside Germany (**OSS is out of scope**), EU businesses without or
with a mismatching VAT ID, buyers outside the EU, reverse charge without the seller's own VAT ID,
and a small business selling abroad. A VAT ID is checked for shape and country
prefix only, not against VIES. VAT is computed on the invoice's net total, rounded half up.

### Seller identity from the environment
`BILLING_SELLER_NAME`, `_ADDRESS` (lines separated by `|`), `_TAX_NUMBER`, `_VAT_ID`, `_EMAIL`,
`_IBAN` (mod-97 checked), `_BIC`, `_BANK`, `BILLING_SMALL_BUSINESS`, `BILLING_PAYMENT_TERM_DAYS`
(14). **No value is in the repository.** Issuing refuses while name, address, tax number or VAT ID,
a valid IBAN, or the small-business decision is missing — `BILLING_SMALL_BUSINESS` deliberately
has no default. Tests pin all of them empty (`OPERATIONAL_DEFAULTS`) and use synthetic values.

### The PDF without a PDF library
`billing/pdf.py` writes a single-page A4 PDF by hand (~150 lines): the standard Helvetica fonts
every reader ships, WinAnsiEncoding (German umlauts, ß, €, §; anything else prints as `?`), a
compressed content stream, deterministic bytes. `billing/document.py` lays out a German invoice
with English subtitles. The **EPC QR code** (GiroCode, EPC069-12 v002: BIC, name, IBAN, amount,
invoice number as remittance) is drawn from a matrix computed by **`segno`**, the only new
dependency: pure Python, no dependencies of its own, typed, widely used. Verified once by rendering
the PDF with PyMuPDF and decoding the QR code with OpenCV (not a project dependency).

### Workflow and surfaces
- **Admin** (staff, development VPS only): draft → *Issue* → send the PDF → *Mark paid today*
  when the transfer is on the bank statement → or *Cancel* (issues the Storno). Issued invoices are
  read-only except the payment fields and cannot be deleted; PDF download per invoice.
- **`manage.py issue_invoices [--dry-run] [--date]`** issues one invoice per started, not yet
  invoiced period of every subscription; idempotent (also under concurrency: a partial unique
  constraint on subscription × period plus a row lock). A subscription that cannot be invoiced is
  reported and the others continue. **Not scheduled.**
- **Customer API**: `GET /api/v1/app/invoices` (issued documents of the caller's organisation,
  codes and cents only) and `GET /api/v1/app/invoices/{id}/pdf` (the stored PDF, attachment,
  `no-store`). Drafts, another organisation's invoices and unknown ids share a byte-identical 404.
  **Amended 2026-09-17 (ADR 0033):** `{id}` and the payload's `id` are the document's public id
  (`Invoice.public_id`, `inv_` + 32 hex), never its primary key; malformed and integer ids get the
  same 404. The legal `number` stays sequential (§ 14 UStG) and is shown, but never addresses a
  document. `billing/0003` adds the column through its database default — a table rewrite, which
  fires no row trigger — so issued invoices get their ids without an `UPDATE` the GoBD trigger
  would refuse, and from then on the trigger freezes the id with the rest of the row.
- **Customer UI**: `/app/invoices`, German and English, one small screen, no new frontend
  dependency.

### A payment processor later
A processor, if one is ever added, is an adapter that **records a payment against an issued
`Invoice`** (today: `services.mark_paid`), and at most creates drafts through `billing.services`.
It never owns plans, subscriptions or numbering, and never issues documents itself.

## Consequences
- Bank reconciliation is manual. Reminders (dunning) are manual. Invoices are sent by hand.
- Invoices live in the development database next to everything else.

### Production: invoices are development-only for now
ADR 0028's production host answers customer data only from signed per-organisation bundles, and
production's own database holds no invoice data. The bundle (`export.document_keys`,
`store.DOCUMENT_KEY`, `customer_cache.CACHED_ROUTES`) carries JSON documents and no invoices or
PDFs, so a production request for `/api/v1/app/invoices` would reach the view and read empty local
tables — an empty list that reads as "you have no invoices".

Decision (2026-09-17): **`/api/v1/app/invoices` and `/api/v1/app/invoices/{id}/pdf` are served on
development only.** `marceline/public_surface.ROLE_ONLY_PREFIXES["dev"]` names the prefix, so with
`MARCELINE_ROLE=prod` both paths get the same bare 404 as an unknown path, whatever the surface.
Caddy's `/api/v1/app/*` allowlist is unchanged (the Django role check is the refusal). The customer
screen treats that 404 as "invoices are not shown here yet; we send each invoice as a PDF by email"
rather than as an error. Invoices are sent by hand meanwhile.

How production gets invoices is **still an open operator decision**: add the list document and the
PDFs to the signed bundle (PDFs are a few KB each, but binary in a JSON envelope), keep a separate
signed invoice document per organisation, or let production hold its own copy. When it is decided,
the endpoints join `CACHED_ROUTES` and the export together and leave `ROLE_ONLY_PREFIXES`.
- Retention: issued invoices are kept for their statutory bookkeeping period — see the
  2026-09-17 amendment below, which corrects the figure this section originally gave — and
  organisations with invoices cannot be deleted (`PROTECT`). The nightly backup covers them.

## Follow-ups (not built)
- **E-invoicing (ZUGFeRD / XRechnung)**: issuing B2B e-invoices is optional for us until the end of
  2027 and mandatory from 2028. The stored snapshot has every field a ZUGFeRD XML needs; add it
  then (a PDF/A-3 with embedded XML, or XRechnung alone).
- VIES / BZSt confirmation of customer VAT IDs before a reverse-charge invoice.
- OSS for EU consumers, if consumers outside Germany are ever sold to.
- Scheduling `issue_invoices`, mailing invoices and reminders, a SEPA direct-debit or card adapter.
- A "Verfahrensdokumentation" (GoBD process description).

## Open operator decisions
- `BILLING_SMALL_BUSINESS` true or false.
- Invoice number prefix (e.g. `RE-`), fixed before the first invoice.
- Plans (none are seeded), payment term (14 days default).
- How invoices reach production (see "Production: invoices are development-only for now").

## Amendment 2026-09-17: retention is eight years, and it is now enforced, not just stated

A review of the data inventory found two things wrong with the Decision and Consequences
sections above:

- **The figure was already superseded.** "Ten-year records (§ 147 AO)" was correct once, but
  since **1 January 2025** the Viertes Bürokratieentlastungsgesetz cut the retention period for
  *Buchungsbelege* — an issued outgoing invoice, or its cancellation, is one — to **eight years**
  in § 147 Abs. 3 AO, § 257 Abs. 4 HGB and § 14b Abs. 1 UStG. Ten years remains only for
  *Handelsbücher*, *Inventare*, *Bilanzen* and *Jahresabschlüsse* — none of which this app stores.
  The period is counted from the **end of the calendar year of issue**, not the issue date: an
  invoice issued on any day of year Y is kept through 31 December of year Y + 8 and becomes
  deletable on 1 January of year Y + 9.
- **"Nothing deletes them" is not a retention policy.** A statutory upper bound on how long
  something *may* be kept is not satisfied by keeping it forever; Art. 5(1)(e) GDPR (storage
  limitation) does not accept "forever" as an answer either. A deletion job is required, not
  optional.

### What changed
- `billing/retention.py`: `RETENTION_YEARS = 8`, `deletable_from(issued_on)` = 1 January of
  `issued_on.year + 9`; `apply_retention(today, dry_run=)` deletes every issued invoice or
  cancellation past that date. A cancellation is issued on or after the invoice it protects, so
  its own deadline is never earlier; the original is only deleted once its cancellation is too
  (`Invoice.cancels`, `on_delete=PROTECT`, unchanged).
- **The DELETE refusal in `billing/migrations/0002` was unconditional** ("issued invoice cannot
  be deleted; issue a cancellation") — correct while nothing needed to delete one, wrong once
  retention became a real policy. `billing/migrations/0004_invoice_retention_deletion.py` adds
  `billing_invoice_deletable_from(issued_on)` (the same rule in SQL) and amends the DELETE branch
  of `billing_invoice_immutable` to allow it past that date, plus the matching allowance in
  `billing_invoiceline_immutable`'s DELETE branch so a parent's own deletion can take its lines
  with it. **The UPDATE branch — an issued invoice is immutable — is untouched**: a row stays
  frozen until the day it is deleted, never edited around the edges first. `InvoiceSequence`
  keeps every number forever, deleted invoices included — the sequence guarantees numbering
  integrity, not that every numbered document survives.
- CLI `invoice_retention --dry-run --date`, same shape as `rights_housekeeping`; scheduled daily
  (`SCHEDULER_INVOICE_RETENTION_HOURS`), after `rights_housekeeping`.
- Billing has no legal-hold concept, unlike `marceline.rights` (ADR 0034); nothing here checks
  one. If invoices ever need one, it has to be added to both the Python check and the database
  trigger, the way retention itself was.

No change to numbering, immutability while an invoice is retained, VAT, the PDF renderer or the
customer API.
