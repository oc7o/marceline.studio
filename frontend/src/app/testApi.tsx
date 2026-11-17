/**
 * Test support for the customer screens: a stubbed customer API, fixtures shaped exactly like the
 * generated schema types, and the real route tree over a memory history.
 *
 * Not a test file itself (no `.test.` in the name) so several screen tests can share it. The fetch
 * stub must still be installed by each test file's own `vi.hoisted` block — openapi-fetch captures
 * `globalThis.fetch` when the client module is first imported (see `customerApp.test.tsx`).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import type { Mock } from "vitest";

import { routeTree } from "../router";
import type { Host } from "./caseFile";
import type { CustomerInvoice } from "./invoices";
import type { NoticeSubmission } from "./notices";
import type {
  CaseDetail,
  CaseStep,
  CaseUnknown,
  CustomerFinding,
  CustomerOriginal,
  Notice,
  NoticeFinding,
  Removal,
} from "./queries";
import type { RightsOverview, RightsWork } from "./rights";

export interface Reply {
  status: number;
  body: unknown;
}

/** A reply, or a promise that never settles (to hold a screen in its loading state). */
export type Route = Reply | "pending";

export const ORGANISATION = {
  id: "org_" + "1".repeat(32),
  name: "Novak Studio",
  segment: "professional",
  role: "owner",
  created_at: "2026-09-16T10:00:00Z",
};
export const ACCOUNT = {
  username: "test-customer@marceline.studio",
  organisation: ORGANISATION,
  monitored_sources: "10_plus",
};

export const CASE_TIMELINE: CaseStep[] = [
  { step: "confirmed", status: "done", at: null, on: "2026-09-02", reason: null, blocked_by_issue: null },
  { step: "notice_prepared", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "notice_sent", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "notice_acknowledged", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "removed", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "rechecked", status: "done", at: "2026-09-15T08:00:00Z", on: null, reason: null, blocked_by_issue: null },
];

/** Exactly what `marceline/app/cases.py` sends today (issue #33: notice drafts are real now). */
export const NOT_AVAILABLE: CaseUnknown[] = [
  { topic: "notice_delivery", reason: "legal_review_pending", blocked_by_issue: 46 },
  { topic: "reupload_watch_coverage", reason: "capability_not_built", blocked_by_issue: null },
];

export function original(overrides: Partial<CustomerOriginal> = {}): CustomerOriginal {
  return {
    id: originalId(7),
    title: "Beach day",
    kind: "video",
    duration_s: 600,
    created_at: "2026-08-01T09:00:00Z",
    state: "found_online",
    counts: { confirmed: 3, removed: 1 },
    ...overrides,
  };
}

/** A fully looked-up EU host; override for the other shapes. */
export function host(overrides: Partial<Host> = {}): Host {
  return {
    hostname: "video.example",
    lookup: "complete",
    looked_up_at: "2026-09-15T06:00:00Z",
    stale: false,
    addresses: ["192.0.2.10", "2001:db8::10"],
    asn: 64500,
    as_name: "EXAMPLE-HOSTING",
    provider: "Example Hosting GmbH",
    country: "DE",
    jurisdiction: "eu",
    cdn: null,
    abuse_contact: {
      email: "abuse@hosting.example",
      source: "ip_network",
      source_url: "https://rdap.db.ripe.net/ip/192.0.2.0/24",
    },
    registrar: {
      name: "Example Registrar Ltd",
      abuse_email: "abuse@registrar.example",
      source_url: "https://rdap.verisign.com/com/v1/domain/video.example",
    },
    response_history: null,
    ...overrides,
  };
}

