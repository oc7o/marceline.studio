// @vitest-environment jsdom
/**
 * The invoices screen against a stubbed customer API: entries, states, both languages, and that
 * the PDF link stays on our own customer API.
 */
import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLanguageForTest } from "../i18n";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import { formatMoney } from "./invoices";
import { formatDay } from "./parts";
import { invoice, invoiceId, remoteLoaders, renderAt, serveCustomer } from "./testApi";

// Load-bearing, see customerApp.test.tsx: openapi-fetch captures fetch at import time, and Node's
// Request refuses the relative URLs the SPA uses on purpose.
const { fetchMock } = vi.hoisted(() => {
  const NativeRequest = globalThis.Request;
  class RelativeRequest extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  }
  globalThis.Request = RelativeRequest;
  const fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return { fetchMock };
});

beforeEach(async () => {
  fetchMock.mockReset();
  window.localStorage.clear();
  document.cookie = "csrftoken=token";
  await setLanguageForTest("en");
});

afterEach(() => {
  cleanup();
});

const INVOICES = [
  invoice({
    id: invoiceId(13),
    number: "2026-00003",
    kind: "cancellation",
    status: "issued",
    issued_on: "2026-09-03",
    due_on: null,
    gross_cents: -11900,
    net_cents: -10000,
    vat_cents: -1900,
    cancels_number: "2026-00001",
  }),
  invoice({ id: invoiceId(12), number: "2026-00002", status: "paid", paid_on: "2026-09-05" }),
  invoice({ id: invoiceId(11), status: "cancelled", cancelled_by_number: "2026-00003" }),
];

describe("the invoices screen", () => {
  it("lists every document with its standing, amount and PDF", async () => {
    const requested = serveCustomer(fetchMock, { "/api/v1/app/invoices": { status: 200, body: INVOICES } });
    const { container } = renderAt("/app/invoices");

    expect(await screen.findByRole("heading", { name: en.invoices.heading })).toBeTruthy();
    const entries = await screen.findAllByRole("article");
    expect(entries).toHaveLength(3);

    const storno = within(entries[0] as HTMLElement);
    expect(storno.getByText(en.invoices.cancellationTitle.replace("{{number}}", "2026-00003"))).toBeTruthy();
    expect(storno.getByText(en.invoices.status.issued)).toBeTruthy();
    expect(storno.getByText(formatMoney(-11900, "en"))).toBeTruthy();
    expect(storno.queryByText(`${en.invoices.dueOn}:`, { exact: false })).toBeNull();

    const paid = within(entries[1] as HTMLElement);
    expect(paid.getByText(en.invoices.status.paid)).toBeTruthy();
    expect(paid.getByText(formatDay("2026-09-05", "en"))).toBeTruthy();

    const cancelled = within(entries[2] as HTMLElement);
    expect(cancelled.getByText(en.invoices.status.cancelled)).toBeTruthy();
    expect(cancelled.getByText(en.invoices.cancelledBy.replace("{{number}}", "2026-00003"))).toBeTruthy();
    expect(cancelled.getByText(en.invoices.vat.domestic)).toBeTruthy();
    const download = cancelled.getByRole("link", { name: en.invoices.download });
    expect(download.getAttribute("href")).toBe(`/api/v1/app/invoices/${invoiceId(11)}/pdf`);
    expect(download.hasAttribute("download")).toBe(true);

    // Nothing is fetched but the customer API, and nothing on the page can load a resource.
    expect(requested.every((path) => path.startsWith("/api/v1/app/"))).toBe(true);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("says so when there are none, in German too", async () => {
    await setLanguageForTest("de");
    serveCustomer(fetchMock, { "/api/v1/app/invoices": { status: 200, body: [] } });
    renderAt("/app/invoices");

    expect(await screen.findByText(de.invoices.empty)).toBeTruthy();
    expect(screen.getByRole("link", { name: de.nav.invoices })).toBeTruthy();
  });

  it("says invoices are not shown yet where the server does not serve them (production)", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/invoices": { status: 404, body: {} } });
    renderAt("/app/invoices");

    expect(await screen.findByText(en.invoices.notOnline)).toBeTruthy();
    expect(screen.queryByRole("button", { name: en.common.retry })).toBeNull();
  });

  it("offers a retry when the list cannot be loaded", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/invoices": { status: 403, body: { detail: "boom" } } });
    renderAt("/app/invoices");

    expect(await screen.findByRole("button", { name: en.common.retry })).toBeTruthy();
    expect(screen.queryByText("boom")).toBeNull();
  });
});
