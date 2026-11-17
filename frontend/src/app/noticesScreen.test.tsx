// @vitest-environment jsdom
/**
 * The account-wide notices screen (`/app/notices`) against a stubbed customer API: every draft
 * across every original, its status, how many matching copies it names, and whether it links back
 * to the original and into the record's own drafts screen.
 */
import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLanguageForTest } from "../i18n";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import {
  noticeFinding,
  notice,
  noticeSubmission,
  originalId,
  remoteLoaders,
  renderAt,
  serveCustomer,
} from "./testApi";

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

describe("the account-wide notices screen", () => {
  it("lists every draft, newest first, with its status, copy count and links", async () => {
    const notices = [
      notice({
        id: "notice_" + "2".repeat(32),
        original_id: originalId(7),
        status: "marked_sent_by_customer",
        marked_sent_at: "2026-09-16T09:00:00Z",
        findings: [noticeFinding({ included: true }), noticeFinding({ page_id: "page_" + "9".repeat(32), included: false })],
      }),
      notice({
        id: "notice_" + "1".repeat(32),
        original_id: originalId(8),
        work_title: "Second work",
        status: "draft",
        findings: [noticeFinding(), noticeFinding({ page_id: "page_" + "8".repeat(32) })],
      }),
    ];
    const requested = serveCustomer(fetchMock, { "/api/v1/app/notices": { status: 200, body: notices } });
    const { container } = renderAt("/app/notices");

    expect(await screen.findByRole("heading", { name: en.notices.list.heading })).toBeTruthy();
    // The nav item itself, not just the route: this is the reachability gap issue #33's Next
    // port had (the screen existed but nothing linked to it) — proven here by rendering the real
    // nav and following its href, not by asserting the route exists.
    const navLink = screen.getByRole("link", { name: en.nav.notices });
    expect(navLink.getAttribute("href")).toBe("/app/notices");

    const entries = await screen.findAllByRole("article");
    expect(entries).toHaveLength(2);

    const sent = within(entries[0] as HTMLElement);
    expect(sent.getByText(en.notices.statuses.marked_sent_by_customer)).toBeTruthy();
    expect(sent.getByText(en.notices.list.copies_one.replace("{{count}}", "1"))).toBeTruthy();
    const originalLink = sent.getByRole("link", { name: "Beach day" });
    expect(originalLink.getAttribute("href")).toBe(`/app/originals/${originalId(7)}`);
    const openLink = sent.getByRole("link", { name: en.notices.list.open });
    expect(openLink.getAttribute("href")).toBe(`/app/originals/${originalId(7)}/notices`);

    const draft = within(entries[1] as HTMLElement);
    expect(draft.getByText(en.notices.statuses.draft)).toBeTruthy();
    expect(draft.getByText(en.notices.list.notSentYet)).toBeTruthy();
    expect(draft.getByText(en.notices.list.copies_other.replace("{{count}}", "2"))).toBeTruthy();

    expect(requested.every((path) => path.startsWith("/api/v1/app/"))).toBe(true);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("says so when there are none, in German too, and the nav links here", async () => {
    await setLanguageForTest("de");
    serveCustomer(fetchMock, { "/api/v1/app/notices": { status: 200, body: [] } });
    renderAt("/app/notices");

    expect(await screen.findByText(de.notices.list.empty)).toBeTruthy();
    expect(screen.getByRole("link", { name: de.nav.notices })).toBeTruthy();
  });

  it("offers a retry when the list cannot be loaded", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/notices": { status: 403, body: { detail: "boom" } } });
    renderAt("/app/notices");

    expect(await screen.findByRole("button", { name: en.common.retry })).toBeTruthy();
    expect(screen.queryByText("boom")).toBeNull();
  });
});

describe("recent notice actions block (issue #86, ADR 0046 §7)", () => {
  it("shows a pending write, a refusal's own sentence, and nothing for a settled one", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/notices": { status: 200, body: [] },
      "/api/v1/app/notices/submissions": {
        status: 200,
        body: [
          noticeSubmission({ id: "sub_" + "1".repeat(32), status: "processing" }),
          noticeSubmission({
            id: "sub_" + "2".repeat(32),
            status: "refused",
            code: "finding_rights_blocked",
          }),
          noticeSubmission({ id: "sub_" + "3".repeat(32), status: "done", code: "created" }),
        ],
      },
    });
    renderAt("/app/notices");

    expect(await screen.findByRole("heading", { name: en.notices.submissions.heading })).toBeTruthy();
    expect(screen.getByText(en.notices.submissions.processing)).toBeTruthy();
    expect(screen.getByText(en.notices.errors.codes.finding_rights_blocked)).toBeTruthy();
    // Only the two unsettled ones: the done write needs no line of its own.
    expect(screen.getAllByText(en.notices.submissions.kinds.notice_approve)).toHaveLength(2);
    expect(screen.queryByText("finding_rights_blocked")).toBeNull();
  });

  it("renders nothing when every submission is settled or there are none", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/notices": { status: 200, body: [] },
      "/api/v1/app/notices/submissions": { status: 200, body: [] },
    });
    renderAt("/app/notices");

    await screen.findByText(en.notices.list.empty);
    expect(screen.queryByRole("heading", { name: en.notices.submissions.heading })).toBeNull();
  });
});
