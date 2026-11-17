/**
 * "Download my data" (issue #131): what a non-200 from `GET /api/v1/app/export` means to the
 * customer, and the `shell.export.*` key that says so.
 *
 * Before this, the same-origin `<a download>` in `Shell` saved whatever Django answered as a
 * file — including a 429 rate-limit refusal or the 404 an account without an organisation gets —
 * so a customer who clicked twice downloaded `{"detail":"rate_limited"}` and read nothing. The
 * BFF route now redirects a non-200 to a small result page instead of relaying it as a file; this
 * module is the one rule both sides share, so the redirect's query string and the page's message
 * cannot drift apart.
 */

export type ExportProblem = "rate_limited" | "not_available" | "error";

const PROBLEMS: readonly ExportProblem[] = ["rate_limited", "not_available", "error"];

/** Django's status, reduced to the three things the customer can be told apart. */
export function exportProblemFor(status: number): ExportProblem {
  if (status === 429) return "rate_limited";
  if (status === 404) return "not_available";
  return "error";
}

/** Narrows an untrusted query-string value; anything unrecognised is the generic problem. */
export function isExportProblem(value: string): value is ExportProblem {
  return (PROBLEMS as readonly string[]).includes(value);
}

/** The `shell.export.*` key for a problem (`frontend/src/i18n/{en,de}/core.ts`). */
export function exportMessageKey(problem: ExportProblem): string {
  switch (problem) {
    case "rate_limited":
      return "shell.export.rateLimited";
    case "not_available":
      return "shell.export.notAvailable";
    case "error":
      return "shell.export.error";
  }
}
