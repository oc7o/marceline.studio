/**
 * The frame's two promises, asserted on the rendered markup.
 *
 * 1. **No imagery**, said in words on every page (issue #53). `lib/privacy.test.ts` proves the app
 *    *cannot* load a remote image; this is the sentence that tells the customer so.
 * 2. **One way to reach a human**, on every page. There is no signup, no password reset (ADR 0023)
 *    and no screen that asks for a data export or a deletion, while eight strings in this app say
 *    "write to us" — so the address has to be in the frame, or those sentences are a dead end. It
 *    is on the signed-out shell too, because sign-in and the invite page are where somebody is
 *    most likely to be stuck.
 *
 * The address itself is a word (`shell.contactEmail`), identical in both languages, and it is the
 * address the published Impressum and Datenschutzerklärung already name — not a new one.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";

import { type Resource, translator } from "../lib/translate";
import type { Words } from "../lib/words";
import { Shell } from "./Shell";

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ refresh: () => undefined, replace: () => undefined }),
}));

const WORDS: Record<"en" | "de", Words> = {
  en: { t: translator(en as unknown as Resource, "en"), language: "en" },
  de: { t: translator(de as unknown as Resource, "de"), language: "de" },
};

function shell(language: "en" | "de", username: string | null): string {
  return renderToStaticMarkup(
    <Shell i18n={WORDS[language]} username={username}>
      content
    </Shell>,
  );
}

describe.each(["en", "de"] as const)("the shell in %s", (language) => {
  const words = WORDS[language];
  const address = words.t("shell.contactEmail");

  it.each([
    ["signed in", "someone@example.test"],
    ["signed out", null],
  ])("names one way to reach a human when %s", (_case, username) => {
    const html = shell(language, username as string | null);
    expect(html).toContain(words.t("shell.contact"));
    expect(html).toContain(`href="mailto:${address}"`);
    expect(html).toContain(address);
  });

  it("says in words that no copy of the material is ever shown", () => {
    expect(shell(language, "someone@example.test")).toContain(words.t("shell.noImagery"));
  });
});

describe("the contact address", () => {
  it("is the same address in both languages, and a plain address with no scheme of its own", () => {
    const address = WORDS.en.t("shell.contactEmail");
    expect(WORDS.de.t("shell.contactEmail")).toBe(address);
    expect(address).toMatch(/^[^\s@:]+@[^\s@:]+\.[a-z]{2,}$/);
  });
});
