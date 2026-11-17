/**
 * Pure helpers for the invoices screen (`/app/invoices`, ADR 0032): money, the PDF address and
 * the status badge tone. The API sends codes and cents; the words and the formatting live here.
 */
import type { Schemas } from "./apiTypes";

export type CustomerInvoice = Schemas["CustomerInvoiceOut"];
export type InvoiceStatus = CustomerInvoice["status"];

/** An amount in cents as the customer's language writes euros: "119,00 €" / "€119.00". */
export function formatMoney(cents: number, language: string): string {
  return new Intl.NumberFormat(language, { style: "currency", currency: "EUR" }).format(cents / 100);
}

/**
 * The stored PDF, same origin, through the customer API only. `id` is the document's public id
 * (`inv_…`, ADR 0033) as the API sent it — never the invoice number, never a database key. It is
 * URL-encoded anyway, so nothing the API sends can leave the invoices path.
 */
export function invoicePdfPath(id: string): string {
  return `/api/v1/app/invoices/${encodeURIComponent(id)}/pdf`;
}

export const INVOICE_STATUS_TONES: Record<InvoiceStatus, string> = {
  open: "bg-sky-100 text-sky-900",
  overdue: "bg-rose-100 text-rose-900",
  paid: "bg-emerald-100 text-emerald-900",
  cancelled: "bg-slate-100 text-slate-700",
  issued: "bg-slate-100 text-slate-700",
};

export const INVOICE_STATUSES = Object.keys(INVOICE_STATUS_TONES) as InvoiceStatus[];
