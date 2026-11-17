// @vitest-environment jsdom
/**
 * The notice-draft screen (issue #33, ADR 0035) against a stubbed customer API: candidates with
 * their blockers, creating a draft, and approving one through to its frozen text — plus the
 * privacy rule (no element able to load a remote resource).
 */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLanguageForTest } from "../i18n";
import { en } from "../i18n/en";
import type { Notice, NoticeCandidates } from "./queries";
import { noticeSubmission, remoteLoaders, renderAt, serveCustomer } from "./testApi";

// Load-bearing, see customerApp.test.tsx.
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

const ORIGINAL_ID = "orig_" + "7".repeat(32);
const PAGE_ID_OK = "page_" + "1".repeat(32);
const PAGE_ID_BLOCKED = "page_" + "2".repeat(32);
const NOTICE_ID = "notice_" + "3".repeat(32);

const CANDIDATES: NoticeCandidates = {
  original_id: ORIGINAL_ID,
  title: "Beach day",
  findings: [
    {
      page_id: PAGE_ID_OK,
      site: "site01.example",
      url: "https://site01.example/watch/1",
      availability: "playable",
      last_checked: "2026-09-17T10:00:00Z",
      extent: "full_copy",
      requires_exception_review: false,
      blockers: [],
      providers: [
        {
          hostname: "site01.example",
          slot: "network",
          provider_name: "Example Hosting",
          nexus: "eu_facing",
          function: "origin_host",
          channels: [
            {
              kind: "abuse_email",
              support: "primary",
              email: "abuse@hosting.example",
              url: null,
              source: "rdap",
              checked_at: null,
              recheck_due: false,
              channel_id: null,
              forwards_to_site_operator: null,
              publishes_to_transparency_db: null,
            },
          ],
          suggested_channel_kind: "abuse_email",
        },
      ],
    },
    {
      page_id: PAGE_ID_BLOCKED,
      site: "other.example",
      url: "https://other.example/watch/2",
      availability: "playable",
      last_checked: "2026-09-17T10:00:00Z",
      extent: "not_determined",
      requires_exception_review: false,
      blockers: ["authority_not_declared"],
      providers: [],
    },
  ],
};

function draftNotice(overrides: Partial<Notice> = {}): Notice {
  return {
    id: NOTICE_ID,
    original_id: ORIGINAL_ID,
    status: "draft",
    is_sandbox: false,
    variant: "dsa_art16",
    language: "en",
    recipient_hostname: "site01.example",
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
    findings: [
      {
        page_id: PAGE_ID_OK,
        url: "https://site01.example/watch/1",
        availability: "playable",
        extent: "full_copy",
        requires_exception_review: false,
        exception_confirmed: false,
        included: true,
      },
    ],
    required_confirmations: [
      { kind: "rights", version: "notice-rights-2026-09-17", en: "I hold the relevant right.", de: "…" },
      { kind: "accuracy", version: "notice-accuracy-2026-09-17", en: "I have reviewed the notice.", de: "…" },
    ],
    confirmations: [],
    preview_text: "Subject: Notice under Article 16 DSA...",
    approved_text: null,
    ...overrides,
  };
}