export function finding(overrides: Partial<CustomerFinding> = {}): CustomerFinding {
  return {
    original_id: originalId(7),
    page_id: pageId(101),
    site: "video.example",
    url: "https://video.example/video/101",
    case_state: "open",
    availability: "playable",
    online: true,
    last_checked: "2026-09-14T09:00:00Z",
    confirmed_on: "2026-09-02",
    removed_at: null,
    reappeared_at: null,
    evidence: "fingerprint_match",
    extent: "full_copy",
    hosting: { page_host: host(), content_hosts: [] },
    ...overrides,
  };
}

export function caseDetail(overrides: Partial<CaseDetail> = {}): CaseDetail {
  return {
    original: original(),
    case: {
      original_id: originalId(7),
      title: "Beach day",
      state: "open",
      opened_on: "2026-09-02",
      removed_at: null,
      reopened_on: null,
      reopen_count: 0,
      counts: { confirmed: 3, removed: 1 },
    },
    findings: [
      finding(),
      finding({
        page_id: pageId(102),
        site: "mirror.example",
        url: "https://mirror.example/v/102",
        availability: "removed",
        online: false,
        removed_at: "2026-09-10T07:00:00Z",
        extent: "excerpt",
      }),
      finding({
        page_id: pageId(103),
        site: "other.example",
        url: "https://other.example/103",
        confirmed_on: "2026-09-03",
        evidence: "no_fingerprint_match",
        extent: "not_determined",
      }),
    ],
    timeline: CASE_TIMELINE,
    not_available: NOT_AVAILABLE,
    ...overrides,
  };
}

export function removal(overrides: Partial<Removal> = {}): Removal {
  return {
    original_id: originalId(7),
    title: "Beach day",
    page_id: pageId(102),
    site: "mirror.example",
    url: "https://mirror.example/v/102",
    removed_at: "2026-09-10T07:00:00Z",
    confirmed_on: "2026-09-02",
    reappeared_at: null,
    last_checked: "2026-09-15T08:00:00Z",
    state: "removed",
    case_state: "removed_watching",
    handled: true,
    ...overrides,
  };
}

export function noticeFinding(overrides: Partial<NoticeFinding> = {}): NoticeFinding {
  return {
    page_id: pageId(101),
    url: "https://video.example/watch/101",
    availability: "playable",
    extent: "full_copy",
    requires_exception_review: false,
    exception_confirmed: false,
    included: true,
    ...overrides,
  };
}

export function notice(overrides: Partial<Notice> = {}): Notice {
  return {
    id: "notice_" + "1".repeat(32),
    original_id: originalId(7),
    status: "draft",
    is_sandbox: false,
    variant: "dsa_art16",
    language: "en",
    recipient_hostname: "video.example",
    recipient_provider_name: "Example Hosting",
    channel_kind: "abuse_email",
    recipient_email: "abuse@hosting.example",
    recipient_url: "",
    work_title: "Beach day",
    created_at: "2026-09-17T10:00:00Z",
    approved_at: null,
    marked_sent_at: null,
    responded_at: null,
    outcome: null,
    removal_observed_at: null,
    withdrawn_at: null,
    withdrawal_reason: null,
    corrected_into: null,
    findings: [noticeFinding()],
    required_confirmations: [],
    confirmations: [],
    preview_text: "preview",
    approved_text: null,
    ...overrides,
  };
}

/** One notice write still pending, or refused after acceptance (issue #86, ADR 0046 §7). */
export function noticeSubmission(overrides: Partial<NoticeSubmission> = {}): NoticeSubmission {
  return {
    id: "sub_" + "1".repeat(32),
    kind: "notice_approve",
    status: "processing",
    code: "",
    notice_id: "notice_" + "1".repeat(32),
    received_at: "2026-09-17T09:00:00Z",
    ...overrides,
  };
}

export function rightsWork(overrides: Partial<RightsWork> = {}): RightsWork {
  return {
    original_id: originalId(7),
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
    notice_blockers: [],
    ...overrides,
  };
}

