import { describe, expect, it } from "vitest";

import { exportMessageKey, exportProblemFor, isExportProblem } from "./exportDownload";

describe("exportProblemFor", () => {
  it("reads a 429 as the rate limit", () => {
    expect(exportProblemFor(429)).toBe("rate_limited");
  });

  it("reads a 404 as not available (no organisation, or the route is off)", () => {
    expect(exportProblemFor(404)).toBe("not_available");
  });

  it("falls back to the generic problem for anything else", () => {
    expect(exportProblemFor(500)).toBe("error");
    expect(exportProblemFor(401)).toBe("error");
    expect(exportProblemFor(403)).toBe("error");
  });
});

describe("isExportProblem", () => {
  it("accepts the three known values", () => {
    expect(isExportProblem("rate_limited")).toBe(true);
    expect(isExportProblem("not_available")).toBe(true);
    expect(isExportProblem("error")).toBe(true);
  });

  it("refuses anything else a query string could carry", () => {
    expect(isExportProblem("")).toBe(false);
    expect(isExportProblem("RATE_LIMITED")).toBe(false);
    expect(isExportProblem("<script>")).toBe(false);
  });
});

describe("exportMessageKey", () => {
  it("names the shell.export key for each problem", () => {
    expect(exportMessageKey("rate_limited")).toBe("shell.export.rateLimited");
    expect(exportMessageKey("not_available")).toBe("shell.export.notAvailable");
    expect(exportMessageKey("error")).toBe("shell.export.error");
  });
});
