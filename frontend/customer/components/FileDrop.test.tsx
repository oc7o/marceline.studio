// @vitest-environment jsdom
/**
 * The file chooser, in a browser-shaped environment: it is an island, and dragging is the one thing
 * a server render cannot show.
 *
 * What matters here is not the drag: it is that a `File` reaches the caller and **nothing else
 * happens to it** — no read, no object URL, no preview. `lib/privacy.test.ts` forbids the
 * constructs; this checks the behaviour.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FileDrop } from "./FileDrop";

afterEach(cleanup);

const video = () => new File(["hashes only"], "reel.mp4", { type: "video/mp4" });

describe("choosing a file", () => {
  it("keeps the real input, with the screen's label pointing at it", () => {
    render(
      <>
        <label htmlFor="video">Video</label>
        <FileDrop id="video" label="Drop it here" onChoose={() => undefined} />
      </>,
    );
    const input = screen.getByLabelText("Video");
    expect(input).toHaveProperty("type", "file");
  });

  it("hands the chosen file to the caller", () => {
    const onChoose = vi.fn();
    const { container } = render(<FileDrop id="video" label="Drop it here" onChoose={onChoose} />);
    const input = container.querySelector("input");
    if (!input) throw new Error("no file input");
    const file = video();
    fireEvent.change(input, { target: { files: [file] } });
    expect(onChoose).toHaveBeenCalledWith(file);
  });

  it("hands over a dropped file too, and marks the target while it is over", () => {
    const onChoose = vi.fn();
    const { container } = render(
      <FileDrop id="video" label="Drop it here" onChoose={onChoose} />,
    );
    const target = container.firstElementChild;
    expect(target).not.toBeNull();
    if (!target) return;

    fireEvent.dragOver(target);
    expect(target.getAttribute("data-over")).toBe("");

    const file = video();
    fireEvent.drop(target, { dataTransfer: { files: [file] } });
    expect(onChoose).toHaveBeenCalledWith(file);
    expect(target.getAttribute("data-over")).toBeNull();
  });

  it("takes nothing while it is disabled", () => {
    const onChoose = vi.fn();
    const { container } = render(
      <FileDrop id="video" label="Drop it here" disabled onChoose={onChoose} />,
    );
    const target = container.firstElementChild;
    if (!target) throw new Error("no drop target");
    fireEvent.drop(target, { dataTransfer: { files: [video()] } });
    expect(onChoose).not.toHaveBeenCalled();
  });

  it("hands over every photograph of a set, and says so on the input", () => {
    const onChoose = vi.fn();
    const { container } = render(
      <FileDrop id="photos" multiple label="Drop them here" onChoose={onChoose} />,
    );
    const input = container.querySelector("input");
    if (!input) throw new Error("no file input");
    expect(input.multiple).toBe(true);

    const photos = [
      new File(["hashes only"], "beach-01.jpg", { type: "image/jpeg" }),
      new File(["hashes only"], "beach-02.jpg", { type: "image/jpeg" }),
    ];
    fireEvent.change(input, { target: { files: photos } });
    expect(onChoose).toHaveBeenCalledWith(photos);
  });

  it("says what was chosen in words, and shows no picture of it", () => {
    const { container } = render(
      <FileDrop id="video" label="Drop it here" chosen="reel.mp4 · 12 MB" onChoose={() => undefined} />,
    );
    expect(screen.getByText("reel.mp4 · 12 MB")).toBeTruthy();
    expect(container.querySelectorAll("img, video, canvas, picture")).toHaveLength(0);
  });
});
