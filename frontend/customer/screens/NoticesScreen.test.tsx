/**
 * The account-wide notices screen (`/app/notices`), rendered to HTML the way Next first sends it
 * (ADR 0036): every draft across every original, its status, how many matching copies it names,
 * and its own links — into the original and into the record's drafts screen.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";

import { type Resource, translator } from "../lib/translate";
import type { Words } from "../lib/words";
import { notice, noticeFinding, noticeId, noticeSubmission, originalId } from "./fixtures";
import { NoticesScreen } from "./NoticesScreen";

const WORDS: Record<"en" | "de", Words> = {
  en: { t: translator(en as unknown as Resource, "en"), language: "en" },
  de: { t: translator(de as unknown as Resource, "de"), language: "de" },
};

describe("the account-wide notices list", () => {
  it.each(["en", "de"] as const)("lists every draft, newest first, in %s", (language) => {
    const i18n = WORDS[language];
    const notices = [
      notice({
        id: noticeId(2),
        original_id: originalId(7),
        status: "marked_sent_by_customer",
        marked_sent_at: "2026-09-16T09:00:00Z",
        findings: [
          noticeFinding({ included: true }),
          noticeFinding({ page_id: "page_" + "9".repeat(32), included: false }),
        ],
      }),
      notice({
        id: noticeId(1),
        original_id: originalId(8),
        work_title: "Second work",
        status: "draft",
        findings: [noticeFinding(), noticeFinding({ page_id: "page_" + "8".repeat(32) })],
      }),
    ];
    const html = renderToStaticMarkup(
      <NoticesScreen i18n={i18n} notices={notices} submissions={null} />,
    );

    expect(html).toContain(i18n.t("notices.list.heading"));
    expect(html).toContain(i18n.t(`notices.statuses.${"marked_sent_by_customer"}`));
    expect(html).toContain(i18n.t(`notices.statuses.${"draft"}`));
    // The included finding only (one of two on the first notice, both of two on the second).
    expect(html).toContain(i18n.t("notices.list.copies", { count: 1 }));
    expect(html).toContain(i18n.t("notices.list.copies", { count: 2 }));
    expect(html).toContain(i18n.t("notices.list.notSentYet"));
    // The title links to the original it belongs to; "open" links into the record's own drafts.
    expect(html).toContain(`href="/app/originals/${originalId(7)}"`);
    expect(html).toContain(`href="/app/originals/${originalId(7)}/notices"`);
    expect(html).toContain(`href="/app/originals/${originalId(8)}"`);
    expect(html).toContain(`href="/app/originals/${originalId(8)}/notices"`);
  });

  it("says so when there are none", () => {
    const html = renderToStaticMarkup(
      <NoticesScreen i18n={WORDS.en} notices={[]} submissions={null} />,
    );
    expect(html).toContain(WORDS.en.t("notices.list.empty"));
  });

  it("offers a retry when the read failed, without repeating a server error", () => {
    const html = renderToStaticMarkup(
      <NoticesScreen i18n={WORDS.en} notices={null} submissions={null} />,
    );
    expect(html).toContain(WORDS.en.t("common.loadFailed"));
    expect(html).toContain(WORDS.en.t("common.retry"));
  });

  it("uses tokens, never a literal Tailwind colour", () => {
    const LITERAL_COLOUR =
      /\b(?:bg|text|border|from|via|to)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b/;
    const html = renderToStaticMarkup(
      <NoticesScreen
        i18n={WORDS.en}
        notices={[notice(), notice({ id: noticeId(2), status: "provider_responded", outcome: "removed" }), notice({ id: noticeId(3), status: "provider_responded", outcome: "refused" })]}
        submissions={[noticeSubmission(), noticeSubmission({ id: "sub_" + "2".repeat(32), status: "refused", code: "finding_rights_blocked" })]}
      />,
    );
    expect(html).not.toMatch(LITERAL_COLOUR);
  });
});

describe("recent notice actions block (issue #86, ADR 0046 §7)", () => {
  it.each(["en", "de"] as const)("shows a pending write in %s", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <NoticesScreen i18n={i18n} notices={[]} submissions={[noticeSubmission({ status: "processing" })]} />,
    );
    expect(html).toContain(i18n.t("notices.submissions.heading"));
    expect(html).toContain(i18n.t("notices.submissions.kinds.notice_approve"));
    expect(html).toContain(i18n.t("notices.submissions.processing"));
    expect(html).toContain(i18n.t("notices.submissions.refreshHint"));
  });

  it.each(["en", "de"] as const)("shows a refusal's own sentence, never its code, in %s", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <NoticesScreen
        i18n={i18n}
        notices={[]}
        submissions={[noticeSubmission({ status: "refused", code: "finding_rights_blocked" })]}
      />,
    );
    expect(html).toContain(i18n.t("notices.errors.codes.finding_rights_blocked"));
    expect(html).not.toContain("finding_rights_blocked");
  });

  it.each(["en", "de"] as const)("a refusal with no words of its own reads the neutral line, in %s", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(
      <NoticesScreen
        i18n={i18n}
        notices={[]}
        submissions={[noticeSubmission({ status: "refused", code: "no_organisation" })]}
      />,
    );
    expect(html).toContain(i18n.t("notices.submissions.refusedOther"));
    expect(html).not.toContain("no_organisation");
  });

  it("a settled write needs no line: the block renders nothing once every submission is done", () => {
    const html = renderToStaticMarkup(
      <NoticesScreen
        i18n={WORDS.en}
        notices={[]}
        submissions={[noticeSubmission({ status: "done", code: "created" })]}
      />,
    );
    expect(html).not.toContain(WORDS.en.t("notices.submissions.heading"));
  });

  it("renders nothing at all when there are no submissions", () => {
    const html = renderToStaticMarkup(
      <NoticesScreen i18n={WORDS.en} notices={[]} submissions={[]} />,
    );
    expect(html).not.toContain(WORDS.en.t("notices.submissions.heading"));
  });
});