describe("the notice-draft screen", () => {
  it("shows a pending write and a refusal, from GET /app/notices/submissions (issue #86, ADR 0046 §7)", async () => {
    serveCustomer(fetchMock, {
      [`/api/v1/app/notices/candidates/${ORIGINAL_ID}`]: { status: 200, body: { ...CANDIDATES, findings: [] } },
      "/api/v1/app/notices": { status: 200, body: [] },
      "/api/v1/app/notices/submissions": {
        status: 200,
        body: [
          noticeSubmission({ id: "sub_" + "1".repeat(32), status: "processing", notice_id: null }),
          noticeSubmission({
            id: "sub_" + "2".repeat(32),
            status: "refused",
            code: "finding_unavailable",
          }),
        ],
      },
    });
    renderAt(`/app/originals/${ORIGINAL_ID}/notices`);

    await screen.findByRole("heading", { name: en.notices.submissions.heading });
    expect(screen.getByText(en.notices.submissions.processing)).toBeTruthy();
    expect(screen.getByText(en.notices.errors.codes.finding_unavailable)).toBeTruthy();
    expect(screen.queryByText("finding_unavailable")).toBeNull();
  });

  it("lists eligible and blocked candidates, and creates a draft from a selection", async () => {
    const requested = serveCustomer(fetchMock, {
      [`/api/v1/app/notices/candidates/${ORIGINAL_ID}`]: { status: 200, body: CANDIDATES },
      "/api/v1/app/notices": { status: 200, body: [] },
      [`/api/v1/app/notices/${NOTICE_ID}`]: { status: 200, body: draftNotice() },
    });
    renderAt(`/app/originals/${ORIGINAL_ID}/notices`);

    await screen.findByRole("heading", { level: 1, name: /Beach day/ });
    // The blocked candidate shows why, in words, not a raw code.
    expect(await screen.findByText(en.notices.blockers.authority_not_declared, { exact: false })).toBeTruthy();

    const checkbox = (await screen.findByText("site01.example")).closest("li")?.querySelector("input[type=checkbox]");
    expect(checkbox).toBeTruthy();
    fireEvent.click(checkbox as HTMLInputElement);

    const channelRadio = await screen.findByRole("radio", { name: /abuse mailbox/i });
    fireEvent.click(channelRadio);
    fireEvent.change(screen.getByLabelText(en.notices.create.claimantName), { target: { value: "Novak Studio" } });
    fireEvent.change(screen.getByLabelText(en.notices.create.claimantEmail), { target: { value: "rights@novak.example" } });

    fireEvent.click(screen.getByRole("button", { name: en.notices.create.submit }));

    // The draft was created, and the list was refetched afterwards.
    await waitFor(() => expect(requested.filter((path) => path === "/api/v1/app/notices").length).toBeGreaterThanOrEqual(2));
  });

  it("approves a draft and shows the frozen text with a copy button and a mailto link", async () => {
    const notice = draftNotice();
    const requested = serveCustomer(fetchMock, {
      [`/api/v1/app/notices/candidates/${ORIGINAL_ID}`]: { status: 200, body: { ...CANDIDATES, findings: [] } },
      "/api/v1/app/notices": { status: 200, body: [notice] },
      [`/api/v1/app/notices/${NOTICE_ID}`]: { status: 200, body: notice },
      [`/api/v1/app/notices/${NOTICE_ID}/approve`]: {
        status: 200,
        body: { ...notice, status: "customer_approved", approved_text: "Full rendered notice text.", approved_at: "2026-09-17T11:00:00Z" },
      },
    });
    renderAt(`/app/originals/${ORIGINAL_ID}/notices`);

    await screen.findByText(en.notices.preview.heading);
    const boxes = screen.getAllByRole("checkbox");
    for (const box of boxes) {
      fireEvent.click(box);
      expect((box as HTMLInputElement).checked).toBe(true);
    }
    const approveButton = screen.getByRole("button", { name: en.notices.confirmations.approve });
    expect((approveButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(approveButton);
    await waitFor(() => expect(requested).toContain(`/api/v1/app/notices/${NOTICE_ID}/approve`));

    expect(await screen.findByText("Full rendered notice text.")).toBeTruthy();
    expect(screen.getByRole("link", { name: en.notices.approved.mailto }).getAttribute("href")).toMatch(/^mailto:abuse@hosting\.example\?/);
    expect(screen.getByRole("button", { name: en.notices.approved.markSent })).toBeTruthy();
    // The three files the customer takes away, each a same-origin link to the customer API.
    expect(screen.getByRole("link", { name: en.notices.approved.downloadText }).getAttribute("href")).toBe(
      `/api/v1/app/notices/${NOTICE_ID}/export/txt`,
    );
    expect(screen.getByRole("link", { name: en.notices.approved.downloadEml }).getAttribute("href")).toBe(
      `/api/v1/app/notices/${NOTICE_ID}/export/eml`,
    );
    expect(screen.getByRole("link", { name: en.notices.approved.downloadAnnex }).getAttribute("href")).toBe(
      `/api/v1/app/notices/${NOTICE_ID}/export/annex`,
    );
    // "Prepare", never "Send": nothing here claims Marceline transmitted anything.
    expect(screen.queryByRole("button", { name: /^send$/i })).toBeNull();
  });

  it("says nothing about a removal while the pages it names are still up", async () => {
    const sent = draftNotice({ status: "marked_sent_by_customer", marked_sent_at: "2026-09-17T11:00:00Z" });
    serveCustomer(fetchMock, {
      [`/api/v1/app/notices/candidates/${ORIGINAL_ID}`]: { status: 200, body: { ...CANDIDATES, findings: [] } },
      "/api/v1/app/notices": { status: 200, body: [sent] },
      [`/api/v1/app/notices/${NOTICE_ID}`]: { status: 200, body: sent },
    });
    renderAt(`/app/originals/${ORIGINAL_ID}/notices`);

    await screen.findByText(en.notices.statuses.marked_sent_by_customer);
    expect(screen.queryByText(/first found every page/i)).toBeNull();
  });

  it("says when our own availability checks first found every page it names gone", async () => {
    const observed = draftNotice({
      status: "marked_sent_by_customer",
      marked_sent_at: "2026-09-17T11:00:00Z",
      removal_observed_at: "2026-09-19T09:00:00Z",
    });
    serveCustomer(fetchMock, {
      [`/api/v1/app/notices/candidates/${ORIGINAL_ID}`]: { status: 200, body: { ...CANDIDATES, findings: [] } },
      "/api/v1/app/notices": { status: 200, body: [observed] },
      [`/api/v1/app/notices/${NOTICE_ID}`]: { status: 200, body: observed },
    });
    renderAt(`/app/originals/${ORIGINAL_ID}/notices`);

    // The stamp comes from our own checks, and the screen says so — it is not a provider's word.
    expect(await screen.findByText(/first found every page/i)).toBeTruthy();
  });

  it("has nothing able to load a remote resource", async () => {
    serveCustomer(fetchMock, {
      [`/api/v1/app/notices/candidates/${ORIGINAL_ID}`]: { status: 200, body: CANDIDATES },
      "/api/v1/app/notices": { status: 200, body: [draftNotice()] },
      [`/api/v1/app/notices/${NOTICE_ID}`]: { status: 200, body: draftNotice() },
    });
    const { container } = renderAt(`/app/originals/${ORIGINAL_ID}/notices`);

    await screen.findByText("site01.example");
    expect(remoteLoaders(container)).toHaveLength(0);
  });
});

