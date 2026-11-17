// @vitest-environment jsdom
/**
 * The case file of one copy: the seven-step timeline, where the copy sits, why we think it is the
 * customer's, the hosting unknowns and the options — rendered honestly from what the API sends,
 * in both languages, with nothing that could load the pirate page or a picture of it.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n, setLanguageForTest } from "../i18n";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import { HostingSection } from "./CaseFileScreen";
import { REMOVAL_TIMES } from "./caseFile";
import { formatDate, formatDay } from "./parts";
import {
  NOT_FOUND,
  caseDetail,
  finding,
  host,
  originalId,
  pageId,
  remoteLoaders,
  renderAt,
  serveCustomer,
} from "./testApi";

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

const date = (value: string, language = "en") => formatDate(value, language);

beforeEach(async () => {
  fetchMock.mockReset();
  window.localStorage.clear();
  document.cookie = "csrftoken=token";
  await setLanguageForTest("en");
});

afterEach(() => {
  cleanup();
});

function serveCase(body = caseDetail()) {
  return serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: { status: 200, body } });
}

function timelineItems() {
  const list = screen.getByRole("list", { name: en.caseFile.timeline.heading });
  return within(list).getAllByRole("listitem");
}

describe("the case file", () => {
  it("renders all six steps: dated and attributed when done, upcoming or unavailable otherwise", async () => {
    serveCase();
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);
    await screen.findByRole("heading", { level: 1, name: en.caseFile.heading.replace("{{site}}", "video.example") });

    const items = timelineItems();
    expect(items.map((item) => [item.dataset.step, item.dataset.status])).toEqual([
      ["confirmed", "done"],
      ["notice_prepared", "pending"],
      ["notice_sent", "pending"],
      ["notice_acknowledged", "pending"],
      ["removed", "pending"],
      ["rechecked", "done"],
    ]);

    const [confirmed, prepared, sent, acknowledged, removed, rechecked] = items.map((item) => within(item));
    // Found and verified are one step, confirmed as your content, with the day and no time.
    expect(confirmed?.getByText(en.caseFile.timeline.steps.confirmed)).toBeTruthy();
    const day = formatDay("2026-09-02", "en");
    expect(confirmed?.getByText(day, { exact: false })).toBeTruthy();
    expect(confirmed?.getByText(en.caseFile.actors.us, { exact: false })).toBeTruthy();
    expect(items[0]?.textContent).not.toMatch(/\d{1,2}:\d{2}/);
    expect(rechecked?.getByText(date("2026-09-14T09:00:00Z"), { exact: false })).toBeTruthy();
    // Notice steps (issue #33): a real capability now, not reached yet for this record — pending,
    // no invented reason; the customer is the one who would send.
    expect(prepared?.getByText(en.caseFile.timeline.status.pending)).toBeTruthy();
    expect(sent?.getByText(en.caseFile.timeline.status.pending)).toBeTruthy();
    expect(sent?.getByText(en.caseFile.actors.you, { exact: false })).toBeTruthy();
    expect(acknowledged?.getByText(en.caseFile.timeline.status.pending)).toBeTruthy();
    // Not reached yet, and no date pretends otherwise.
    expect(removed?.getByText(en.caseFile.timeline.status.pending)).toBeTruthy();
    expect(removed?.getByText(en.caseFile.actors.site, { exact: false })).toBeTruthy();
    expect(removed?.queryByText(en.caseFile.timeline.status.done)).toBeNull();
  });

  it("shows where the copy sits, with the address as text and one explicit new-tab link", async () => {
    serveCase();
    const { container } = renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    const where = await screen.findByRole("region", { name: en.caseFile.where.heading });
    expect(within(where).getByText("https://video.example/video/101")).toBeTruthy();
    expect(within(where).getByText(date("2026-09-14T09:00:00Z"))).toBeTruthy();
    expect(within(where).getByText(en.caseFile.where.online)).toBeTruthy();
    expect(within(where).getByText(en.finding.extent.full_copy.label)).toBeTruthy();
    expect(within(where).getByText(en.finding.extent.full_copy.explanation)).toBeTruthy();

    const link = within(where).getByRole("link", { name: en.caseFile.where.open });
    expect(link.getAttribute("href")).toBe("https://video.example/video/101");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer noopener");
    // The only link on the page the browser could follow to a third party. The frame's contact
    // `mailto:` is not one — it opens a mail program and fetches nothing, so it is excluded by its
    // scheme, which leaves every http(s) link still failing this.
    const external = [...container.querySelectorAll("a")].filter((anchor) => {
      const href = anchor.getAttribute("href") ?? "";
      return !href.startsWith("/") && !href.startsWith("mailto:");
    });
    expect(external).toEqual([link]);

    // No embed, no image, no preview, no favicon — nothing that loads by itself.
    expect(remoteLoaders(container)).toEqual([]);
    expect(container.innerHTML).not.toMatch(/<(img|iframe|video|embed|object)\b/i);
  });

  it("says how much of the original a copy carries, and when that is not determined", async () => {
    serveCase(caseDetail({ findings: [finding({ extent: "excerpt" }), finding({ page_id: pageId(102), extent: "not_determined" })] }));
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);
    let where = await screen.findByRole("region", { name: en.caseFile.where.heading });
    expect(within(where).getByText(en.finding.extent.excerpt.explanation)).toBeTruthy();
    cleanup();

    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(102)}`);
    where = await screen.findByRole("region", { name: en.caseFile.where.heading });
    expect(within(where).getByText(en.finding.extent.not_determined.label)).toBeTruthy();
    expect(within(where).getByText(en.finding.extent.not_determined.explanation)).toBeTruthy();
  });

  it("keeps a non-web address as text only", async () => {
    serveCase(caseDetail({ findings: [finding({ url: "javascript:alert(1)" })] }));
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    expect(await screen.findByText("javascript:alert(1)")).toBeTruthy();
    expect(screen.queryByRole("link", { name: en.caseFile.where.open })).toBeNull();
  });

  it("dates a removal and says when the copy came back", async () => {
    serveCase(
      caseDetail({
        findings: [finding({ online: true, removed_at: "2026-09-05T00:00:00Z", reappeared_at: "2026-09-10T00:00:00Z" })],
      }),
    );
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    const where = await screen.findByRole("region", { name: en.caseFile.where.heading });
    expect(within(where).getByText(en.caseFile.where.reappeared.replace("{{date}}", date("2026-09-10T00:00:00Z")))).toBeTruthy();
    const removed = timelineItems()[4] as HTMLElement;
    expect(removed.dataset.status).toBe("done");
    expect(within(removed).getByText(en.caseFile.timeline.reappeared.replace("{{date}}", date("2026-09-10T00:00:00Z")))).toBeTruthy();
  });

  it("explains why it is yours in plain words — never how sure we are", async () => {
    serveCase();
    const { container } = renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    const why = await screen.findByRole("region", { name: en.caseFile.why.heading });
    expect(within(why).getByText(en.caseFile.why.fingerprintMatch)).toBeTruthy();
    expect(within(why).queryByText(en.caseFile.why.noMatch)).toBeNull();
    expect(within(why).getByText(en.caseFile.why.confirmed.replace("{{date}}", formatDay("2026-09-02", "en")))).toBeTruthy();
    expect(container.textContent).not.toMatch(/\d\s*%/);
  });

  it("shows who hosts it: provider, country and notice channel, abuse contact with its source, registrar, history", async () => {
    serveCase();
    const { container } = renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    const hosting = await screen.findByRole("region", { name: en.caseFile.hosting.heading });
    const block = within(within(hosting).getByRole("region", { name: "video.example" }));
    expect(block.getByText(en.caseFile.hosting.pageHost.replace("{{hostname}}", "video.example"))).toBeTruthy();
    expect(block.getByText("Example Hosting GmbH")).toBeTruthy();
    expect(block.getByText("Germany")).toBeTruthy();
    expect(block.getByText(en.caseFile.hosting.jurisdiction.eu)).toBeTruthy();
    expect(block.getByText(en.caseFile.hosting.noCdn)).toBeTruthy();
    expect(block.getByText("abuse@hosting.example")).toBeTruthy();
    expect(block.getByText(en.caseFile.hosting.abuseSource.ip_network)).toBeTruthy();
    expect(block.getByText(en.caseFile.hosting.record.replace("{{url}}", "https://rdap.db.ripe.net/ip/192.0.2.0/24"))).toBeTruthy();
    expect(block.getByText("Example Registrar Ltd")).toBeTruthy();
    expect(block.getByText(en.caseFile.hosting.registrarAbuse.replace("{{email}}", "abuse@registrar.example"))).toBeTruthy();
    expect(block.getByText(en.caseFile.hosting.facts.response_history)).toBeTruthy();
    expect(block.getByText(en.caseFile.hosting.history.none)).toBeTruthy();

    // Addresses and the AS number are technical details: collapsed, never up front.
    const details = hosting.querySelector("details");
    expect(details?.open).toBe(false);
    expect(details?.textContent).toContain("192.0.2.10");
    expect(details?.textContent).toContain("AS64500");
    const outside = (hosting.textContent ?? "").replace(details?.textContent ?? "", "");
    expect(outside).not.toContain("192.0.2.10");
    expect(outside).not.toContain("64500");
    // Registry records are text: the only link out of the case file is the pirate page itself.
    expect(container.querySelectorAll("a[href*='rdap']")).toHaveLength(0);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("names the network in front, preview hosts and an old lookup", async () => {
    serveCase(
      caseDetail({
        findings: [
          finding({
            hosting: {
              page_host: host({ cdn: "cloudflare", stale: true, country: "US", jurisdiction: "us" }),
              content_hosts: [
                host({
                  hostname: "cdn.example",
                  lookup: "partial",
                  provider: null,
                  country: null,
                  jurisdiction: "unknown",
                  abuse_contact: null,
                  registrar: null,
                  response_history: { removed_percent: 75, usual_removal_time: "3_plus_days" },
                }),
              ],
            },
          }),
        ],
      }),
    );
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    const hosting = await screen.findByRole("region", { name: en.caseFile.hosting.heading });
    const page = within(within(hosting).getByRole("region", { name: "video.example" }));
    expect(page.getByText(en.caseFile.hosting.cdn.replaceAll("{{cdn}}", "cloudflare"))).toBeTruthy();
    expect(page.getByText(en.caseFile.hosting.jurisdiction.us)).toBeTruthy();
    expect(page.getByText(en.caseFile.hosting.stale.replace("{{date}}", date("2026-09-15T06:00:00Z")))).toBeTruthy();

    expect(within(hosting).getByText(en.caseFile.hosting.contentHosts)).toBeTruthy();
    const preview = within(within(hosting).getByRole("region", { name: "cdn.example" }));
    expect(preview.getByText(en.caseFile.hosting.lookup.partial)).toBeTruthy();
    expect(preview.getByText(en.caseFile.hosting.jurisdiction.unknown)).toBeTruthy();
    expect(preview.getAllByText(en.caseFile.hosting.notKnown)).toHaveLength(3);
    const history = preview.getByText(en.caseFile.hosting.history.removed.replace("{{percent}}", "75"), { exact: false });
    expect(history.textContent).toBe(
      `${en.caseFile.hosting.history.removed.replace("{{percent}}", "75")} · ${en.caseFile.hosting.history.usually["3_plus_days"]}`,
    );
  });

  it("says when a host has not been looked up, and shows no empty facts", async () => {
    serveCase(
      caseDetail({
        findings: [
          finding({
            hosting: {
              page_host: host({
                lookup: "not_looked_up",
                looked_up_at: null,
                addresses: [],
                asn: null,
                as_name: null,
                provider: null,
                country: null,
                jurisdiction: "unknown",
                abuse_contact: null,
                registrar: null,
              }),
              content_hosts: [],
            },
          }),
        ],
      }),
    );
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    const hosting = await screen.findByRole("region", { name: en.caseFile.hosting.heading });
    expect(within(hosting).getByText(en.caseFile.hosting.lookup.not_looked_up)).toBeTruthy();
    expect(within(hosting).queryByText(en.caseFile.hosting.facts.provider)).toBeNull();
    expect(hosting.querySelector("details")).toBeNull();
  });

  it("shows the response history rounded and in the past tense, or that there is none yet", async () => {
    for (const [language, resources] of [
      ["en", en],
      ["de", de],
    ] as const) {
      await setLanguageForTest(language);
      const none = render(
        <I18nextProvider i18n={i18n}>
          <HostingSection finding={{ hosting: { page_host: host({ response_history: null }), content_hosts: [] } }} />
        </I18nextProvider>,
      );
      const empty = none.container.querySelector('[data-history="none"]');
      expect(empty?.textContent).toBe(resources.caseFile.hosting.history.none);
      expect(empty?.textContent).not.toMatch(/\d/);
      cleanup();

      for (const time of [...REMOVAL_TIMES, null]) {
        const { container } = render(
          <I18nextProvider i18n={i18n}>
            <HostingSection
              finding={{
                hosting: {
                  page_host: host({ response_history: { removed_percent: 35, usual_removal_time: time } }),
                  content_hosts: [],
                },
              }}
            />
          </I18nextProvider>,
        );
        const row = container.querySelector('[data-history="shown"]');
        const removed = resources.caseFile.hosting.history.removed.replace("{{percent}}", "35");
        expect(row?.textContent).toContain(removed);
        expect(row?.textContent).toContain(resources.caseFile.hosting.history.note);
        if (time) expect(row?.textContent).toContain(resources.caseFile.hosting.history.usually[time]);
        else expect(row?.textContent).not.toContain(" · ");
        cleanup();
      }
    }
  });

  it("offers prepare-notice as a real link and shows the rest as disabled affordances that say why", async () => {
    serveCase();
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    const options = await screen.findByRole("region", { name: en.caseFile.options.heading });
    // Prepare notice draft (issue #33) is offered now: a link into the notice screen, not a
    // disabled button with a "why not" reason.
    const prepare = within(options).getByRole("link", { name: en.caseFile.options.prepare_notice.label });
    expect(prepare.getAttribute("href")).toBe(`/app/originals/${originalId(7)}/notices`);
    expect(within(options).getByText(en.caseFile.options.delivery)).toBeTruthy();

    const buttons = within(options).getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual([
      en.caseFile.options.search_delisting.label,
      en.caseFile.options.escalation.label,
      en.caseFile.options.reupload_watch.label,
    ]);
    expect(buttons.every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
    // "Prepare", never "Send": acting on the customer's behalf is a legal question.
    expect(within(options).queryByRole("button", { name: /send|senden|verschick/i })).toBeNull();
    expect(within(options).queryByRole("link", { name: /send|senden|verschick/i })).toBeNull();
    expect(within(options).queryByText(en.caseFile.reasons.legal_review_pending)).toBeNull();
    // Options the API says nothing about are not offered — not faked as available.
    expect(within(options).getAllByText(en.caseFile.options.notOffered)).toHaveLength(2);
    // The escalation ladder is visible, and nothing on it has started.
    const ladder = within(options).getByRole("list", { name: en.caseFile.options.ladder.label });
    expect(within(ladder).getAllByRole("listitem")).toHaveLength(4);
    expect(within(ladder).getAllByText(en.caseFile.options.ladder.notStarted, { exact: false })).toHaveLength(4);
    expect(within(options).getByText(en.caseFile.options.coverage)).toBeTruthy();
  });

  it("renders a 404 for the original as not found", async () => {
    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(9)}`]: NOT_FOUND });
    renderAt(`/app/originals/${originalId(9)}/copies/${pageId(101)}`);

    expect(await screen.findByText(en.notFound.caseFile)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a copy that is not among the original's findings as not found", async () => {
    // Also how a safety-flagged page looks from here: the API never sends it, so there is nothing
    // to render and no other way to reach it.
    serveCase();
    const { container } = renderAt(`/app/originals/${originalId(7)}/copies/${pageId(999)}`);

    expect(await screen.findByText(en.notFound.caseFile)).toBeTruthy();
    expect(container.textContent).not.toContain("video.example");
  });

  it.each([
    [originalId(7), "x1"],
    [originalId(7), "101"], // the old integer URLs (ADR 0033)
    ["7", pageId(101)],
    ["7", "101"],
    [pageId(7), originalId(101)], // the kinds swapped
  ])("does not ask the API for /app/originals/%s/copies/%s", async (original, page) => {
    const requested = serveCustomer(fetchMock, {});
    renderAt(`/app/originals/${original}/copies/${page}`);

    expect(await screen.findByText(en.notFound.caseFile)).toBeTruthy();
    expect(requested.some((path) => path.startsWith("/api/v1/app/cases"))).toBe(false);
  });

  it("shows loading and error states", async () => {
    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: "pending" });
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);
    expect(await screen.findByText(en.common.loading)).toBeTruthy();
    cleanup();

    serveCustomer(fetchMock, { [`/api/v1/app/cases/${originalId(7)}`]: { status: 403, body: { detail: "Forbidden" } } });
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(en.common.loadFailed);
    expect(alert.textContent).not.toContain("Forbidden");
  });

  it("speaks German", async () => {
    serveCase();
    await setLanguageForTest("de");
    renderAt(`/app/originals/${originalId(7)}/copies/${pageId(101)}`);

    expect(await screen.findByRole("heading", { level: 1, name: de.caseFile.heading.replace("{{site}}", "video.example") })).toBeTruthy();
    for (const heading of [de.caseFile.where.heading, de.caseFile.timeline.heading, de.caseFile.why.heading, de.caseFile.hosting.heading, de.caseFile.options.heading]) {
      expect(screen.getByRole("heading", { name: heading })).toBeTruthy();
    }
    expect(screen.getByText(de.caseFile.timeline.steps.notice_prepared)).toBeTruthy();
    expect(screen.getByRole("link", { name: de.caseFile.options.prepare_notice.label })).toBeTruthy();
    expect(screen.getByRole("link", { name: de.caseFile.where.open })).toBeTruthy();
    expect(screen.getByText(date("2026-09-14T09:00:00Z", "de"))).toBeTruthy();
    expect(screen.getByText(de.finding.extent.full_copy.label)).toBeTruthy();
    // No English left behind in the German case file.
    expect(screen.queryByText(en.caseFile.options.heading)).toBeNull();
    expect(screen.queryByText(en.caseFile.timeline.status.unavailable)).toBeNull();
  });
});
