// @vitest-environment jsdom
/**
 * The rights screen against a stubbed customer API: statuses, blockers, the verbatim declaration,
 * what a self-declaration sends, the production 404 and that nothing can load a remote resource.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLanguageForTest } from "../i18n";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import type { RightsOverview } from "./rights";
import { remoteLoaders, renderAt, serveCustomer } from "./testApi";

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

const OVERVIEW: RightsOverview = {
  claimant: {
    claimant_type: null,
    identity_status: "unverified",
    identity_method: null,
    account_control_token: null,
  },
  work_declaration: { version: "work-2026-09-17", en: "I confirm the English text.", de: "Ich bestätige den deutschen Text." },
  partner_declaration: { version: "partners-none-2026-09-17", en: "No partners, in English.", de: "Keine Partner, auf Deutsch." },
  withdrawal_declaration: { version: "withdraw-2026-09-17", en: "I withdraw it.", de: "Ich ziehe sie zurück." },
  authority_declaration: {
    version: "authority-2026-09-18",
    en: "Our documents give us this authority.",
    de: "Unsere Unterlagen geben uns diese Befugnis.",
  },
  authority_withdrawal_declaration: {
    version: "authority-withdraw-2026-09-18",
    en: "I take that back.",
    de: "Ich nehme das zurück.",
  },
  partners: [],
  partners_none_affirmed_at: null,
  works: [
    {
      original_id: "orig_7",
      title: "Beach day",
      rights_status: "unverified",
      capacity: null,
      underlying_owner_name: null,
      asserted_right: null,
      asserted_right_detail: "",
      declared_at: null,
      declaration_version: null,
      authority_declared_at: null,
      authority_declaration_version: null,
      notice_blockers: ["claimant_type_missing", "rights_record_missing"],
    },
    {
      original_id: "orig_8",
      title: "Studio scene",
      rights_status: "disputed",
      capacity: "owner",
      underlying_owner_name: null,
      asserted_right: "film_producer_right",
      asserted_right_detail: "",
      declared_at: "2026-09-10T10:00:00Z",
      declaration_version: "work-2026-09-17",
      authority_declared_at: null,
      authority_declaration_version: null,
      notice_blockers: ["rights_disputed", "competing_claim"],
    },
    {
      // Everything staff can do is done; only the customer's own statement is missing.
      original_id: "orig_9",
      title: "Hotel room",
      rights_status: "documents_complete",
      capacity: "authorised_agent",
      underlying_owner_name: "Rights Holder GmbH",
      asserted_right: "copyright",
      asserted_right_detail: "",
      declared_at: "2026-09-11T10:00:00Z",
      declaration_version: "work-2026-09-17",
      authority_declared_at: null,
      authority_declaration_version: null,
      notice_blockers: ["authority_not_declared"],
    },
  ],
  proofs: [],
  proof_upload: { available: true, max_bytes: 10485760, content_types: ["application/pdf", "image/png", "image/jpeg"] },
};

describe("the rights screen", () => {
  it("shows each work's status and what is still missing, and loads nothing remote", async () => {
    const requested = serveCustomer(fetchMock, { "/api/v1/app/rights": { status: 200, body: OVERVIEW } });
    const { container } = renderAt("/app/rights");

    expect(await screen.findByRole("heading", { name: en.rights.heading })).toBeTruthy();
    const beach = within(await screen.findByRole("article", { name: "Beach day" }));
    expect(beach.getByText(en.rights.works.status.unverified)).toBeTruthy();
    expect(beach.getByText(en.rights.works.blockers.claimant_type_missing)).toBeTruthy();
    const studio = within(screen.getByRole("article", { name: "Studio scene" }));
    expect(studio.getByText(en.rights.works.status.disputed)).toBeTruthy();
    expect(studio.getByText(en.rights.works.blockers.competing_claim)).toBeTruthy();
    expect(studio.queryByRole("button", { name: en.rights.works.redeclare })).toBeNull();
    expect(screen.getByText(en.rights.partners.empty)).toBeTruthy();

    expect(requested.every((path) => path.startsWith("/api/v1/app/"))).toBe(true);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("sends a self-declaration with the text version and language shown", async () => {
    await setLanguageForTest("de");
    serveCustomer(fetchMock, { "/api/v1/app/rights": { status: 200, body: OVERVIEW } });
    renderAt("/app/rights");

    const beach = within(await screen.findByRole("article", { name: "Beach day" }));
    fireEvent.click(beach.getByRole("button", { name: de.rights.works.declare }));
    expect(beach.getByText("Ich bestätige den deutschen Text.")).toBeTruthy();
    const submit = beach.getByRole("button", { name: de.rights.works.submit });
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    const declared: RightsOverview = {
      ...OVERVIEW,
      works: OVERVIEW.works.map((work) =>
        work.original_id === "orig_7" ? { ...work, rights_status: "self_declared", capacity: "owner", asserted_right: "copyright" } : work,
      ),
    };
    let sent: unknown = null;
    const base = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      if (request.method === "POST" && request.url.endsWith("/api/v1/app/rights/works/orig_7/declaration")) {
        sent = await request.json();
        expect(request.headers.get("X-CSRFToken")).toBe("token");
        return new Response(JSON.stringify(declared), { status: 200, headers: { "content-type": "application/json" } });
      }
      return base?.(input, init) as Promise<Response>;
    });

    fireEvent.click(beach.getByRole("checkbox"));
    fireEvent.click(submit);

    await waitFor(() => {
      expect(sent).toEqual({
        capacity: "owner",
        asserted_right: "copyright",
        underlying_owner_name: "",
        asserted_right_detail: "",
        declaration_version: "work-2026-09-17",
        language: "de",
        confirmed: true,
      });
    });
    expect(await beach.findByText(de.rights.works.status.self_declared)).toBeTruthy();
  });

  it("withdraws a self-declaration with a reason, and only while it is self-declared", async () => {
    const selfDeclared: RightsOverview = {
      ...OVERVIEW,
      works: OVERVIEW.works.map((work) =>
        work.original_id === "orig_7" ? { ...work, rights_status: "self_declared", capacity: "owner", asserted_right: "copyright" } : work,
      ),
    };
    serveCustomer(fetchMock, { "/api/v1/app/rights": { status: 200, body: selfDeclared } });
    renderAt("/app/rights");

    const beach = within(await screen.findByRole("article", { name: "Beach day" }));
    const studio = within(screen.getByRole("article", { name: "Studio scene" }));
    expect(studio.queryByRole("button", { name: en.rights.works.withdraw })).toBeNull();
    fireEvent.click(beach.getByRole("button", { name: en.rights.works.withdraw }));
    expect(beach.getByText("I withdraw it.")).toBeTruthy();

    let sent: unknown = null;
    const base = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      if (request.method === "POST" && request.url.endsWith("/api/v1/app/rights/works/orig_7/withdrawal")) {
        sent = await request.json();
        return new Response(JSON.stringify(OVERVIEW), { status: 200, headers: { "content-type": "application/json" } });
      }
      return base?.(input, init) as Promise<Response>;
    });

    fireEvent.change(beach.getByLabelText(en.rights.works.withdrawReason), { target: { value: "declared_in_error" } });
    fireEvent.click(beach.getByRole("checkbox"));
    fireEvent.click(beach.getByRole("button", { name: en.rights.works.withdrawSubmit }));

    await waitFor(() => {
      expect(sent).toEqual({
        reason: "declared_in_error",
        note: "",
        declaration_version: "withdraw-2026-09-17",
        language: "en",
        confirmed: true,
      });
    });
    expect(await beach.findByText(en.rights.works.status.unverified)).toBeTruthy();
  });

  it("declares authority over the documents, and only once they are complete", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/rights": { status: 200, body: OVERVIEW } });
    renderAt("/app/rights");

    // Nothing to declare while our staff are still working on the documents.
    const beach = within(await screen.findByRole("article", { name: "Beach day" }));
    expect(beach.queryByRole("button", { name: en.rights.works.authority })).toBeNull();

    const hotel = within(await screen.findByRole("article", { name: "Hotel room" }));
    expect(hotel.getByText(en.rights.works.status.documents_complete)).toBeTruthy();
    expect(hotel.getByText(en.rights.works.blockers.authority_not_declared)).toBeTruthy();
    fireEvent.click(hotel.getByRole("button", { name: en.rights.works.authority }));
    // The wording stored is the wording shown: served by the API, rendered verbatim.
    expect(hotel.getByText("Our documents give us this authority.")).toBeTruthy();
    const submit = hotel.getByRole("button", { name: en.rights.works.authoritySubmit });
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    const declared: RightsOverview = {
      ...OVERVIEW,
      works: OVERVIEW.works.map((work) =>
        work.original_id === "orig_9"
          ? {
              ...work,
              rights_status: "authority_declared",
              authority_declared_at: "2026-09-18T09:00:00Z",
              authority_declaration_version: "authority-2026-09-18",
              notice_blockers: [],
            }
          : work,
      ),
    };
    let sent: unknown = null;
    const base = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      if (request.method === "POST" && request.url.endsWith("/api/v1/app/rights/works/orig_9/authority")) {
        sent = await request.json();
        expect(request.headers.get("X-CSRFToken")).toBe("token");
        return new Response(JSON.stringify(declared), { status: 200, headers: { "content-type": "application/json" } });
      }
      return base?.(input, init) as Promise<Response>;
    });

    fireEvent.click(hotel.getByRole("checkbox"));
    fireEvent.click(submit);

    await waitFor(() => {
      expect(sent).toEqual({ declaration_version: "authority-2026-09-18", language: "en", confirmed: true });
    });
    expect(await hotel.findByText(en.rights.works.status.authority_declared)).toBeTruthy();
    expect(hotel.getByRole("button", { name: en.rights.works.authorityWithdraw })).toBeTruthy();
    expect(hotel.queryByRole("button", { name: en.rights.works.authority })).toBeNull();
  });

  it("shows a refusal in words, never the code", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/rights": { status: 200, body: OVERVIEW } });
    renderAt("/app/rights");
    const form = within(await screen.findByRole("form", { name: en.rights.partners.add }));
    const base = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      if (request.method === "POST" && request.url.endsWith("/api/v1/app/rights/partners")) {
        return new Response(JSON.stringify({ detail: "invalid_domain" }), {
          status: 409,
          headers: { "content-type": "application/json" },
        });
      }
      return base?.(input, init) as Promise<Response>;
    });

    fireEvent.change(form.getByLabelText(en.rights.partners.name), { target: { value: "Shop" } });
    fireEvent.change(form.getByLabelText(en.rights.partners.domain), { target: { value: "no domain" } });
    fireEvent.click(form.getByRole("button", { name: en.rights.partners.add }));

    expect(await form.findByText(en.rights.errors.codes.invalid_domain)).toBeTruthy();
    expect(screen.queryByText("invalid_domain")).toBeNull();
  });

  it("says the rights status is not shown here where the server does not serve it (production)", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/rights": { status: 404, body: {} } });
    renderAt("/app/rights");

    expect(await screen.findByText(en.rights.notOnline)).toBeTruthy();
    expect(screen.getByRole("link", { name: en.nav.rights })).toBeTruthy();
  });
});
