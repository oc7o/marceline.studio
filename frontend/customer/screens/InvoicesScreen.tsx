/**
 * The invoices (`/app/invoices`, ADR 0032): every issued invoice and cancellation, newest first,
 * each with its standing and the stored PDF.
 *
 * Read-only. Payment is by bank transfer with the invoice number as reference; the bank details
 * and a GiroCode are on the PDF itself, so this screen never repeats them.
 *
 * The PDF link goes to this server's own route handler, not to the Django path — the browser needs
 * no address on the private side, the same rule the writes follow.
 */
import { type CustomerInvoice, formatMoney } from "@shared/app/invoices";

import { BADGE, LoadError, TitleRow, formatDay } from "../components/parts";
import { Banner, EmptyState } from "../components/ui";
import { INVOICE_TONES } from "../lib/tones";
import type { Words } from "../lib/words";

/** The stored PDF through the BFF. `id` is the document's public id (`inv_…`, ADR 0033). */
export function invoicePdfPath(id: string): string {
  return `/app/api/invoices/${encodeURIComponent(id)}/pdf`;
}

export function InvoicesScreen({
  i18n,
  invoices,
  /** true when the API answered 404: production does not serve invoices yet (ADR 0032). */
  notOnline,
}: {
  i18n: Words;
  invoices: CustomerInvoice[] | null;
  notOnline: boolean;
}) {
  const { t } = i18n;
  return (
    <section aria-labelledby="invoices-heading" className="flex flex-col gap-3">
      <div>
        <h1 id="invoices-heading" className="font-display text-display-m text-ink">
          {t("invoices.heading")}
        </h1>
        <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("invoices.lead")}</p>
      </div>
      {notOnline ? (
        // Production does not serve invoices yet (ADR 0032): they are sent by email meanwhile.
        <Banner role="note">{t("invoices.notOnline")}</Banner>
      ) : invoices === null ? (
        <LoadError i18n={i18n} href="/app/invoices" />
      ) : invoices.length === 0 ? (
        <EmptyState title={t("invoices.empty")} next={t("invoices.emptyNext")} veil />
      ) : (
        <ul className="flex flex-col gap-2">
          {invoices.map((invoice) => (
            <li key={invoice.id}>
              <InvoiceEntry i18n={i18n} invoice={invoice} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function InvoiceEntry({ i18n, invoice }: { i18n: Words; invoice: CustomerInvoice }) {
  const { t, language } = i18n;
  const day = (value: string) => formatDay(value, language);
  const title =
    invoice.kind === "cancellation"
      ? t("invoices.cancellationTitle", { number: invoice.number })
      : t("invoices.invoiceTitle", { number: invoice.number });

  return (
    <article
      aria-label={title}
      className="rounded-md border border-hairline bg-surface-100 p-4 shadow-sm"
    >
      <TitleRow
        title={<div className="text-body-l font-medium text-ink">{title}</div>}
        badge={
          <span
            data-status={invoice.status}
            className={`${BADGE} ${INVOICE_TONES[invoice.status]}`}
          >
            {t(`invoices.status.${invoice.status}`)}
          </span>
        }
      />
      {/* The amount is a metric: mono, so the digits line up between one invoice and the next. */}
      <div className="mt-2 font-mono text-metric tabular-nums text-ink">
        {formatMoney(invoice.gross_cents, language)}
      </div>
      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-caption text-ink-muted sm:grid-cols-2">
        <div>
          <dt className="inline">{t("invoices.issuedOn")}: </dt>
          <dd className="inline">{day(invoice.issued_on)}</dd>
        </div>
        {invoice.due_on ? (
          <div>
            <dt className="inline">{t("invoices.dueOn")}: </dt>
            <dd className="inline">{day(invoice.due_on)}</dd>
          </div>
        ) : null}
        {invoice.period_start && invoice.period_end ? (
          <div>
            <dt className="inline">{t("invoices.period")}: </dt>
            <dd className="inline">
              {day(invoice.period_start)} – {day(invoice.period_end)}
            </dd>
          </div>
        ) : null}
        {invoice.paid_on ? (
          <div>
            <dt className="inline">{t("invoices.paidOn")}: </dt>
            <dd className="inline">{day(invoice.paid_on)}</dd>
          </div>
        ) : null}
      </dl>
      <p className="mt-2 text-caption text-ink-muted">{t(`invoices.vat.${invoice.vat_treatment}`)}</p>
      {invoice.cancelled_by_number ? (
        <p className="mt-1 text-caption text-ink-muted">
          {t("invoices.cancelledBy", { number: invoice.cancelled_by_number })}
        </p>
      ) : null}
      <a
        href={invoicePdfPath(invoice.id)}
        download
        className="tap mt-3 rounded-sm text-button text-brand-ink underline"
      >
        {t("invoices.download")}
      </a>
    </article>
  );
}