export function rightsOverview(overrides: Partial<RightsOverview> = {}): RightsOverview {
  return {
    claimant: { claimant_type: null, identity_status: "unverified", identity_method: null, account_control_token: null },
    work_declaration: { version: "work-1", en: "I confirm the English text.", de: "Ich bestätige den deutschen Text." },
    partner_declaration: { version: "partners-1", en: "No partners, in English.", de: "Keine Partner, auf Deutsch." },
    withdrawal_declaration: { version: "withdraw-1", en: "I withdraw it.", de: "Ich ziehe sie zurück." },
    authority_declaration: { version: "authority-1", en: "Our papers give us this.", de: "Unsere Unterlagen geben uns das." },
    authority_withdrawal_declaration: { version: "authority-withdraw-1", en: "I withdraw that.", de: "Ich ziehe das zurück." },
    partners: [],
    partners_none_affirmed_at: null,
    works: [],
    proofs: [],
    proof_upload: { available: true, max_bytes: 10485760, content_types: ["application/pdf", "image/png", "image/jpeg"] },
    ...overrides,
  };
}

export function invoice(overrides: Partial<CustomerInvoice> = {}): CustomerInvoice {
  return {
    id: invoiceId(11),
    number: "2026-00001",
    kind: "invoice",
    status: "open",
    issued_on: "2026-09-01",
    due_on: "2026-09-15",
    period_start: "2026-09-01",
    period_end: "2026-09-30",
    currency: "EUR",
    net_cents: 10000,
    vat_cents: 1900,
    gross_cents: 11900,
    vat_treatment: "domestic",
    paid_on: null,
    cancels_number: null,
    cancelled_by_number: null,
    ...overrides,
  };
}

/**
 * Public ids as the API sends them (ADR 0033): `orig_` / `page_` / `inv_` and 32 hex digits. The number
 * only keeps fixtures readable (`originalId(7)`); it is not a database key and nothing parses it.
 */
export function originalId(number: number): string {
  return `orig_${String(number).padStart(32, "0")}`;
}

export function invoiceId(number: number): string {
  return `inv_${String(number).padStart(32, "0")}`;
}

export function pageId(number: number): string {
  return `page_${String(number).padStart(32, "0")}`;
}

export const NOT_FOUND: Reply = { status: 404, body: { detail: "Case not found" } };

/**
 * Serve a signed-in customer plus the given `GET` routes (by path). Anything else is a 404, and
 * every request path is recorded so a test can assert what was (not) asked for.
 */
export function serveCustomer(fetchMock: Mock, routes: Record<string, Route>): string[] {
  const requested: string[] = [];
  const base: Record<string, Route> = {
    "/api/v1/app/auth/session": { status: 200, body: { authenticated: true, username: ACCOUNT.username } },
    "/api/v1/app/me": { status: 200, body: ACCOUNT },
    "/api/v1/app/originals": { status: 200, body: [] },
    "/api/v1/app/findings": { status: 200, body: [] },
    "/api/v1/app/removals": { status: 200, body: [] },
    "/api/v1/app/fingerprints/submissions": { status: 200, body: [] },
    "/api/v1/app/notices/submissions": { status: 200, body: [] },
  };
  const table = { ...base, ...routes };
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    const path = new URL(request.url, "http://localhost").pathname;
    requested.push(path);
    const route = table[path] ?? { status: 404, body: { detail: `unexpected ${path}` } };
    if (route === "pending") return new Promise<Response>(() => undefined);
    return new Response(JSON.stringify(route.body), {
      status: route.status,
      headers: { "content-type": "application/json" },
    });
  });
  return requested;
}

export function renderAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, container: result.container };
}

/** Anything in the rendered DOM that could load a remote resource by itself. */
export function remoteLoaders(container: HTMLElement): Element[] {
  return [
    ...container.querySelectorAll("img, picture, source, video, audio, iframe, embed, object, canvas, image, link"),
    ...container.querySelectorAll("[src], [srcset], [poster], [style*='url']"),
  ];
}
