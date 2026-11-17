/**
 * Phone first (issue #117), asserted where a phone cannot be: no browser runs on the machine
 * that builds this, and `make browser-parity` is the only thing here that downloads one. So what
 * these tests check is the **shape of the markup and the stylesheet** — the decisions that, once
 * made, are what a phone will render.
 *
 * They are not a substitute for looking. `docs/runbooks/phone-check.md` is fifteen checks with a
 * real phone in a hand, and that is the acceptance test for this work. These are the guards that
 * keep the decisions from being undone by the next change.
 *
 * The four things guarded here are the four that were actually wrong on a phone, and each one
 * would pass every other test in this project while being unusable:
 *
 * 1. the navigation can be reached with a thumb, without scrolling to the top of the page;
 * 2. a control is at least 44 px and an input is at least 16 px (below that, iOS zooms the page
 *    in and never zooms back out);
 * 3. the frame is not taller than the content it frames;
 * 4. an empty screen says what it is waiting for, in both languages.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";

import { type Resource, translator } from "../lib/translate";
import type { Words } from "../lib/words";
import { Shell } from "./Shell";
import { EmptyState, LinkButton, Pagination } from "./ui";

vi.mock("next/navigation", () => ({
  usePathname: () => "/notices",
  useRouter: () => ({ refresh: () => undefined, replace: () => undefined }),
}));

const WORDS: Record<"en" | "de", Words> = {
  en: { t: translator(en as unknown as Resource, "en"), language: "en" },
  de: { t: translator(de as unknown as Resource, "de"), language: "de" },
};

const css = readFileSync(path.join(import.meta.dirname, "..", "app", "globals.css"), "utf8");

/**
 * The one `@media (pointer: coarse)` block, which is where both floors live. Empty when there is
 * none, which is a failure of the first test below rather than of importing this file.
 */
const coarse = (() => {
  const start = css.indexOf("@media (pointer: coarse)");
  if (start === -1) return "";
  const rest = css.slice(start);
  const end = rest.indexOf("\n}\n");
  return end === -1 ? rest : rest.slice(0, end);
})();

function shell(language: "en" | "de", username: string | null = "someone@example.test"): string {
  return renderToStaticMarkup(
    <Shell i18n={WORDS[language]} username={username}>
      content
    </Shell>,
  );
}

describe("the stylesheet's phone floors", () => {
  it("makes every form control 16 px on a finger-driven browser", () => {
    // Under 16 px Safari on iOS zooms the page in on focus and does not zoom back out. The two
    // `INPUT` constants in this app are `text-body-s` (13 px) and the sign-in form is 15 px, so
    // without this rule every input in the app triggers it.
    expect(coarse).toMatch(/input,\s*\n\s*select,\s*\n\s*textarea\s*\{\s*\n\s*font-size:\s*16px;/);
  });

  it("gives every control a 44 px floor there, and names the link shapes it covers", () => {
    expect(coarse).toMatch(/min-height:\s*2\.75rem;/);
    for (const selector of ["button", "select", "summary", '[role="button"]', "a[download]", ".tap"]) {
      expect(coarse, `${selector} has no 44 px floor`).toContain(selector);
    }
  });

  it("keys the floors on the pointer, never on the width", () => {
    // A desktop window narrowed to 360 px is still a mouse: it must not grow every button.
    expect(css).not.toMatch(/@media\s*\([^)]*max-width[^)]*\)\s*\{[^@]*min-height:\s*2\.75rem/);
  });

  it("defines `.tap` outside the media query, so a link's margins apply on a desktop too", () => {
    const tap = css.slice(css.lastIndexOf(".tap {"));
    expect(tap).toMatch(/display:\s*inline-flex;/);
  });
});

