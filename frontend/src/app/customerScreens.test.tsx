// @vitest-environment jsdom
/**
 * The overview, the original detail and the removals log against a stubbed customer API: their
 * loading, empty, error and not-found states, what each state says, and both languages.
 *
 * Expected copy is read from the resource files, so rewording is not a failing test — a missing or
 * wrong message is.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLanguageForTest } from "../i18n";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import { formatDate, formatDay } from "./parts";
import {
  NOT_FOUND,
  caseDetail,
  finding,
  original,
  originalId,
  pageId,
  removal,
  remoteLoaders,
  renderAt,
  rightsOverview,
  rightsWork,
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

/** i18next's plural resolution, spelled out for the two languages' one/other rule. */
function plural(forms: { one: string; other: string }, count: number, values: Record<string, string> = {}) {
  const text = count === 1 ? forms.one : forms.other;
  return Object.entries({ count: String(count), ...values }).reduce(
    (result, [key, value]) => result.replaceAll(`{{${key}}}`, value),
    text,
  );
}

const FORBIDDEN_ANSWER = { status: 403, body: { detail: "Forbidden" } };

beforeEach(async () => {
  fetchMock.mockReset();
  window.localStorage.clear();
  document.cookie = "csrftoken=token";
  await setLanguageForTest("en");
});

afterEach(() => {
  cleanup();
});

