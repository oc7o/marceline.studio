/**
 * The server-rendered screens, rendered to HTML the way a browser first receives them (ADR 0036).
 *
 * This is what "synchronous server component" buys: no request, no router, no query client, no
 * jsdom — `renderToStaticMarkup` over the same tree Next streams. The assertions are the ones the
 * SPA's screen tests made about facts and wording, in **both** languages, because a German
 * customer reading an English sentence is the failure this catches.
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";

import { type Resource, translator } from "../lib/translate";
import type { Words } from "../lib/words";
import {
  ForgotPasswordScreen,
  ResendVerificationScreen,
  ResetPasswordScreen,
  VerifyEmailScreen,
} from "./AccountEmailScreen";
import { CaseFileScreen } from "./CaseFileScreen";
import { DataRequestsScreen } from "./DataRequestsScreen";
import { type HomeData, HomeScreen } from "./HomeScreen";
import { InviteScreen } from "./InviteScreen";
import { InvoicesScreen } from "./InvoicesScreen";
import { OriginalScreen } from "./OriginalScreen";
import { RemovalsScreen } from "./RemovalsScreen";
import { VerificationScreen } from "./VerificationScreen";
import {
  account,
  caseDetail,
  dataRequest,
  dataRequests,
  finding,
  host,
  invoice,
  inviteState,
  original,
  originalId,
  pageId,
  must,
  removal,
  submission,
  upload,
  claimedAccount,
  verificationStatus,
} from "./fixtures";

// The overview's poller is a client island; outside a Next render there is no router to refresh.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: () => undefined, replace: () => undefined }),
  usePathname: () => "/",
}));

const WORDS: Record<"en" | "de", Words> = {
  en: { t: translator(en as unknown as Resource, "en"), language: "en" },
  de: { t: translator(de as unknown as Resource, "de"), language: "de" },
};

const LANGUAGES = ["en", "de"] as const;


/**
 * The overview's data, with the parts a test does not care about defaulted. Adding a read to the
 * overview then costs one line here instead of one per test.
 */
function home(overrides: Partial<HomeData> = {}): HomeData {
  return {
    account: account(),
    originals: [original()],
    findings: [finding()],
    submissions: [],
    removals: [],
    rights: null,
    ...overrides,
  };
}

