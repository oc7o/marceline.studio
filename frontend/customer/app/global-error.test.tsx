/**
 * The last-resort boundary (issue #132): replaces the root layout's own `<html>`/`<body>`, so it
 * must define both itself and depend on no stylesheet. Same "no stack trace" rule as `error.tsx`.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import GlobalError from "./global-error";

describe("the global error boundary", () => {
  it("defines its own html and body, since the layout that would have is what failed", () => {
    const html = renderToStaticMarkup(
      <GlobalError error={new Error("boom")} reset={() => undefined} />,
    );
    expect(html).toMatch(/^<html/);
    expect(html).toContain("<body>");
  });

  it("never renders the thrown error's message or stack", () => {
    const secret = "SECRET_DETAIL_a_stack_trace_would_carry";
    const error = new Error(secret);
    error.stack = `Error: ${secret}\n    at somewhere`;
    const html = renderToStaticMarkup(<GlobalError error={error} reset={() => undefined} />);
    expect(html).not.toContain(secret);
    expect(html).not.toContain("at somewhere");
  });

  it("says so in German and in English, with a way back and the contact address", () => {
    const html = renderToStaticMarkup(
      <GlobalError error={new Error("boom")} reset={() => undefined} />,
    );
    expect(html).toContain('lang="de"');
    expect(html).toContain('lang="en"');
    expect(html).toContain('href="/app"');
    expect(html).toContain('href="mailto:contact@marceline.studio"');
  });

  it("uses no class name: nothing here may depend on the stylesheet that failed to load", () => {
    const html = renderToStaticMarkup(
      <GlobalError error={new Error("boom")} reset={() => undefined} />,
    );
    expect(html).not.toContain("class=");
  });
});