describe("the overview", () => {
  const ORIGINALS = [
    original({ id: originalId(1), title: "Nothing here", state: "nothing_found", counts: { confirmed: 0, removed: 0 } }),
    original({ id: originalId(2), title: "All gone", state: "found_removed", counts: { confirmed: 1, removed: 1 } }),
    original({ id: originalId(7), title: "Beach day", state: "found_online", counts: { confirmed: 2, removed: 1 } }),
  ];
  const FINDINGS = [
    finding({ original_id: originalId(7), site: "video.example" }),
    finding({ original_id: originalId(7), page_id: pageId(102), site: "mirror.example", online: false }),
    finding({ original_id: originalId(2), page_id: pageId(201), site: "gone.example", online: false }),
  ];

  it("answers per original: state, counts, where — online first", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: ORIGINALS },
      "/api/v1/app/findings": { status: 200, body: FINDINGS },
    });
    const { container } = renderAt("/app");

    const cards = await screen.findAllByRole("article");
    expect(cards.map((card) => card.getAttribute("aria-label"))).toEqual(["Beach day", "All gone", "Nothing here"]);

    const online = within(cards[0] as HTMLElement);
    expect(online.getByText(en.states.found_online)).toBeTruthy();
    expect(online.getByText(plural({ one: en.overview.confirmed_one, other: en.overview.confirmed_other }, 2, { removed: "1" }))).toBeTruthy();
    expect(await online.findByText(en.overview.sites.replace("{{sites}}", "video.example, mirror.example"))).toBeTruthy();
    expect(online.getByRole("link", { name: en.overview.openCase }).getAttribute("href")).toBe(`/app/originals/${originalId(7)}`);

    const removed = within(cards[1] as HTMLElement);
    expect(removed.getByText(en.states.found_removed)).toBeTruthy();

    const nothing = within(cards[2] as HTMLElement);
    expect(nothing.getByText(en.states.nothing_found)).toBeTruthy();
    expect(nothing.getByText(en.overview.nothingFound)).toBeTruthy();
    expect(nothing.queryByRole("link", { name: en.overview.openCase })).toBeNull();

    // Bands, never percentages; and nothing that could fetch a picture.
    expect(container.textContent).not.toMatch(/%/);
    // No trace of review work: unconfirmed copies do not exist for a customer (2026-09-17).
    expect(container.textContent).not.toMatch(/being checked|still checking|in review|reviewing/i);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("says roughly how much we watch — a bucket, never the count or the sites", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/originals": { status: 200, body: ORIGINALS } });
    const { container } = renderAt("/app");

    expect(
      await screen.findByText(en.coverage.watching.replace("{{sources}}", en.coverage.sources["10_plus"])),
    ).toBeTruthy();
    expect(container.textContent).not.toMatch(/\b17\b/);
  });

  it("says monitoring is active and what appears here — never how busy we are", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/originals": { status: 200, body: ORIGINALS } });
    renderAt("/app");

    expect(await screen.findByText(en.overview.monitoring)).toBeTruthy();
    expect(screen.getByText(en.overview.explainer)).toBeTruthy();
    cleanup();

    await setLanguageForTest("de");
    serveCustomer(fetchMock, { "/api/v1/app/originals": { status: 200, body: [] } });
    renderAt("/app");
    expect(await screen.findByText(de.overview.monitoring)).toBeTruthy();
    expect(screen.getByText(de.overview.explainer)).toBeTruthy();
  });

  it("summarises the three states", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/originals": { status: 200, body: ORIGINALS } });
    renderAt("/app");

    expect(await screen.findByText(en.overview.summary.found_online_one.replace("{{count}}", "1"))).toBeTruthy();
    expect(screen.getByText(en.overview.summary.found_removed_one.replace("{{count}}", "1"))).toBeTruthy();
    expect(screen.getByText(en.overview.summary.nothing_found_one.replace("{{count}}", "1"))).toBeTruthy();
  });

  it("shows a loading state", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/originals": "pending" });
    renderAt("/app");
    expect(await screen.findByText(en.common.loading)).toBeTruthy();
  });

  it("says when there are no originals", async () => {
    serveCustomer(fetchMock, {});
    renderAt("/app");
    expect(await screen.findByText(en.overview.empty)).toBeTruthy();
  });

  it("shows an error with a way to retry, not the server's text", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/originals": FORBIDDEN_ANSWER });
    renderAt("/app");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(en.common.loadFailed);
    expect(alert.textContent).not.toContain("Forbidden");

    serveCustomer(fetchMock, { "/api/v1/app/originals": { status: 200, body: ORIGINALS } });
    fireEvent.click(within(alert).getByRole("button", { name: en.common.retry }));
    expect(await screen.findByRole("article", { name: "Beach day" })).toBeTruthy();
  });

  it("keeps the answer when only the sites fail to load", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: ORIGINALS },
      "/api/v1/app/findings": FORBIDDEN_ANSWER,
    });
    renderAt("/app");

    const card = await screen.findByRole("article", { name: "Beach day" });
    expect(await within(card).findByText(en.overview.sitesFailed)).toBeTruthy();
    expect(within(card).getByText(en.states.found_online)).toBeTruthy();
  });

  it("offers the customer navigation", async () => {
    serveCustomer(fetchMock, {});
    renderAt("/app");
    const nav = await screen.findByRole("navigation", { name: en.nav.label });
    expect(within(nav).getByRole("link", { name: en.nav.removals }).getAttribute("href")).toBe("/app/removals");
  });

  it("speaks German", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: ORIGINALS },
      "/api/v1/app/findings": { status: 200, body: FINDINGS },
    });
    await setLanguageForTest("de");
    renderAt("/app");

    expect(await screen.findByRole("heading", { name: de.overview.heading })).toBeTruthy();
    const card = await screen.findByRole("article", { name: "Beach day" });
    expect(within(card).getByText(de.states.found_online)).toBeTruthy();
    expect(within(card).getByText(plural({ one: de.overview.confirmed_one, other: de.overview.confirmed_other }, 2, { removed: "1" }))).toBeTruthy();
    expect(within(card).getByRole("link", { name: de.overview.openCase })).toBeTruthy();
  });

  it("answers the state of the whole catalogue: totals across every original (issue #32)", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: ORIGINALS },
      "/api/v1/app/findings": { status: 200, body: FINDINGS },
    });
    renderAt("/app");

    // 0+1+2 human-reviewed matching copies across the three originals, 0+1+1 of them removed.
    const heading = await screen.findByText(en.overview.totals.heading);
    const section = heading.closest("section");
    expect(section).toBeTruthy();
    expect(section?.textContent).toContain(
      plural({ one: en.overview.totals.originals_one, other: en.overview.totals.originals_other }, 3),
    );
    expect(section?.textContent).toContain(
      plural({ one: en.overview.confirmed_one, other: en.overview.confirmed_other }, 3, { removed: "2" }),
    );
  });

  it("lists what changed most recently across the catalogue, newest first", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: ORIGINALS },
      "/api/v1/app/findings": { status: 200, body: FINDINGS },
      "/api/v1/app/removals": {
        status: 200,
        body: [removal({ original_id: originalId(2), title: "All gone", removed_at: "2026-09-12T07:00:00Z" })],
      },
    });
    renderAt("/app");

    const removedLine = en.overview.recent.removed.replace("{{title}}", "All gone").replace("{{date}}", formatDate("2026-09-12T07:00:00Z", "en"));
    expect(await screen.findByText(removedLine)).toBeTruthy();
  });

  it("says nothing changed recently when there is nothing to report", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/originals": { status: 200, body: ORIGINALS } });
    renderAt("/app");
    expect(await screen.findByText(en.overview.recent.empty)).toBeTruthy();
  });

  it("notes a rights step still in the way of a notice draft, only on the original it blocks (ADR 0034)", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: ORIGINALS },
      "/api/v1/app/findings": { status: 200, body: FINDINGS },
      "/api/v1/app/rights": {
        status: 200,
        body: rightsOverview({
          works: [rightsWork({ original_id: originalId(7), notice_blockers: ["claimant_type_missing"] })],
        }),
      },
    });
    renderAt("/app");

    const blocked = within(await screen.findByRole("article", { name: "Beach day" }));
    expect(await blocked.findByText(en.rights.works.blockers.claimant_type_missing)).toBeTruthy();
    expect(blocked.getByRole("link", { name: en.overview.rightsLink }).getAttribute("href")).toBe("/app/rights");

    // "All gone" has findings too but no rights row named it: no blocker note on its card.
    const other = within(await screen.findByRole("article", { name: "All gone" }));
    expect(other.queryByText(en.rights.works.missing)).toBeNull();
  });

  it("says nothing about rights when that read is not served here (production, ADR 0034)", async () => {
    const requested = serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: ORIGINALS },
      "/api/v1/app/findings": { status: 200, body: FINDINGS },
      "/api/v1/app/rights": { status: 404, body: { detail: "not found" } },
    });
    renderAt("/app");

    const card = await screen.findByRole("article", { name: "Beach day" });
    expect(within(card).queryByText(en.rights.works.missing)).toBeNull();
    await waitFor(() => expect(requested).toContain("/api/v1/app/rights"));
  });
});

