import { describe, expect, it } from "vitest";

import { de } from "../i18n/de";
import { en } from "../i18n/en";
import { INVOICE_STATUSES, formatMoney, invoicePdfPath } from "./invoices";

describe("invoice helpers", () => {
  it("formats cents as euros in the customer's language", () => {
    // Intl may use a narrow no-break space before the sign; compare without it.
    const plain = (text: string) => text.replace(/\s/g, " ");
    expect(plain(formatMoney(11900, "de"))).toBe("119,00 €");
    expect(formatMoney(11900, "en")).toBe("€119.00");
    expect(plain(formatMoney(-11900, "de"))).toBe("-119,00 €");
  });

  it("builds a same-origin customer API path for the PDF", () => {
    const id = "inv_" + "4".repeat(32);
    expect(invoicePdfPath(id)).toBe(`/api/v1/app/invoices/${id}/pdf`);
    expect(invoicePdfPath("../../review")).toBe("/api/v1/app/invoices/..%2F..%2Freview/pdf");
  });

  // Template-literal keys are invisible to translationKeys.test.ts, so every code is checked here.
  it("has words for every status and VAT treatment in both languages", () => {
    for (const resources of [en, de]) {
      for (const status of INVOICE_STATUSES) {
        expect(resources.invoices.status[status]).toBeTruthy();
      }
      for (const treatment of ["domestic", "reverse_charge", "small_business"] as const) {
        expect(resources.invoices.vat[treatment]).toBeTruthy();
      }
    }
  });
});
