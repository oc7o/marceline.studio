// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AvailabilityBadge } from "./Availability";
import { StatusLegend } from "./StatusLegend";

afterEach(cleanup);

const NOW = new Date("2026-09-15T12:00:00Z");

describe("AvailabilityBadge", () => {
  it("shows online with the age of the last check", () => {
    render(<AvailabilityBadge page={{ availability: "playable", availability_checked_at: "2026-09-15T09:00:00Z" }} now={NOW} />);
    expect(screen.getByText("online")).toBeTruthy();
    expect(screen.getByText("checked 3 h ago")).toBeTruthy();
  });

  it("shows removed and never-checked pages", () => {
    render(
      <>
        <AvailabilityBadge page={{ availability: "removed", availability_checked_at: "2026-09-14T12:00:00Z" }} now={NOW} />
        <AvailabilityBadge page={{ availability: "unknown", availability_checked_at: null }} now={NOW} />
      </>,
    );
    expect(screen.getByText("removed")).toBeTruthy();
    expect(screen.getByText("checked 1 d ago")).toBeTruthy();
    expect(screen.getByText("unknown")).toBeTruthy();
    expect(screen.getByText("never checked")).toBeTruthy();
  });
});

describe("StatusLegend", () => {
  it("opens a legend that says nothing is auto-confirmed", () => {
    render(<StatusLegend />);
    const toggle = screen.getByRole("button", { name: "What do the statuses mean?" });
    expect(screen.queryByRole("region")).toBeNull();

    fireEvent.click(toggle);

    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const legend = screen.getByRole("region", { name: "Status legend" });
    expect(legend.textContent).toContain("Nothing is auto-confirmed");
    expect(legend.textContent).toContain("proposed");
    expect(legend.textContent).toContain("rejected");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("region")).toBeNull();
  });
});
