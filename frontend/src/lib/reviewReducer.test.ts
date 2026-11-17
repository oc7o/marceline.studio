import { describe, expect, it } from "vitest";

import {
  type ItemStatus,
  type ReviewAction,
  type ReviewState,
  initialReviewState,
  reviewReducer,
  statusOf,
  undoable,
} from "./reviewReducer";

type Undo = { ids: number[] };

function run(actions: ReviewAction<Undo>[], state = initialReviewState<Undo>()): ReviewState<Undo> {
  return actions.reduce(reviewReducer<Undo>, state);
}

const item = (key: string): { key: string; status: ItemStatus } => ({ key, status: "proposed" });
const order = ["a", "b", "c", "d"];

describe("navigation", () => {
  it("skips forward up to the end-of-queue position and back to zero", () => {
    const end = run([
      { type: "next", total: 2 },
      { type: "next", total: 2 },
      { type: "next", total: 2 },
    ]);
    expect(end.index).toBe(2);
    expect(run([{ type: "prev" }, { type: "prev" }], end).index).toBe(0);
    expect(run([{ type: "goto", index: 9, total: 4 }]).index).toBe(4);
  });
});

describe("decision flow", () => {
  it("applies a decision optimistically and moves to the next item", () => {
    const state = run([
      { type: "decided", id: 1, keys: ["a"], decision: "confirm", previous: { a: "proposed" }, advance: true, total: 4 },
    ]);
    expect(state.index).toBe(1);
    expect(statusOf(state, item("a"))).toBe("confirmed");
    expect(statusOf(state, item("b"))).toBe("proposed");
    expect(undoable(state)).toBeNull(); // not settled yet
  });

  it("undoes the last settled decision and returns to its item", () => {
    const state = run([
      { type: "decided", id: 1, keys: ["a"], decision: "confirm", previous: { a: "proposed" }, advance: true, total: 4 },
      { type: "decided", id: 2, keys: ["b"], decision: "reject", previous: { b: "proposed" }, advance: true, total: 4 },
      { type: "settled", id: 1, undo: { ids: [10] } },
      { type: "settled", id: 2, undo: { ids: [11] } },
    ]);
    expect(state.index).toBe(2);
    expect(undoable(state)?.undo).toEqual({ ids: [11] });

    const undone = reviewReducer(state, { type: "undone" });
    expect(undone.index).toBe(1);
    expect(statusOf(undone, item("b"))).toBe("proposed");
    expect(statusOf(undone, item("a"))).toBe("confirmed");
    expect(undoable(undone)?.undo).toEqual({ ids: [10] });
  });

  it("reverts a failed decision", () => {
    const state = run([
      { type: "decided", id: 7, keys: ["a"], decision: "reject", previous: { a: "proposed" }, advance: true, total: 4 },
      { type: "failed", id: 7 },
    ]);
    expect(state.index).toBe(0);
    expect(statusOf(state, item("a"))).toBe("proposed");
    expect(state.history).toEqual([]);
  });

  it("restores an earlier override on undo", () => {
    const state = run([
      { type: "setStatus", keys: ["a"], status: "rejected" },
      { type: "decided", id: 1, keys: ["a"], decision: "confirm", previous: { a: "rejected" }, advance: false, total: 4 },
      { type: "settled", id: 1, undo: { ids: [1] } },
      { type: "undone" },
    ]);
    expect(statusOf(state, item("a"))).toBe("rejected");
  });

  it("bulk decisions keep the position and clear the selection", () => {
    const state = run([
      { type: "toggleSelect", key: "b" },
      { type: "toggleSelect", key: "c" },
      {
        type: "decided",
        id: 1,
        keys: ["b", "c"],
        decision: "reject",
        previous: { b: "proposed", c: "proposed" },
        advance: false,
        total: 4,
      },
    ]);
    expect(state.index).toBe(0);
    expect(state.selected).toEqual([]);
    expect(statusOf(state, item("c"))).toBe("rejected");
  });
});

describe("selection", () => {
  it("toggles single items", () => {
    const state = run([
      { type: "toggleSelect", key: "a" },
      { type: "toggleSelect", key: "b" },
      { type: "toggleSelect", key: "a" },
    ]);
    expect(state.selected).toEqual(["b"]);
  });

  it("selects a shift-click range from the anchor in either direction", () => {
    expect(
      run([
        { type: "toggleSelect", key: "a" },
        { type: "selectRange", order, key: "c" },
      ]).selected,
    ).toEqual(["a", "b", "c"]);
    expect(
      run([
        { type: "toggleSelect", key: "d" },
        { type: "selectRange", order, key: "b" },
      ]).selected,
    ).toEqual(["d", "b", "c"]);
  });

  it("treats a range without anchor as a toggle, and clears", () => {
    const state = run([{ type: "selectRange", order, key: "c" }]);
    expect(state.selected).toEqual(["c"]);
    expect(reviewReducer(state, { type: "clearSelection" }).selected).toEqual([]);
  });
});