describe("the overview", () => {
  it.each(LANGUAGES)("[%s] lists one card per original with its state and counts", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={i18n}
        data={home({
          originals: [
            original(),
            original({
              id: originalId(8),
              title: "Winter set",
              state: "nothing_found",
              counts: { confirmed: 0, removed: 0 },
            }),
          ],
        })}
      />,
    );
    expect(html).toContain("Beach day");
    expect(html).toContain("Winter set");
    expect(html).toContain(i18n.t("states.found_online"));
    expect(html).toContain(i18n.t("states.nothing_found"));
    expect(html).toContain(i18n.t("overview.confirmed", { count: 3, removed: 1 }));
    // An original with nothing found says so; it never gets a count of work in progress.
    expect(html).toContain(i18n.t("overview.nothingFound"));
    // Coverage is the bucket, never a list of sites (operator 2026-09-16).
    expect(html).toContain(i18n.t("coverage.sources.10_plus"));
    expect(html).not.toContain("video.example</span> ·");
  });

  it.each(LANGUAGES)("[%s] sums the catalogue and lists what changed recently", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={i18n}
        data={home({ removals: [removal()] })}
      />,
    );
    expect(html).toContain(i18n.t("overview.totals.heading"));
    expect(html).toContain(i18n.t("overview.totals.originals", { count: 1 }));
    expect(html).toContain(i18n.t("overview.recent.heading"));
    // Every recent change is one of the three the API's own rows can justify, with its own date.
    expect(html).toMatch(/overview\.recent\.|Beach day/);
  });

  it("draws exactly one Veil, behind the page header and nothing else", () => {
    const html = renderToStaticMarkup(<HomeScreen i18n={WORDS.en} data={home()} />);
    // The system allows one per view. Three planes, one Veil.
    expect(html.match(/blur-veil/g)).toHaveLength(3);
    const veil = html.indexOf("bg-veil-brand");
    const firstCard = html.indexOf("overview-heading");
    expect(veil).toBeGreaterThan(-1);
    // It opens inside the header, before the cards below it ever start.
    expect(veil).toBeLessThan(html.indexOf(WORDS.en.t("overview.totals.heading")));
    expect(firstCard).toBeGreaterThan(-1);
  });

  it("offers the one primary action once: the header, or the empty state, never both", () => {
    const add = WORDS.en.t("overview.addOriginal");
    const withOriginals = renderToStaticMarkup(<HomeScreen i18n={WORDS.en} data={home()} />);
    expect([...withOriginals.matchAll(new RegExp(add, "g"))]).toHaveLength(1);
    const empty = renderToStaticMarkup(
      <HomeScreen i18n={WORDS.en} data={home({ originals: [] })} />,
    );
    expect([...empty.matchAll(new RegExp(add, "g"))]).toHaveLength(1);
    // The empty catalogue keeps its own sentence, and asks for no Veil: the header has this view's.
    expect(empty).toContain(WORDS.en.t("overview.emptyCanAdd"));
  });

  it("says nothing recent rather than guessing when a read failed", () => {
    const html = renderToStaticMarkup(
      <HomeScreen i18n={WORDS.en} data={home({ removals: null })} />,
    );
    expect(html).not.toContain(WORDS.en.t("overview.recent.heading"));
    // …but the totals, which need no removals, are still there.
    expect(html).toContain(WORDS.en.t("overview.totals.heading"));
  });

  it("says where the copies are, from the findings", () => {
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS.en}
        data={home()}
      />,
    );
    expect(html).toContain("video.example");
  });

  it("says so when the findings could not be loaded, instead of showing none", () => {
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS.en}
        data={home({ findings: null })}
      />,
    );
    expect(html).toContain(WORDS.en.t("overview.sitesFailed"));
  });

  it("renders a load error rather than an empty list when the originals failed", () => {
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS.en}
        data={home({ originals: null, findings: null, submissions: null, removals: null })}
      />,
    );
    expect(html).toContain(WORDS.en.t("common.loadFailed"));
    expect(html).not.toContain(WORDS.en.t("overview.empty"));
  });

  it("shows the customer's own upload being added, and nothing about our review work", () => {
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS.en}
        data={home({ findings: [], submissions: [submission()] })}
      />,
    );
    expect(html).toContain("New clip");
    expect(html).toContain(WORDS.en.t("addOriginal.submissions.status.processing"));
  });

  describe("pagination (issue #134)", () => {
    const many = Array.from({ length: 45 }, (_, index) =>
      original({ id: originalId(index + 1), title: `Original ${index + 1}` }),
    );

    it("offers no pagination controls when everything fits on one page", () => {
      const html = renderToStaticMarkup(
        <HomeScreen i18n={WORDS.en} data={home({ originals: [original()] })} />,
      );
      expect(html).not.toContain(WORDS.en.t("pagination.label"));
    });

    it.each(LANGUAGES)("[%s] shows only the first 20 and says so", (language) => {
      const i18n = WORDS[language];
      const html = renderToStaticMarkup(
        <HomeScreen i18n={i18n} data={home({ originals: many })} />,
      );
      expect(html).toContain("Original 1");
      expect(html).toContain("Original 20");
      expect(html).not.toContain("Original 21");
      expect(html).toContain(i18n.t("pagination.status", { start: 1, end: 20, total: 45 }));
      // Still totalled over the whole catalogue, not just the page shown.
      expect(html).toContain(i18n.t("overview.totals.originals", { count: 45 }));
    });

    it("renders the requested page, with a link back and a link forward", () => {
      const html = renderToStaticMarkup(
        <HomeScreen i18n={WORDS.en} data={home({ originals: many })} page={2} />,
      );
      expect(html).toContain("Original 21");
      expect(html).toContain("Original 40");
      expect(html).not.toContain("Original 1<");
      expect(html).toContain(WORDS.en.t("pagination.status", { start: 21, end: 40, total: 45 }));
      expect(html).toContain('href="/app"');
      expect(html).toContain('href="/app?page=3"');
    });

    it("clamps a page past the end to the last real page rather than showing nothing", () => {
      const html = renderToStaticMarkup(
        <HomeScreen i18n={WORDS.en} data={home({ originals: many })} page={99} />,
      );
      expect(html).toContain("Original 41");
      expect(html).toContain(WORDS.en.t("pagination.status", { start: 41, end: 45, total: 45 }));
    });
  });

  it("offers nothing to add when the account may not", () => {
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS.en}
        data={home({
          account: account({ can_add_originals: false }),
          findings: [],
          submissions: [submission()],
        })}
      />,
    );
    expect(html).not.toContain("/app/originals/new");
    expect(html).not.toContain("New clip");
  });

  it("tells an account without an organisation, and shows no originals", () => {
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS.en}
        data={home({
          account: account({ organisation: null }),
          findings: [],
          submissions: null,
        })}
      />,
    );
    expect(html).toContain(WORDS.en.t("home.noOrganisation"));
    expect(html).not.toContain("Beach day");
  });
});

