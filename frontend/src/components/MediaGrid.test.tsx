// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { IMAGE_MODE_KEY, resetImageModeCache } from "../lib/imageMode";
import { ImageModeToggle } from "./ImageModeToggle";
import { MediaGrid } from "./MediaGrid";

const media = [
  { id: 11, kind: "poster" },
  { id: 12, kind: "frame" },
];

beforeEach(() => {
  window.localStorage.clear();
  resetImageModeCache();
});
afterEach(cleanup);

describe("ImageModeToggle", () => {
  it("starts in privacy mode and remembers load mode", () => {
    render(<ImageModeToggle />);
    const toggle = screen.getByRole("switch");
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(toggle.textContent).toContain("Privacy mode");

    fireEvent.click(toggle);

    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(window.localStorage.getItem(IMAGE_MODE_KEY)).toBe("load");
  });
});

describe("MediaGrid", () => {
  it("renders no image in privacy mode", () => {
    const { container } = render(<MediaGrid media={media} hidden={false} mode="privacy" />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getAllByText("privacy mode")).toHaveLength(2);
  });

  it("loads images only through the passthrough endpoint", () => {
    const { container } = render(<MediaGrid media={media} hidden={false} mode="load" />);
    const sources = [...container.querySelectorAll("img")].map((img) => img.getAttribute("src"));
    expect(sources).toEqual(["/api/v1/media/11/image", "/api/v1/media/12/image"]);
  });

  it("shows image unavailable when the passthrough fails", () => {
    render(<MediaGrid media={media.slice(0, 1)} hidden={false} mode="load" />);
    fireEvent.error(screen.getByRole("img"));
    expect(screen.getByText("image unavailable")).toBeTruthy();
  });

  it("offers no reveal control in privacy mode: there is nothing to reveal", () => {
    render(<MediaGrid media={media} hidden={false} mode="privacy" />);
    expect(screen.queryByRole("button", { name: /reveal/i })).toBeNull();
  });

  /*
    The design system's media rule, and this is the surface it was written for: load mode decides
    whether the bytes are fetched, the blur decides whether they are looked at. One control for the
    group, and the blur comes back the moment the group loses focus.
  */
  it("blurs unreviewed media until asked, and re-blurs when the group loses focus", () => {
    const { container } = render(
      <>
        <MediaGrid media={media} hidden={false} mode="load" />
        <button type="button">elsewhere</button>
      </>,
    );
    const blurred = () => [...container.querySelectorAll("img")].every((img) => img.className.includes("blur-thumb"));
    expect(blurred()).toBe(true);

    const reveal = screen.getByRole("button", { name: "Reveal images" });
    fireEvent.click(reveal);
    expect(blurred()).toBe(false);

    fireEvent.blur(reveal, { relatedTarget: screen.getByRole("button", { name: "elsewhere" }) });
    expect(blurred()).toBe(true);
  });

  it("never renders images of flagged pages, even in load mode", () => {
    const { container } = render(<MediaGrid media={media} hidden mode="load" />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/flagged for safety review/)).toBeTruthy();
  });

  /* The common-frame screen marks which frame of a page repeats (issue #16 follow-up: "which
     frame is the same"), sorted first so the eye finds it without scanning the whole row. */
  it("marks the given media id and puts it first", () => {
    const { container } = render(
      <MediaGrid media={media} hidden={false} mode="load" markedIds={new Set([12])} />,
    );
    const items = [...container.querySelectorAll("li")];
    expect(items[0]?.className).toContain("border-accent");
    expect(items[0]?.querySelector("img")?.getAttribute("src")).toBe("/api/v1/media/12/image");
    expect(items[0]?.textContent).toContain("repeated frame");
    expect(items[1]?.className).not.toContain("border-accent");
  });

  it("marks nothing when markedIds is not given, and keeps the given order", () => {
    const { container } = render(<MediaGrid media={media} hidden={false} mode="load" />);
    const sources = [...container.querySelectorAll("img")].map((img) => img.getAttribute("src"));
    expect(sources).toEqual(["/api/v1/media/11/image", "/api/v1/media/12/image"]);
    expect(container.querySelector(".border-accent")).toBeNull();
  });
});
