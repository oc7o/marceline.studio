/**
 * The root error boundary (issue #132): never a stack trace, a way back to `/app`, the contact
 * address, and both languages — there is no request here to ask which one the customer reads.
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import ErrorBoundary from "./error";

describe("the error boundary", () => {
  it("never renders the thrown error's message or stack", () => {
    const secret = "SECRET_DETAIL_a_stack_trace_would_carry";
    const error = new Error(secret);
    error.stack = `Error: ${secret}\n    at somewhere`;
    const html = renderToStaticMarkup(<ErrorBoundary error={error} reset={() => undefined} />);
    expect(html).not.toContain(secret);
    expect(html).not.toContain("at somewhere");
  });

  it("says so in German and in English", () => {
    const html = renderToStaticMarkup(
      <ErrorBoundary error={new Error("boom")} reset={() => undefined} />,
    );
    expect(html).toContain('lang="de"');
    expect(html).toContain('lang="en"');
  });

  it("offers a way back to the overview and the contact address", () => {
    const html = renderToStaticMarkup(
      <ErrorBoundary error={new Error("boom")} reset={() => undefined} />,
    );
    expect(html).toContain('href="/app"');
    expect(html).toContain('href="mailto:contact@marceline.studio"');
  });

  it("wires the reset button to the reset prop", () => {
    const reset = vi.fn();
    const html = renderToStaticMarkup(<ErrorBoundary error={new Error("boom")} reset={reset} />);
    expect(html).toContain("<button");
    expect(reset).not.toHaveBeenCalled();
  });
});