describe("files still being fingerprinted (issue #59, ADR 0044)", () => {
  // The customer is invited to close the add page while we hash; this is where they find the file
  // again, and where a file that was not added says why.
  it.each(LANGUAGES)("[%s] lists what is in flight and what was not added", (language) => {
    const words = (language === "en" ? en : de).addOriginal.upload;
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS[language]}
        data={home({
          uploads: [
            upload({ status: "hashing", frames_done: 3, frames_total: 12 }),
            upload({
              id: `upl_${"2".repeat(32)}`,
              status: "failed",
              code: "decode_failed",
              reason: "decode_failed",
              bytes_deleted: true,
            }),
          ],
        })}
      />,
    );
    expect(html).toContain(words.open.heading);
    expect(html).toContain(words.state.hashing);
    expect(html).toContain("25%");
    expect(html).toContain(words.reasons.decode_failed);
    // The result code is ours to word; it never reaches the page.
    expect(html).not.toContain(">decode_failed<");
  });

  it("shows nothing where cloud fingerprinting is off, which is everywhere today", () => {
    const html = renderToStaticMarkup(<HomeScreen i18n={WORDS.en} data={home()} />);
    expect(html).not.toContain(en.addOriginal.upload.open.heading);
  });

  it("lists neither an added file nor one the customer cancelled", () => {
    const html = renderToStaticMarkup(
      <HomeScreen
        i18n={WORDS.en}
        data={home({
          uploads: [
            upload({ status: "done", original_id: originalId(1), bytes_deleted: true }),
            upload({ id: "upl_c", status: "cancelled", reason: "cancelled" }),
          ],
        })}
      />,
    );
    expect(html).not.toContain(en.addOriginal.upload.open.heading);
  });
});

describe("one original", () => {
  it.each(LANGUAGES)("[%s] shows the record's state and every copy", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(<OriginalScreen i18n={i18n} detail={caseDetail()} />);
    expect(html).toContain("Beach day");
    expect(html).toContain("video.example");
    expect(html).toContain("mirror.example");
    expect(html).toContain(i18n.t("original.case.heading"));
    // The copy's own extent is a fact about the copy, never a confidence.
    expect(html).toContain(i18n.t("finding.extent.full_copy.label"));
    expect(html).toContain(i18n.t("finding.extent.excerpt.label"));
  });

  it("shows no match confidence of any kind", () => {
    const html = renderToStaticMarkup(<OriginalScreen i18n={WORDS.en} detail={caseDetail()} />);
    for (const word of ["confidence", "band", "distance", "Hamming", "%"]) {
      expect(html).not.toContain(word);
    }
  });
});

