import { describe, expect, it } from "vitest";

import { availabilityInfo, availabilityTone, isRemoved, recheckMessage, relativeAge } from "./availability";

const NOW = new Date("2026-09-15T12:00:00Z");

describe("relativeAge", () => {
  it("formats minutes, hours and days", () => {
    expect(relativeAge("2026-09-15T11:59:30Z", NOW)).toBe("just now");
    expect(relativeAge("2026-09-15T11:48:00Z", NOW)).toBe("12 min ago");
    expect(relativeAge("2026-09-15T09:00:00Z", NOW)).toBe("3 h ago");
    expect(relativeAge("2026-09-13T11:00:00Z", NOW)).toBe("2 d ago");
  });

  it("returns null for missing or unreadable values and never goes negative", () => {
    expect(relativeAge(null, NOW)).toBeNull();
    expect(relativeAge("not a date", NOW)).toBeNull();
    expect(relativeAge("2026-09-15T13:00:00Z", NOW)).toBe("just now");
  });
});

describe("availabilityInfo", () => {
  it("maps statuses to online / removed / unknown", () => {
    expect(availabilityTone("playable")).toBe("online");
    expect(availabilityTone("removed")).toBe("removed");
    expect(availabilityTone("unknown")).toBe("unknown");
    expect(availabilityTone("something new")).toBe("unknown");
  });

  it("says when the page was checked, or that it never was", () => {
    expect(availabilityInfo({ availability: "playable", availability_checked_at: "2026-09-15T09:00:00Z" }, NOW)).toEqual({
      tone: "online",
      label: "online",
      checked: "checked 3 h ago",
    });
    expect(availabilityInfo({ availability: "unknown", availability_checked_at: null }, NOW).checked).toBe("never checked");
    expect(isRemoved({ availability: "removed" })).toBe(true);
    expect(isRemoved({ availability: "playable" })).toBe(false);
  });
});

describe("recheckMessage", () => {
  it("explains that results arrive after the crawler visit", () => {
    expect(recheckMessage({ queued: [1, 2, 3], in_progress: [], skipped: [] })).toBe(
      "Queued 3 pages — results appear after the crawler visits them.",
    );
    expect(recheckMessage({ queued: [1], in_progress: [2], skipped: [3, 4] })).toBe(
      "Queued 1 page — results appear after the crawler visits them; 1 page already being checked; 2 pages skipped (site not enabled).",
    );
  });
});
