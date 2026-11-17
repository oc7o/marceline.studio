// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type ReleaseItem, ReleaseRow } from "./SafetyReleaseQueue";

afterEach(cleanup);

const ITEM: ReleaseItem = {
  id: 7,
  site: "example.test",
  title: "Alice Customer flagged term costume sketch",
  tags: ["costume"],
  performers: ["Alice Customer"],
  matches: [{ term: "flagged term", category: "minor", action: "customer_release_queue" }],
  customer_credits: [{ creator_id: 3, creator_name: "Alice Customer", matched_in: ["performers", "title"] }],
  flagged_at: "2026-09-17T08:00:00Z",
  queued_at: "2026-09-17T08:00:00Z",
};

describe("ReleaseRow", () => {
  it("shows text only: title, credits, terms, performers and tags, and no image or link", () => {
    const { container } = render(<ReleaseRow item={ITEM} checked={false} onToggle={() => undefined} />);

    expect(screen.getByText("Alice Customer flagged term costume sketch")).toBeTruthy();
    expect(screen.getByText("Alice Customer · performers, title")).toBeTruthy();
    expect(screen.getByText("Performers: Alice Customer")).toBeTruthy();
    expect(screen.getByText("Tags: costume")).toBeTruthy();
    expect(container.querySelector("img, a, video, iframe, picture")).toBeNull();
  });

  it("toggles the selection", () => {
    const onToggle = vi.fn();
    render(<ReleaseRow item={ITEM} checked={false} onToggle={onToggle} />);

    fireEvent.click(screen.getByRole("checkbox"));
    expect(onToggle).toHaveBeenCalledOnce();
  });

  it("warns when no customer creator is credited any more", () => {
    render(<ReleaseRow item={{ ...ITEM, customer_credits: [] }} checked={false} onToggle={() => undefined} />);

    expect(screen.getByText("No customer creator credited any more: keep it blocked.")).toBeTruthy();
  });
});