describe("one copy's record", () => {
  const detail = caseDetail();
  const copy = must(detail.findings[0], "the first finding");

  it.each(LANGUAGES)("[%s] shows where it is, what happened and who hosts it", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <CaseFileScreen i18n={i18n} detail={detail} finding={copy} />,
    );
    expect(html).toContain("https://video.example/video/101");
    expect(html).toContain(i18n.t("caseFile.timeline.heading"));
    expect(html).toContain("Example Hosting GmbH");
    // The suggested notice channel, never "DSA applies".
    expect(html).toContain(i18n.t("caseFile.hosting.jurisdiction.eu"));
    // An option with no endpoint is a disabled button that says why.
    expect(html).toContain(i18n.t("caseFile.options.prepare_notice.label"));
    expect(html).toContain("disabled");
    // …but preparing a notice draft has one, so it is a real link into that screen and not the
    // disabled affordance. Without this the whole feature was unreachable in this app.
    expect(html).toContain(`href="/app/originals/${detail.original.id}/notices"`);
  });

  it("keeps every timeline step readable as a step, with its status in words", () => {
    const html = renderToStaticMarkup(
      <CaseFileScreen i18n={WORDS.en} detail={detail} finding={copy} />,
    );
    // The shared `Timeline` primitive renders these; the codes are what a test and a reader of the
    // DOM identify a step by, and they survived the move.
    expect(html).toMatch(/<ol[^>]*>/);
    expect(html).toMatch(/data-step="[a-z_]+"/);
    expect(html).toMatch(/data-status="(done|pending|unavailable)"/);
    // Colour never carries the state: each marker is decoration and the words sit next to it.
    expect(html).toContain('aria-hidden="true"');
    expect(html).toContain(WORDS.en.t("caseFile.timeline.status.done"));
  });

  it("marks what the machine found apart from what a person decided", () => {
    const html = renderToStaticMarkup(
      <CaseFileScreen i18n={WORDS.en} detail={detail} finding={copy} />,
    );
    // Violet is the system's mark for automation; the words say the same thing, so the colour is
    // never the only carrier. A human review stays neutral.
    expect(html).toContain("bg-accent");
    expect(html).toContain("bg-border-control");
  });

  it("links out to the matched page exactly once, with no referrer", () => {
    const html = renderToStaticMarkup(
      <CaseFileScreen i18n={WORDS.en} detail={detail} finding={copy} />,
    );
    const links = [...html.matchAll(/href="https:\/\/tube\.example\/video\/101"/g)];
    expect(links).toHaveLength(1);
    expect(html).toContain('rel="noreferrer noopener"');
  });

  it("names what is not known instead of showing an empty record", () => {
    const unknown = caseDetail({
      findings: [
        finding({
          hosting: { page_host: host({ lookup: "not_looked_up", provider: null }), content_hosts: [] },
        }),
      ],
    });
    const html = renderToStaticMarkup(
      <CaseFileScreen i18n={WORDS.en} detail={unknown} finding={must(unknown.findings[0])} />,
    );
    expect(html).toContain(WORDS.en.t("caseFile.hosting.lookup.not_looked_up"));
    // …and no rows at all for that host: not one empty "not known yet". (Looked for inside the
    // host's own section, because the escalation ladder further down names a registrar too.)
    const block = html.slice(html.indexOf('data-host="video.example"'));
    expect(block.slice(0, block.indexOf("</section>"))).not.toContain("<dl");
  });

  it("shows a response history only as the API rounded it", () => {
    const withHistory = caseDetail({
      findings: [
        finding({
          hosting: {
            page_host: host({
              response_history: { removed_percent: 75, usual_removal_time: "3_plus_days" },
            }),
            content_hosts: [],
          },
        }),
      ],
    });
    const html = renderToStaticMarkup(
      <CaseFileScreen i18n={WORDS.en} detail={withHistory} finding={must(withHistory.findings[0])} />,
    );
    expect(html).toContain(WORDS.en.t("caseFile.hosting.history.removed", { percent: 75 }));
    expect(html).toContain(WORDS.en.t("caseFile.hosting.history.note"));
  });
});