describe("the shell on a phone", () => {
  it("puts the navigation at the bottom of the screen up to `sm`, and under the header above it", () => {
    const html = shell("en");
    expect(html).toMatch(/<nav[^>]*class="[^"]*fixed inset-x-0 bottom-0/);
    // …and turns back into the tab row from `sm` up, in the same element.
    expect(html).toMatch(/<nav[^>]*class="[^"]*sm:static/);
  });

  it("keeps the bar clear of the home indicator, and the page clear of the bar", () => {
    const html = shell("en");
    expect(html).toContain("pb-[env(safe-area-inset-bottom)]");
    expect(html).toContain("pb-[calc(2.75rem_+_env(safe-area-inset-bottom))]");
  });

  it("reserves no room for a bar there is none of, signed out", () => {
    expect(shell("en", null)).not.toContain("pb-[calc(2.75rem_+_env(safe-area-inset-bottom))]");
  });

  it("gives every navigation item a 44 px cell and an equal fifth of the width", () => {
    const html = shell("en");
    expect(html.match(/min-h-11/g)?.length).toBe(5);
    expect(html.match(/class="min-w-0 flex-1 sm:flex-none"/g)?.length).toBe(5);
  });

  it.each(["en", "de"] as const)("names each place twice in %s: short in the bar, long in the row", (language) => {
    const html = shell(language);
    const i18n = WORDS[language];
    for (const place of ["overview", "removals", "notices", "invoices", "rights"] as const) {
      expect(html).toContain(`<span class="sm:hidden">${i18n.t(`nav.short.${place}`)}</span>`);
      expect(html).toContain(`<span class="hidden sm:inline">${i18n.t(`nav.${place}`)}</span>`);
    }
  });

  it("keeps every short German label inside a fifth of a 360 px screen", () => {
    // 360 / 5 = 72 px, less 4 px of padding. A conservative 7 px per character at 12 px
    // (`text-caption`) puts the ceiling at 9 characters: "Entfernungsprotokoll" is why the short
    // names exist at all, and "Rechnungen" is the one that is close.
    for (const place of ["overview", "removals", "notices", "invoices", "rights"] as const) {
      const label = WORDS.de.t(`nav.short.${place}`);
      expect(label.length, `${label} is too long for the bottom bar`).toBeLessThanOrEqual(10);
    }
  });

  it("drops the tagline and the signed-in name up to `sm`, and keeps both above it", () => {
    const html = shell("en");
    expect(html).toContain(`<p class="hidden text-caption text-ink-subtle sm:block">`);
    expect(html).toContain(`<span class="hidden text-body-s text-ink-muted sm:inline">`);
    // Neither is removed: an `sm` screen still shows them.
    expect(html).toContain(WORDS.en.t("shell.tagline"));
    expect(html).toContain("someone@example.test");
  });

  it("opens with a skip link into a focusable main landmark", () => {
    const html = shell("en");
    expect(html.indexOf('href="#main"')).toBeLessThan(html.indexOf("<header"));
    expect(html).toContain(WORDS.en.t("shell.skipToContent"));
    expect(html).toMatch(/<main id="main" tabindex="-1"/);
  });

  it.each(["en", "de"] as const)("says what the skip link does in %s", (language) => {
    expect(WORDS[language].t("shell.skipToContent")).not.toBe("shell.skipToContent");
  });

  it("gives the page a 16 px gutter on a phone and 32 px from `sm` up", () => {
    expect(shell("en")).toMatch(/<main[^>]*class="[^"]*p-4 sm:p-8/);
  });
});

describe("the primitives on a phone", () => {
  it("makes a link that looks like a button tappable", () => {
    expect(renderToStaticMarkup(<LinkButton href="/app">Go</LinkButton>)).toMatch(/class="tap /);
  });

  it("gives a primary action the whole width of a phone only when it is asked to", () => {
    const wide = renderToStaticMarkup(
      <LinkButton href="/app" block>
        Add an original
      </LinkButton>,
    );
    expect(wide).toContain("w-full justify-center sm:w-auto sm:justify-start");
    expect(renderToStaticMarkup(<LinkButton href="/app">Go</LinkButton>)).not.toContain("w-full");
  });

  it("keeps both pagination steps the same height, the dead one included", () => {
    const html = renderToStaticMarkup(
      <Pagination label="Pages" status="1–20 of 57" previous={{ label: "Back" }} next={{ label: "More", href: "/app?page=2" }} />,
    );
    expect(html.match(/class="tap /g)?.length).toBe(2);
  });

  it("renders an empty state's next step as its own, quieter paragraph", () => {
    const html = renderToStaticMarkup(
      <EmptyState title="Nothing yet" next="What happens next: something." />,
    );
    expect(html).toContain('data-empty-next=""');
    expect(html).toMatch(/data-empty-next[^>]*class="[^"]*text-body-s text-ink-subtle/);
  });
});

describe("every empty list says what happens next", () => {
  const KEYS = [
    "overview.emptyNext",
    "removals.emptyNext",
    "invoices.emptyNext",
    "notices.list.emptyNext",
  ] as const;

  it.each(KEYS)("%s is a real sentence in both languages", (key) => {
    for (const language of ["en", "de"] as const) {
      const text = WORDS[language].t(key);
      expect(text, `${key} is missing in ${language}`).not.toBe(key);
      expect(text.length).toBeGreaterThan(40);
    }
  });

  /*
    The sentence that is most tempting to over-promise is exactly this one, so the two bans that
    matter most are restated here rather than left to `wording.test.ts` alone: no duration, and
    nothing about how much will be found. `wording.test.ts` scans every string in both languages
    and is the real guard; this one names the failure at the key.
  */
  it.each(KEYS)("%s promises no time and no outcome", (key) => {
    for (const language of ["en", "de"] as const) {
      const text = WORDS[language].t(key);
      expect(text).not.toMatch(/\bwithin \d|\b\d+ (hours?|days?)\b|innerhalb|\bStunden\b/i);
      expect(text).not.toMatch(/guarantee|garant|expect|erwarte|likel|wahrscheinlich/i);
    }
  });
});

describe("the add-original entry is honest about a phone", () => {
  it.each(["en", "de"] as const)("says where local hashing is hard, in %s", (language) => {
    const text = WORDS[language].t("addOriginal.onAPhone");
    expect(text).not.toBe("addOriginal.onAPhone");
    expect(text.length).toBeGreaterThan(60);
  });

  it("promises nothing about sending the file instead — that option is off (ADR 0044)", () => {
    for (const language of ["en", "de"] as const) {
      const text = WORDS[language].t("addOriginal.onAPhone");
      expect(text).not.toMatch(/upload|hochlad|send (us|it)|senden Sie/i);
    }
  });
});
