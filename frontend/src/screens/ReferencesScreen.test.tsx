// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ReferenceItem } from "../api/client";
import type { ReviewState } from "../lib/reviewReducer";
import { ItemView, type PairUndo, QueueList } from "./ReferencesScreen";

afterEach(cleanup);

function makeItem(key: string): ReferenceItem {
  return {
    key,
    status: "proposed",
    reference: {
      id: 1,
      title: "Original",
      creator_id: 1,
      creator_name: "Creator",
      creator_names: ["Creator"],
      duration_s: 100,
    },
    page: {
      id: 1,
      site: "example.test",
      title: "Page",
      url: "https://example.test/x",
      duration_s: 100,
      availability: "playable",
      availability_checked_at: null,
      uploaded_at: null,
      safety_status: "clear",
    },
    signals: {
      score: 0.5,
      duration_diff_s: 0,
      duration_ratio: 1,
      title_similarity: 0.5,
      creator_linked: true,
      matched_strength: null,
      related_kind: null,
      related_page_id: null,
    },
    leak: null,
    match: null,
    cluster: null,
    media: [],
    media_hidden: false,
  };
}

function makeState(index: number): ReviewState<PairUndo> {
  return { index, overrides: {}, selected: [], anchor: null, history: [] };
}

describe("ItemView", () => {
  // jsdom's Element has no scrollTo at all: adding it (rather than spying on an existing one)
  // must be undone by hand, or it leaks into every other test file sharing this worker.
  const originalScrollTo = Element.prototype.scrollTo;
  let focusSpy: ReturnType<typeof vi.spyOn>;
  let scrollToSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    focusSpy = vi.spyOn(HTMLElement.prototype, "focus");
    scrollToSpy = vi.fn();
    Element.prototype.scrollTo = scrollToSpy as unknown as typeof Element.prototype.scrollTo;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    Element.prototype.scrollTo = originalScrollTo;
  });

  it("moves accessible focus to the new item without letting the browser scroll for it, then deliberately puts the page back at the top", () => {
    render(
      <main>
        <ItemView
          item={makeItem("r1-p1")}
          status="proposed"
          selected={false}
          mode="privacy"
          thresholds={undefined}
          onDecide={() => undefined}
          onSkip={() => undefined}
          onReset={() => undefined}
          onRecheck={() => undefined}
        />
      </main>,
    );

    expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true });
    expect(scrollToSpy).toHaveBeenCalledWith({ top: 0 });
  });

  it("does it again for every new item, so a run of decisions keeps landing on top", () => {
    const { rerender } = render(
      <main>
        <ItemView
          item={makeItem("r1-p1")}
          status="proposed"
          selected={false}
          mode="privacy"
          thresholds={undefined}
          onDecide={() => undefined}
          onSkip={() => undefined}
          onReset={() => undefined}
          onRecheck={() => undefined}
        />
      </main>,
    );
    scrollToSpy.mockClear();

    rerender(
      <main>
        <ItemView
          item={makeItem("r1-p2")}
          status="proposed"
          selected={false}
          mode="privacy"
          thresholds={undefined}
          onDecide={() => undefined}
          onSkip={() => undefined}
          onReset={() => undefined}
          onRecheck={() => undefined}
        />
      </main>,
    );

    expect(scrollToSpy).toHaveBeenCalledWith({ top: 0 });
  });
});

describe("QueueList", () => {
  // Same as above: jsdom has no scrollIntoView, so adding one must be removed again by hand.
  const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

  afterEach(() => {
    HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
  });

  it("never calls the ancestor-climbing scrollIntoView (that used to drag the whole page down)", () => {
    const scrollIntoViewSpy = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoViewSpy as unknown as typeof HTMLElement.prototype.scrollIntoView;

    const items = [makeItem("r1-p1"), makeItem("r1-p2")];
    render(<QueueList items={items} state={makeState(1)} dispatch={() => undefined} />);

    expect(scrollIntoViewSpy).not.toHaveBeenCalled();
  });
});