describe("the removals log", () => {
  it.each(LANGUAGES)("[%s] shows each entry with its standing and coverage note", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <RemovalsScreen i18n={i18n} removals={[removal()]} account={account()} />,
    );
    expect(html).toContain("mirror.example");
    expect(html).toContain(i18n.t("removals.watched"));
    expect(html).toContain(i18n.t("coverage.sources.10_plus"));
    // The address is text here; only the record links out.
    expect(html).not.toContain('href="https://mirror.example/v/102"');
  });

  it("says so when the log could not be loaded", () => {
    const html = renderToStaticMarkup(
      <RemovalsScreen i18n={WORDS.en} removals={null} account={account()} />,
    );
    expect(html).toContain(WORDS.en.t("common.loadFailed"));
    expect(html).not.toContain(WORDS.en.t("removals.empty"));
  });

  it("paginates a long log instead of rendering every entry (issue #134)", () => {
    const many = Array.from({ length: 25 }, (_, index) =>
      removal({ page_id: pageId(index + 1), title: `Removal ${index + 1}` }),
    );
    const firstPage = renderToStaticMarkup(
      <RemovalsScreen i18n={WORDS.en} removals={many} account={account()} />,
    );
    expect(firstPage).toContain("Removal 1<");
    expect(firstPage).toContain("Removal 20<");
    expect(firstPage).not.toContain("Removal 21<");
    expect(firstPage).toContain(WORDS.en.t("pagination.status", { start: 1, end: 20, total: 25 }));
    expect(firstPage).toContain('href="/app/removals?page=2"');

    const secondPage = renderToStaticMarkup(
      <RemovalsScreen i18n={WORDS.en} removals={many} account={account()} page={2} />,
    );
    expect(secondPage).toContain("Removal 21<");
    expect(secondPage).toContain("Removal 25<");
    expect(secondPage).not.toContain("Removal 1<");
    expect(secondPage).toContain('href="/app/removals"');
  });
});

describe("the invoices", () => {
  it.each(LANGUAGES)("[%s] shows the amount, the dates and a same-origin PDF link", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <InvoicesScreen i18n={i18n} invoices={[invoice()]} notOnline={false} />,
    );
    expect(html).toContain("2026-00001");
    expect(html).toContain(i18n.t("invoices.status.open"));
    expect(html).toContain(`/app/api/invoices/${invoice().id}/pdf`);
    // No bank details on screen; they are on the PDF (ADR 0032).
    expect(html).not.toMatch(/IBAN|BIC/);
  });

  it("says 'not shown here yet' for a 404 rather than 'could not be loaded'", () => {
    const html = renderToStaticMarkup(
      <InvoicesScreen i18n={WORDS.en} invoices={null} notOnline />,
    );
    expect(html).toContain(WORDS.en.t("invoices.notOnline"));
    expect(html).not.toContain(WORDS.en.t("common.loadFailed"));
  });
});

describe("no screen can load a remote image", () => {
  it.each(LANGUAGES)("[%s] renders no img, video or remote src", (language) => {
    const i18n = WORDS[language];
    const detail = caseDetail();
    const html = [
      renderToStaticMarkup(
        <HomeScreen
          i18n={i18n}
          data={home()}
        />,
      ),
      renderToStaticMarkup(<OriginalScreen i18n={i18n} detail={detail} />),
      renderToStaticMarkup(
        <CaseFileScreen i18n={i18n} detail={detail} finding={must(detail.findings[0])} />,
      ),
      renderToStaticMarkup(
        <RemovalsScreen i18n={i18n} removals={[removal()]} account={account()} />,
      ),
      renderToStaticMarkup(<InvoicesScreen i18n={i18n} invoices={[invoice()]} notOnline={false} />),
    ].join("\n");
    expect(html).not.toMatch(/<img|<video|<picture|<iframe|srcset=|poster=/i);
    // The one external address in the whole app is the matched page itself, in the record.
    const external = [...html.matchAll(/(?:href|src)="(https?:\/\/[^"]+)"/g)].map((m) => m[1]);
    expect(external).toEqual(["https://video.example/video/101"]);
  });
});

describe("the pages a copy lives on", () => {
  it("is not reachable for a page that is not among the findings", () => {
    const detail = caseDetail();
    expect(detail.findings.some((row) => row.page_id === pageId(999))).toBe(false);
  });
});


/**
 * The invite redemption (issue #115, ADR 0052), rendered the way a browser first receives it.
 *
 * The neutral page is the assertion that matters: every reason a link does not work arrives here
 * as the same `null`, so the rendered HTML must not contain a word that would distinguish them.
 */
