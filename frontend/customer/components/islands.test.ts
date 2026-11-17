/**
 * The inventory of client components — the modules that carry `"use client"` and therefore ship
 * JavaScript to a browser (ADR 0036, "server components by default").
 *
 * Every other guard in this project measures the **consequence**: `scripts/check-bundle-budget.mjs`
 * fails when a route's own chunks pass 16 KB gzip. That is the right guard for size and the wrong
 * one for design — an island of 300 bytes passes it comfortably and still turns a screen that the
 * server could render into one that needs React to have loaded before it does anything. On a
 * phone on mobile data that is the difference the customer feels, and it is invisible in a diff
 * that says "+1 file".
 *
 * So this is the list, by name, of the modules allowed to be islands, with the reason each one
 * cannot be a server component. Adding a file here is not forbidden — it is a decision, and the
 * failing test is where it gets made deliberately. **The question to answer before adding one is
 * whether the server could render it**: the words arrive as props already, the data is fetched in
 * the `page.tsx` above, and `<details>` is HTML.
 *
 * Phone first (issue #117) added none: the navigation changed shape entirely and stayed the same
 * island it was, because a media query is not JavaScript. The mailed account actions (issue #149,
 * ADR 0054) added three, all on their own routes and none in the shell: the banner that asks for a
 * confirmation is a link to a page, so no signed-in screen pays for a button it does not show.
 */
import { describe, expect, it } from "vitest";

import { sourceFiles } from "../lib/sourceFiles";

/** Why each of these must run in the browser. One line each, and a real reason. */
const ISLANDS: Record<string, string> = {
  "app/error.tsx": "Next requires an error boundary to be a Client Component (it takes `reset`, issue #132)",
  "app/global-error.tsx":
    "same as app/error.tsx, and it replaces the root layout's own <html>/<body> on top of that (issue #132)",
  "components/AccountMailButton.tsx":
    "a confirmation must be a POST, not a link a mail scanner can fetch (issue #149, ADR 0054)",
  "components/AddOriginalScreen.tsx":
    "reads the customer's file and hashes it in the page (issue #29, ADR 0044)",
  "components/DataRequestForms.tsx":
    "three writes: a copy is asked for, a deletion is asked for after a word is typed, and either can be taken back — each followed by asking the server for the page again (ADR 0056)",
  "components/FileDrop.tsx": "a drop target is a DOM event handler",
  "components/ForgotPasswordForm.tsx":
    "an address is posted and the one neutral sentence is rendered in place (issue #149, ADR 0054)",
  "components/InviteForm.tsx":
    "a password is posted, a refusal becomes a sentence, and each step asks the server for the next one (issue #115, ADR 0052)",
  "components/LanguageSwitch.tsx": "writes the language cookie and asks the server for the page again",
  "components/Nav.tsx": "the 'you are here' marker needs the browser's own path",
  "components/NoticeDraftScreen.tsx": "a draft is filled in, ticked and submitted without leaving the page",
  "components/Refresher.tsx": "asks the server again on a timer while something is moving",
  "components/ResetPasswordForm.tsx":
    "a new password is posted and a refusal becomes a sentence (issue #149, ADR 0054)",
  "components/RightsScreen.tsx": "forms, file upload and writes without leaving the page",
  "components/VerificationForm.tsx":
    "two forms: the platform account is claimed and \"I have placed the code\" is pressed, each followed by asking the server for the page again (ADR 0055)",
  "components/SignInForm.tsx": "a password is posted and the answer becomes a sentence (ADR 0023)",
  "components/SignOutButton.tsx": "posts the sign-out and then asks the server for everything again",
  "lib/uploadMedia.ts": "reads and sends a file in chunks from the page (ADR 0044)",
  "lib/useUploadStatus.ts": "polls an upload's status with a backoff (ADR 0044 amendment)",
  "lib/write.ts": "how an island sends a write and then refreshes the route",
};

describe("the client components", () => {
  const found = sourceFiles()
    .filter(({ source }) => /^\s*["']use client["']/m.test(source))
    .map(({ file }) => file)
    .sort();

  it("are exactly the ones on the list, and no others", () => {
    // A new name here means something became a client component. Decide it, then add it above
    // with the reason — do not delete the name that is failing.
    expect(found).toEqual(Object.keys(ISLANDS).sort());
  });

  it("found the islands at all", () => {
    expect(found.length).toBeGreaterThan(5);
  });

  it("keeps the screens themselves on the server", () => {
    const screens = sourceFiles().filter(({ file }) => file.startsWith("screens/"));
    expect(screens.length).toBeGreaterThan(4);
    for (const { file, source } of screens) {
      expect(/^\s*["']use client["']/m.test(source), `${file} became a client component`).toBe(false);
    }
  });

  it("keeps every page and layout on the server", () => {
    const pages = sourceFiles().filter(
      ({ file }) => /\/(page|layout|not-found)\.tsx$/.test(file) || file === "app/not-found.tsx",
    );
    expect(pages.length).toBeGreaterThan(8);
    for (const { file, source } of pages) {
      expect(/^\s*["']use client["']/m.test(source), `${file} became a client component`).toBe(false);
    }
  });
});
