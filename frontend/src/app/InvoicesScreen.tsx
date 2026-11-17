/**
 * The invoices (`/app/invoices`, ADR 0032): every issued invoice and cancellation, newest first,
 * each with its standing and the stored PDF.
 *
 * Read-only. Payment is by bank transfer with the invoice number as reference; the bank details
 * and a GiroCode are on the PDF itself, so this screen never repeats them.
 */
import { useTranslation } from "react-i18next";

import { useLanguage } from "../i18n";
import { type CustomerInvoice, INVOICE_STATUS_TONES, formatMoney, invoicePdfPath } from "./invoices";
import { BADGE, LoadError, Loading, TitleRow, isNotFound, useFormatDay } from "./parts";
import { useCustomerInvoices } from "./queries";

export function InvoicesScreen() {
  const { t } = useTranslation();
  const invoices = useCustomerInvoices();

  return (
    <section aria-labelledby="invoices-heading" className="flex flex-col gap-3">
      <div>
        <h1 id="invoices-heading" className="text-xl font-bold">
          {t("invoices.heading")}
        </h1>
        <p className="mt-1 text-sm text-slate-600">{t("invoices.lead")}</p>
      </div>
      {invoices.isPending ? (
        <Loading />
      ) : invoices.isError && isNotFound(invoices.error) ? (
        // Production does not serve invoices yet (ADR 0032): they are sent by email meanwhile.
        <p role="note" className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {t("invoices.notOnline")}
        </p>
      ) : invoices.isError ? (
        <LoadError onRetry={() => void invoices.refetch()} />
      ) : invoices.data.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {t("invoices.empty")}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {invoices.data.map((invoice) => (
            <li key={invoice.id}>
              <InvoiceEntry invoice={invoice} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function InvoiceEntry({ invoice }: { invoice: CustomerInvoice }) {
  const { t } = useTranslation();
  const [language] = useLanguage();
  const formatDay = useFormatDay();
  const title =
    invoice.kind === "cancellation"
      ? t("invoices.cancellationTitle", { number: invoice.number })
      : t("invoices.invoiceTitle", { number: invoice.number });

  return (
    <article aria-label={title} className="rounded-lg border border-slate-200 bg-white p-3">
      <TitleRow
        title={<div className="font-medium">{title}</div>}
        badge={
          <span data-status={invoice.status} className={`${BADGE} ${INVOICE_STATUS_TONES[invoice.status]}`}>
            {t(`invoices.status.${invoice.status}`)}
          </span>
        }
      />
      <div className="mt-1 text-lg font-semibold tabular-nums">{formatMoney(invoice.gross_cents, language)}</div>
      <dl className="mt-1 grid grid-cols-1 gap-x-4 gap-y-1 text-xs text-slate-600 sm:grid-cols-2">
        <div>
          <dt className="inline">{t("invoices.issuedOn")}: </dt>
          <dd className="inline">{formatDay(invoice.issued_on)}</dd>
        </div>
        {invoice.due_on ? (
          <div>
            <dt className="inline">{t("invoices.dueOn")}: </dt>
            <dd className="inline">{formatDay(invoice.due_on)}</dd>
          </div>
        ) : null}
        {invoice.period_start && invoice.period_end ? (
          <div>
            <dt className="inline">{t("invoices.period")}: </dt>
            <dd className="inline">
              {formatDay(invoice.period_start)} – {formatDay(invoice.period_end)}
            </dd>
          </div>
        ) : null}
        {invoice.paid_on ? (
          <div>
            <dt className="inline">{t("invoices.paidOn")}: </dt>
            <dd className="inline">{formatDay(invoice.paid_on)}</dd>
          </div>
        ) : null}
      </dl>
      <p className="mt-1 text-xs text-slate-600">{t(`invoices.vat.${invoice.vat_treatment}`)}</p>
      {invoice.cancelled_by_number ? (
        <p className="mt-1 text-xs text-slate-600">
          {t("invoices.cancelledBy", { number: invoice.cancelled_by_number })}
        </p>
      ) : null}
      <a
        href={invoicePdfPath(invoice.id)}
        download
        className="mt-2 inline-block text-sm font-medium text-sky-800 underline"
      >
        {t("invoices.download")}
      </a>
    </article>
  );
}