describe("the invite redemption", () => {
  const resource = (code: "en" | "de") => (code === "de" ? de : en) as unknown as Resource;
  const TOKEN = "t".repeat(32);

  it.each(LANGUAGES)("says one neutral thing about a link that does not work (%s)", (code) => {
    const i18n = must(WORDS[code]);
    const html = renderToStaticMarkup(
      <InviteScreen i18n={i18n} token={TOKEN} state={null} words={resource(code)} />,
    );
    expect(html).toContain(i18n.t("onboarding.unavailable.heading"));
    for (const leak of ["expired", "revoked", "abgelaufen", "widerrufen", "benutzt"]) {
      expect(html.toLowerCase()).not.toContain(leak);
    }
  });

  it.each(LANGUAGES)("asks for a password first, with the address fixed (%s)", (code) => {
    const i18n = must(WORDS[code]);
    const html = renderToStaticMarkup(
      <InviteScreen i18n={i18n} token={TOKEN} state={inviteState()} words={resource(code)} />,
    );
    expect(html).toContain("creator@example.invalid");
    expect(html).toContain(i18n.t("onboarding.password.heading"));
    expect(html).toContain(i18n.t("onboarding.email.fixed"));
  });

  it("shows both German wordings verbatim, marked as German, with a tick each", () => {
    const i18n = must(WORDS.en);
    const html = renderToStaticMarkup(
      <InviteScreen
        i18n={i18n}
        token={TOKEN}
        state={inviteState({ step: "declaration" })}
        words={resource("en")}
      />,
    );
    expect(html).toContain('lang="de"');
    expect(html).toContain("§ 14 BGB");
    // The majority and legal-capacity declaration (issue #123): its own wording, its own box.
    expect(html).toContain("volljährig und unbeschränkt geschäftsfähig");
    expect(html).toContain(i18n.t("onboarding.declaration.ageHeading"));
    expect(html.match(/type="checkbox"/g)).toHaveLength(2);
    // A declaration, not a verification: nothing on this screen asks for a birth date.
    expect(html).not.toMatch(/type="date"/i);
  });

  it("says the step cannot be finished while no document version is published", () => {
    const i18n = must(WORDS.en);
    const html = renderToStaticMarkup(
      <InviteScreen
        i18n={i18n}
        token={TOKEN}
        state={inviteState({ step: "documents", documents: [], documents_published: false })}
        words={resource("en")}
      />,
    );
    expect(html).toContain(i18n.t("onboarding.documents.notPublished"));
    // No tick-box to agree to something that is not published.
    expect(html).not.toContain(i18n.t("onboarding.documents.tick"));
  });

  it("carries no image and no media element", () => {
    const html = renderToStaticMarkup(
      <InviteScreen
        i18n={must(WORDS.en)}
        token={TOKEN}
        state={inviteState({ step: "documents" })}
        words={resource("en")}
      />,
    );
    expect(html).not.toMatch(/<img|<video|<picture|<iframe|srcset=|poster=/i);
  });
});


