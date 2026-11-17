import { describe, expect, it } from "vitest";

import {
  addReferenceCommand,
  ago,
  barWidth,
  budgetTone,
  bytesLabel,
  count,
  domainHref,
  durationLabel,
  loadTone,
  queryLine,
  rate,
  robotsLabel,
  share,
  shellQuote,
  usd,
} from "./overviewFormat";

describe("numbers", () => {
  it("formats counts", () => {
    expect(count(12345)).toBe("12,345");
    expect(count(null)).toBe("—");
    expect(count(Number.NaN)).toBe("—");
  });

  it("clamps shares and bar widths", () => {
    expect(share(1, 4)).toBe(0.25);
    expect(share(5, 4)).toBe(1);
    expect(share(3, 0)).toBe(0);
    expect(barWidth(1, 3)).toBe("33.3%");
    expect(barWidth(2, 0)).toBe("0%");
  });

  it("colors the request budget", () => {
    expect(budgetTone(10, 1000)).toBe("ok");
    expect(budgetTone(800, 1000)).toBe("warn");
    expect(budgetTone(1000, 1000)).toBe("bad");
    expect(budgetTone(0, 0)).toBe("bad");
  });

  it("formats rates and money", () => {
    expect(rate(2 / 3)).toBe("66.7%");
    expect(rate(1)).toBe("100.0%");
    expect(rate(null)).toBe("n/a");
    expect(usd(0.006)).toBe("$0.0060");
    expect(usd(undefined)).toBe("—");
  });

  it("formats ages", () => {
    const now = new Date("2026-09-15T12:00:00Z");
    expect(ago("2026-09-15T11:59:50Z", now)).toBe("just now");
    expect(ago("2026-09-15T11:55:00Z", now)).toBe("5 min ago");
    expect(ago("2026-09-15T09:00:00Z", now)).toBe("3 h ago");
    expect(ago("2026-09-10T12:00:00Z", now)).toBe("5 d ago");
    expect(ago(null, now)).toBe("—");
    expect(ago("nonsense", now)).toBe("—");
  });

  it("labels robots results", () => {
    expect(robotsLabel(true)).toBe("allowed");
    expect(robotsLabel(false)).toBe("disallowed");
    expect(robotsLabel(null)).toBe("not probed");
  });

  it("formats byte sizes", () => {
    expect(bytesLabel(512)).toBe("512 B");
    expect(bytesLabel(84 * 1024 * 1024)).toBe("84.0 MB");
    expect(bytesLabel(7.9 * 1024 * 1024 * 1024)).toBe("7.9 GB");
    expect(bytesLabel(0)).toBe("0 B");
    expect(bytesLabel(null)).toBe("—");
    expect(bytesLabel(-5)).toBe("—");
  });

  it("formats plain durations", () => {
    expect(durationLabel(30)).toBe("30 s");
    expect(durationLabel(90)).toBe("2 min");
    expect(durationLabel(3600)).toBe("1 h");
    expect(durationLabel(3600 * 3)).toBe("3 h");
    expect(durationLabel(3600 * 72)).toBe("3 d");
    expect(durationLabel(null)).toBe("—");
  });

  it("colors load average against core count", () => {
    expect(loadTone(1, 4)).toBe("ok");
    expect(loadTone(4, 4)).toBe("warn");
    expect(loadTone(8, 4)).toBe("bad");
    expect(loadTone(null, 4)).toBe("ok");
    expect(loadTone(10, null)).toBe("ok");
  });
});

describe("candidate domains", () => {
  it("links bare hostnames only", () => {
    expect(domainHref("example.com")).toBe("https://example.com/");
    expect(domainHref("xn--bcher-kva.example")).toBe("https://xn--bcher-kva.example/");
    expect(domainHref("javascript:alert(1)")).toBeNull();
    expect(domainHref("evil.com/path")).toBeNull();
    expect(domainHref("user@evil.com")).toBeNull();
    expect(domainHref("localhost")).toBeNull();
    expect(domainHref("")).toBeNull();
  });

  it("renders search evidence as text", () => {
    expect(queryLine({ query: "artist leak", rank: 2, url: "https://a.test/v", title: "Video" })).toBe(
      "#2 “artist leak” → https://a.test/v · Video",
    );
    expect(queryLine({ query: "q", rank: null, url: "https://a.test/", title: "" })).toBe("#? “q” → https://a.test/");
  });
});

describe("add_reference command", () => {
  it("quotes arguments for the shell", () => {
    expect(shellQuote("simple-name")).toBe("simple-name");
    expect(shellQuote("Jane Doe")).toBe("'Jane Doe'");
    expect(shellQuote("it's")).toBe(`'it'\\''s'`);
    expect(addReferenceCommand(["Jane Doe"], "/videos/a.mp4", "Sunrise")).toBe(
      "cd backend && uv run python manage.py add_reference --creator 'Jane Doe' --file /videos/a.mp4 --title Sunrise",
    );
    expect(addReferenceCommand(["Sasha Meyer", "Carla Bremer"], "/videos/a.mp4", "Scene")).toContain(
      "--creator 'Sasha Meyer' --creator 'Carla Bremer' --file /videos/a.mp4",
    );
    expect(addReferenceCommand([])).toContain("--creator CREATOR");
    expect(addReferenceCommand()).toContain("--creator CREATOR --file /path/to/original.mp4 --title TITLE");
  });
});
