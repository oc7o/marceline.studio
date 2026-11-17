/**
 * The nav's own reachability guarantee (ADR 0036).
 *
 * A route existing is not the same as something pointing at it: the whole notice-draft feature
 * was fully built and unreachable in this app for a while because only the *link* into it was
 * missing (`screens/CaseFileScreen.tsx`'s "prepare_notice" option had no href) — `routes.test.ts`
 * stayed green the entire time, because it only checks that a route's page file exists, never that
 * anything links to it. This renders the real `Shell`/`Nav` and follows the actual href, so the
 * account-wide notices list cannot go quietly unreachable the same way.
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";

import { type Resource, translator } from "../lib/translate";
import type { Words } from "../lib/words";
import { Shell } from "./Shell";

// Nav is a client component only because "you are here" needs the browser's own path, and the
// header's language switch needs the router to refresh after changing it; outside a real Next
// render there is no router to ask, so both are stubbed.
vi.mock("next/navigation", () => ({
  usePathname: () => "/notices",
  useRouter: () => ({ refresh: () => undefined, replace: () => undefined }),
}));

const WORDS: Record<"en" | "de", Words> = {
  en: { t: translator(en as unknown as Resource, "en"), language: "en" },
  de: { t: translator(de as unknown as Resource, "de"), language: "de" },
};

describe("the customer nav", () => {
  it.each(["en", "de"] as const)(
    "links the account-wide notices list, with its own label, in %s",
    (language) => {
      const i18n = WORDS[language];
      const html = renderToStaticMarkup(
        <Shell i18n={i18n} username="test-customer@marceline.studio">
          content
        </Shell>,
      );
      expect(html).toContain(`href="/notices"`);
      expect(html).toContain(i18n.t("nav.notices"));
    },
  );

  it("renders no nav at all when signed out, so nothing is reachable that way either", () => {
    const html = renderToStaticMarkup(
      <Shell i18n={WORDS.en} username={null}>
        content
      </Shell>,
    );
    expect(html).not.toContain(`href="/notices"`);
  });
});

describe("the download-my-data link (ADR 0045 amendment 2026-09-18)", () => {
  it.each(["en", "de"] as const)(
    "links to the export route, with its own label, in %s, once me.can_export_data says so",
    (language) => {
      const i18n = WORDS[language];
      const html = renderToStaticMarkup(
        <Shell i18n={i18n} username="test-customer@marceline.studio" canExportData>
          content
        </Shell>,
      );
      expect(html).toContain('href="/app/api/export"');
      expect(html).toContain(i18n.t("shell.downloadData"));
    },
  );

  it("renders no download link when the account may not export (the public host)", () => {
    const html = renderToStaticMarkup(
      <Shell i18n={WORDS.en} username="test-customer@marceline.studio" canExportData={false}>
        content
      </Shell>,
    );
    expect(html).not.toContain("/app/api/export");
  });

  it("defaults to no download link when the caller says nothing", () => {
    const html = renderToStaticMarkup(
      <Shell i18n={WORDS.en} username="test-customer@marceline.studio">
        content
      </Shell>,
    );
    expect(html).not.toContain("/app/api/export");
  });
});