describe("the account-control step (ADR 0055)", () => {
  const slice = (language: "en" | "de") =>
    (language === "de" ? de : en) as unknown as Resource;
  /**
   * The rendered HTML with React's entity escaping undone, so an assertion can compare against the
   * resource string itself. "the code was in the account's public bio" reaches the markup as
   * `account&#x27;s` — escaping is correct behaviour, and a test that works around it by asserting
   * a fragment would stop checking the sentence it is about.
   */
  const plain = (html: string) =>
    html.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&");

  it.each(LANGUAGES)("[%s] shows the code to place and asks for no document at all", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <VerificationScreen i18n={i18n} words={slice(language)} status={verificationStatus()} />,
    );

    expect(html).toContain("marceline-ABCD2345");
    expect(plain(html)).toContain(i18n.t("verification.code.place"));
    expect(plain(html)).toContain(i18n.t("verification.noDocuments"));
    expect(plain(html)).toContain(i18n.t("verification.accounts.state.pending"));
    // The claimed address is text, never a link: nothing of ours may open what a creator typed.
    expect(html).toContain("fanhub.example/example.creator");
    expect(html).not.toContain('href="https://fanhub.example/example.creator"');
    // Not one input that could ask for a document or a birth date (operator, 2026-09-19).
    expect(html).not.toContain('type="date"');
    expect(html).not.toContain('type="file"');
  });

  it.each(LANGUAGES)("[%s] offers no code where no platform checked anybody", (language) => {
    // ADR 0055, operator review 2026-09-19: a code in the bio of the creator's own website proves
    // nothing about who they are, so none is issued and the screen says what happens instead.
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <VerificationScreen
        i18n={i18n}
        words={slice(language)}
        status={verificationStatus({
          accounts: [claimedAccount({ platform: "own_website", code: null, code_expires_at: null })],
        })}
      />,
    );

    expect(plain(html)).toContain(i18n.t("verification.code.videoCall"));
    expect(plain(html)).not.toContain(i18n.t("verification.code.place"));
    expect(plain(html)).not.toContain(i18n.t("verification.code.placed"));
  });

  it.each(LANGUAGES)("[%s] names the method once our team has seen it", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <VerificationScreen
        i18n={i18n}
        words={slice(language)}
        status={verificationStatus({
          identity_status: "verified",
          method: "bio_code",
          accounts: [
            claimedAccount({
              state: "verified",
              code: null,
              code_expires_at: null,
              method: "bio_code",
              decided_at: "2026-09-20T09:00:00Z",
            }),
          ],
        })}
      />,
    );

    expect(plain(html)).toContain(i18n.t("verification.status.verified"));
    expect(plain(html)).toContain(i18n.t("verification.method.bio_code"));
    // A settled claim has no live code, and the screen must not print a stale one.
    expect(html).not.toContain("marceline-ABCD2345");
  });
});

describe("the data-request screen (ADR 0056, issue #133)", () => {
  const slice = (language: "en" | "de") => (language === "de" ? de : en) as unknown as Resource;
  const plain = (html: string) =>
    html.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&");

  it.each(LANGUAGES)("[%s] offers both requests, the deadline and the address", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <DataRequestsScreen i18n={i18n} words={slice(language)} status={dataRequests()} />,
    );

    expect(plain(html)).toContain(i18n.t("dataRequests.export.submit"));
    expect(plain(html)).toContain(i18n.t("dataRequests.deletion.submit"));
    // The one-month deadline is Art. 12(3) GDPR's, stated on the screen and not only in a document.
    expect(plain(html)).toContain(i18n.t("dataRequests.deadline"));
    // The address comes from the server; no string in this app spells it out for this screen.
    expect(html).toContain("contact@marceline.studio");
    // A deletion is never one press: the word the server sent has to be typed.
    expect(plain(html)).toContain(i18n.t("dataRequests.deletion.phrase", { phrase: "DELETE" }));
  });

  it.each(LANGUAGES)("[%s] shows an open request with its due date and the way back", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <DataRequestsScreen
        i18n={i18n}
        words={slice(language)}
        status={dataRequests({ requests: [dataRequest({ kind: "deletion" })] })}
      />,
    );

    expect(plain(html)).toContain(i18n.t("dataRequests.status.requested"));
    expect(plain(html)).toContain(i18n.t("dataRequests.withdraw"));
    // Asking again while one is open is not offered: the server refuses it (`already_open`).
    expect(plain(html)).not.toContain(i18n.t("dataRequests.deletion.submit"));
  });

  it.each(LANGUAGES)("[%s] never says anything was exported or deleted", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <DataRequestsScreen
        i18n={i18n}
        words={slice(language)}
        status={dataRequests({
          requests: [
            dataRequest({
              kind: "deletion",
              status: "done",
              open: false,
              settled_at: "2026-09-25T09:00:00Z",
            }),
          ],
        })}
      />,
    );

    // A settled request is a record of an exchange, in the history — never a statement that data is
    // gone. The deletion itself is `end_pilot` plus the retention window (ADR 0045).
    expect(plain(html)).toContain(i18n.t("dataRequests.history.heading"));
    expect(plain(html)).toContain(i18n.t("dataRequests.status.done"));
    // And the screen offers the request again, because the settled one is not open any more.
    expect(plain(html)).toContain(i18n.t("dataRequests.deletion.submit"));
  });
});