describe("the original detail", () => {
  it("shows the case and every copy, each linking to its case file", async () => {
    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: { status: 200, body: caseDetail() } });
    const { container } = renderAt(`/app/originals/${originalId(7)}`);

    expect(await screen.findByRole("heading", { level: 1, name: "Beach day" })).toBeTruthy();
    expect(screen.getByText(en.original.case.opened.replace("{{date}}", formatDay("2026-09-02", "en")))).toBeTruthy();
    expect(screen.getByText(en.original.case.open)).toBeTruthy();

    const links = screen.getAllByRole("link", { name: en.finding.openCaseFile });
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      `/app/originals/${originalId(7)}/copies/${pageId(101)}`,
      `/app/originals/${originalId(7)}/copies/${pageId(102)}`,
      `/app/originals/${originalId(7)}/copies/${pageId(103)}`,
    ]);
    expect(screen.getByText(en.finding.confirmedOn.replace("{{date}}", formatDay("2026-09-03", "en")))).toBeTruthy();
    expect(screen.getAllByText(en.finding.whereabouts.gone)).toHaveLength(1);
    // How much of the original each copy carries — excerpts are copies like any other (issue #62).
    for (const extent of ["full_copy", "excerpt", "not_determined"] as const) {
      expect(screen.getAllByText(en.finding.extent[extent].label)).toHaveLength(1);
    }
    // The detail page links to case files, never to the pirate pages themselves.
    expect([...container.querySelectorAll("a")].some((link) => link.target === "_blank")).toBe(false);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("says a fully removed case stays open, and when it reopened", async () => {
    const detail = caseDetail({
      original: original({ state: "found_removed" }),
      case: {
        original_id: originalId(7),
        title: "Beach day",
        state: "removed_watching",
        opened_on: "2026-09-02",
        removed_at: "2026-09-12T10:00:00Z",
        reopened_on: "2026-09-08",
        reopen_count: 2,
        counts: { confirmed: 1, removed: 1 },
      },
    });
    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: { status: 200, body: detail } });
    renderAt(`/app/originals/${originalId(7)}`);

    expect(
      await screen.findByText(en.original.case.removedWatching.replace("{{date}}", formatDate("2026-09-12T10:00:00Z", "en"))),
    ).toBeTruthy();
    expect(
      screen.getByText(
        plural({ one: en.original.case.reopened_one, other: en.original.case.reopened_other }, 2, {
          date: formatDay("2026-09-08", "en"),
        }),
      ),
    ).toBeTruthy();
  });

  it("says there is no case while nothing was found", async () => {
    const detail = caseDetail({
      original: original({ state: "nothing_found", counts: { confirmed: 0, removed: 0 } }),
      case: null,
      findings: [],
    });
    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: { status: 200, body: detail } });
    renderAt(`/app/originals/${originalId(7)}`);

    expect(await screen.findByText(en.original.case.none)).toBeTruthy();
    expect(screen.queryByRole("heading", { name: en.original.findings.heading })).toBeNull();
  });

  it("renders a 404 as not found, not as an error", async () => {
    const requested = serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(9)}`]: NOT_FOUND });
    renderAt(`/app/originals/${originalId(9)}`);

    expect(await screen.findByRole("heading", { name: en.notFound.heading })).toBeTruthy();
    expect(screen.getByText(en.notFound.original)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Case not found/)).toBeNull();
    // Asked once: a 404 is an answer, not something to retry.
    expect(requested.filter((path) => path === `/api/v1/app/cases/${originalId(9)}`)).toHaveLength(1);
  });

  it.each(["abc", "7", pageId(7), originalId(7).toUpperCase()])(
    "does not even ask for %s, which is not an original's public id",
    async (id) => {
      // An old integer URL is simply not found (ADR 0033): no redirect, no request.
      const requested = serveCustomer(fetchMock, {});
      renderAt(`/app/originals/${id}`);

      expect(await screen.findByText(en.notFound.original)).toBeTruthy();
      expect(requested.some((path) => path.startsWith("/api/v1/app/cases"))).toBe(false);
    },
  );

  it("shows loading and error states", async () => {
    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: "pending" });
    renderAt(`/app/originals/${originalId(7)}`);
    expect(await screen.findByText(en.common.loading)).toBeTruthy();
    cleanup();

    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: FORBIDDEN_ANSWER });
    renderAt(`/app/originals/${originalId(7)}`);
    expect((await screen.findByRole("alert")).textContent).toContain(en.common.loadFailed);
  });

  it("speaks German", async () => {
    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: { status: 200, body: caseDetail() } });
    await setLanguageForTest("de");
    renderAt(`/app/originals/${originalId(7)}`);

    expect(await screen.findByRole("heading", { name: de.original.case.heading })).toBeTruthy();
    expect(screen.getByRole("heading", { name: de.original.findings.heading })).toBeTruthy();
    expect(screen.getAllByRole("link", { name: de.finding.openCaseFile })).toHaveLength(3);
    expect(screen.getByText(de.finding.extent.excerpt.label)).toBeTruthy();
  });
});

describe("the removals log", () => {
  const REMOVALS = [
    removal({ page_id: pageId(102), title: "Beach day", site: "mirror.example" }),
    removal({ page_id: pageId(104), title: "Pool", site: "open.example", handled: false, case_state: "open" }),
    removal({
      page_id: pageId(105),
      title: "Garden",
      site: "back.example",
      state: "reopened",
      handled: false,
      reappeared_at: "2026-09-14T10:00:00Z",
      case_state: "open",
    }),
  ];

  it("lists what came down, whether it stayed down, and what was watched", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/removals": { status: 200, body: REMOVALS } });
    const { container } = renderAt("/app/removals");

    expect(await screen.findByRole("heading", { name: en.removals.heading })).toBeTruthy();
    // The limit of "handled" is said before any entry claims it, with the rough coverage.
    const note = screen.getByRole("note");
    expect(note.textContent).toContain(en.removals.watched);
    expect(
      await within(note).findByText(en.coverage.removals.replace("{{sources}}", en.coverage.sources["10_plus"]), {
        exact: false,
      }),
    ).toBeTruthy();

    const entries = await screen.findAllByRole("article");
    expect(entries).toHaveLength(3);
    expect(within(entries[0] as HTMLElement).getByText(en.removals.standing.handled)).toBeTruthy();
    expect(within(entries[1] as HTMLElement).getByText(en.removals.standing.removed_case_open)).toBeTruthy();
    expect(within(entries[1] as HTMLElement).queryByText(en.removals.standing.handled)).toBeNull();
    const reopened = within(entries[2] as HTMLElement);
    expect(reopened.getByText(en.removals.standing.reopened)).toBeTruthy();
    expect(reopened.getByText(en.removals.reappeared.replace("{{date}}", formatDate("2026-09-14T10:00:00Z", "en")))).toBeTruthy();

    const first = within(entries[0] as HTMLElement);
    expect(first.getByText(formatDate("2026-09-10T07:00:00Z", "en"))).toBeTruthy();
    expect(first.getByText("https://mirror.example/v/102")).toBeTruthy();
    expect(first.getByRole("link", { name: en.finding.openCaseFile }).getAttribute("href")).toBe(`/app/originals/${originalId(7)}/copies/${pageId(102)}`);
    // Addresses are text here; nothing links out and nothing loads. The frame's contact `mailto:`
    // is allowed by its scheme — it fetches nothing — so an http(s) link would still fail this.
    const outbound = [...container.querySelectorAll("a")].filter((link) => {
      const href = link.getAttribute("href") ?? "";
      return !href.startsWith("/app") && !href.startsWith("mailto:");
    });
    expect(outbound).toEqual([]);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("says when nothing has been removed", async () => {
    serveCustomer(fetchMock, {});
    renderAt("/app/removals");
    expect(await screen.findByText(en.removals.empty)).toBeTruthy();
  });

  it("shows loading and error states", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/removals": "pending" });
    renderAt("/app/removals");
    expect(await screen.findByText(en.common.loading)).toBeTruthy();
    cleanup();

    serveCustomer(fetchMock, { "/api/v1/app/removals": FORBIDDEN_ANSWER });
    renderAt("/app/removals");
    expect((await screen.findByRole("alert")).textContent).toContain(en.common.loadFailed);
  });

  it("speaks German", async () => {
    serveCustomer(fetchMock, { "/api/v1/app/removals": { status: 200, body: REMOVALS } });
    await setLanguageForTest("de");
    renderAt("/app/removals");

    expect(await screen.findByRole("heading", { name: de.removals.heading })).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain(de.removals.watched);
    expect(
      await screen.findByText(de.coverage.removals.replace("{{sources}}", de.coverage.sources["10_plus"]), { exact: false }),
    ).toBeTruthy();
    expect(await screen.findByText(de.removals.standing.handled)).toBeTruthy();
    await waitFor(() => expect(screen.getByText(de.removals.standing.reopened)).toBeTruthy());
  });
});

/**
 * Operator feedback 2026-09-17: a long title pushed the state label onto a ragged next line. jsdom
 * does no layout, so this pins the structure that prevents it: the label is a non-shrinking,
 * non-wrapping flex item beside a flexible title, and it only stacks under the title at the one
 * deliberate breakpoint (380 px and narrower). German, because its labels are the longer ones.
 */
describe("a long title never pushes the status label onto a ragged line", () => {
  const LONG = "Strandtag mit Freundinnen — die komplette ungekürzte Fassung, Teil 2 von 3 (Sommer 2026, Nordsee)";

  function classes(element: Element | null | undefined): string[] {
    return element?.getAttribute("class")?.split(/\s+/) ?? [];
  }

  /** Asserts the row around `label` and returns the title's slot. */
  function expectTitleRow(label: HTMLElement): HTMLElement {
    const badgeSlot = label.parentElement;
    const row = badgeSlot?.parentElement;
    expect(row?.hasAttribute("data-title-row")).toBe(true);
    const slots = [...(row?.children ?? [])];
    expect(slots).toHaveLength(2);
    expect(slots[1]).toBe(badgeSlot);

    // One row from 381 px; below that the label is a deliberate second row, never a flex wrap.
    expect(classes(row)).toEqual(
      expect.arrayContaining(["flex", "flex-col", "min-[381px]:flex-row", "min-[381px]:justify-between"]),
    );
    expect(classes(row)).not.toContain("flex-wrap");
    // The title takes the flexible space and may shrink; the label neither shrinks nor wraps.
    expect(classes(slots[0])).toEqual(expect.arrayContaining(["min-w-0", "min-[381px]:flex-1"]));
    expect(classes(badgeSlot)).toEqual(expect.arrayContaining(["shrink-0", "whitespace-nowrap"]));
    expect(classes(label)).toContain("whitespace-nowrap");
    return slots[0] as HTMLElement;
  }

  function expectClamped(element: Element | null | undefined) {
    expect(classes(element)).toEqual(expect.arrayContaining(["line-clamp-2", "break-words"]));
    expect(element?.getAttribute("title")).toBe(LONG);
  }

  it("on the overview", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/originals": { status: 200, body: [original({ title: LONG })] },
      "/api/v1/app/findings": { status: 200, body: [finding()] },
    });
    await setLanguageForTest("de");
    renderAt("/app");

    const card = await screen.findByRole("article", { name: LONG });
    const titleSlot = expectTitleRow(within(card).getByText(de.states.found_online));
    expectClamped(titleSlot.querySelector("h2"));
  });

  it("on the original detail: the heading shows in full, the labels stay put", async () => {
    serveCustomer(fetchMock, {
      [`/api/v1/app/cases/${originalId(7)}`]: { status: 200, body: caseDetail({ original: original({ title: LONG }) }) },
    });
    await setLanguageForTest("de");
    renderAt(`/app/originals/${originalId(7)}`);

    const heading = await screen.findByRole("heading", { level: 1, name: LONG });
    const titleSlot = expectTitleRow(screen.getByText(de.states.found_online));
    expect(titleSlot.contains(heading)).toBe(true);
    expect(classes(heading).some((name) => name.startsWith("line-clamp"))).toBe(false);

    const labels = [...screen.getAllByText(de.finding.whereabouts.online), ...screen.getAllByText(de.finding.whereabouts.gone)];
    expect(labels.length).toBeGreaterThan(1);
    labels.forEach((label) => expectTitleRow(label));
  });

  it("in the removals log, with the longest standing", async () => {
    serveCustomer(fetchMock, {
      "/api/v1/app/removals": { status: 200, body: [removal({ title: LONG, handled: false, case_state: "open" })] },
    });
    await setLanguageForTest("de");
    renderAt("/app/removals");

    const entry = await screen.findByRole("article");
    const titleSlot = expectTitleRow(within(entry).getByText(de.removals.standing.removed_case_open));
    expectClamped(titleSlot.firstElementChild);
    // Site and address are not squeezed beside the label: only the title shares its row.
    expect(titleSlot.textContent).toBe(LONG);
  });
});