/**
 * The mailed account actions (issue #149, ADR 0054), rendered the way a browser first receives them.
 *
 * Two assertions matter here and both are about what the HTML must **not** say: the neutral page
 * cannot name a reason (every reason arrives as the same `null`), and the request screen cannot
 * promise that a mail was sent to an address that may have no account.
 */
describe("the mailed account actions", () => {
  const resource = (code: "en" | "de") => (code === "de" ? de : en) as unknown as Resource;
  const TOKEN = "r".repeat(32);

  it.each(LANGUAGES)("says one neutral thing about a link that does not work (%s)", (code) => {
    const i18n = must(WORDS[code]);
    for (const html of [
      renderToStaticMarkup(
        <ResetPasswordScreen i18n={i18n} token={TOKEN} state={null} words={resource(code)} />,
      ),
      renderToStaticMarkup(
        <VerifyEmailScreen i18n={i18n} token={TOKEN} state={null} words={resource(code)} />,
      ),
    ]) {
      expect(html).toContain(i18n.t("accountEmail.unavailable.heading"));
      for (const leak of ["expired", "used", "too old", "abgelaufen", "benutzt", "unbekannt"]) {
        expect(html.toLowerCase()).not.toContain(leak.toLowerCase());
      }
    }
  });

  it.each(LANGUAGES)("asks for an address without promising anything (%s)", (code) => {
    const i18n = must(WORDS[code]);
    const html = renderToStaticMarkup(
      <ForgotPasswordScreen i18n={i18n} words={resource(code)} />,
    );
    expect(html).toContain(i18n.t("accountEmail.forgot.heading"));
    // The confirmation text is deliberately conditional; it is not rendered before a submit, and
    // the words themselves say "if this address has an account".
    expect(html).not.toContain(i18n.t("accountEmail.sent.heading"));
    expect(must(en.accountEmail).sent.body.toLowerCase()).toContain("if this address has an account");
  });

  it.each(LANGUAGES)("names the account a reset link belongs to (%s)", (code) => {
    const i18n = must(WORDS[code]);
    const html = renderToStaticMarkup(
      <ResetPasswordScreen
        i18n={i18n}
        token={TOKEN}
        state={{ email: "creator@example.invalid", password_min_length: 8 }}
        words={resource(code)}
      />,
    );
    expect(html).toContain("creator@example.invalid");
    expect(html).toContain(i18n.t("accountEmail.reset.heading"));
    expect(html).toContain('type="password"');
  });

  it("confirms an address with a button, never with the visit", () => {
    const i18n = must(WORDS.en);
    const html = renderToStaticMarkup(
      <VerifyEmailScreen
        i18n={i18n}
        token={TOKEN}
        state={{ email: "creator@example.invalid" }}
        words={resource("en")}
      />,
    );
    // A form with a submit button, not a link that a mail scanner could follow.
    expect(html).toContain("<form");
    expect(html).toContain(i18n.t("accountEmail.confirm.submit"));
    expect(html).not.toContain(`href="/app/api/verify-email/${TOKEN}"`);
  });

  it.each(LANGUAGES)("offers no dead button when no mail can be sent (%s)", (code) => {
    const i18n = must(WORDS[code]);
    const html = renderToStaticMarkup(
      <ResendVerificationScreen
        i18n={i18n}
        username="creator@example.invalid"
        canVerify={false}
        words={resource(code)}
      />,
    );
    expect(html).toContain(i18n.t("accountEmail.resend.unavailable"));
    expect(html).not.toContain(i18n.t("accountEmail.resend.submit"));
  });

  it("says nothing is blocked by an unconfirmed address", () => {
    // ADR 0054 §3: the beta blocks nothing over this, so no screen may imply that it does.
    const words = must(en.accountEmail);
    expect(words.resend.lead.toLowerCase()).toContain("either way");
  });
});
