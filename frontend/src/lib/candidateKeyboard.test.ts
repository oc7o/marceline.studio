import { describe, expect, it } from "vitest";

import {
  type CandidateState,
  candidateKeyToCommand,
  candidateReducer,
  initialCandidateState,
  targetIds,
} from "./candidateKeyboard";

const loaded = (ids: number[]): CandidateState => candidateReducer(initialCandidateState, { type: "load", ids });

describe("candidateKeyToCommand", () => {
  it.each([
    ["a", "approve"],
    ["A", "approve"],
    ["r", "reject"],
    ["h", "hoster"],
    ["I", "ignore"],
    ["p", "reset"],
  ])("maps %s to %s", (key, action) => {
    expect(candidateKeyToCommand({ key })).toEqual({ type: "decide", action });
  });

  it("maps navigation, selection and help", () => {
    expect(candidateKeyToCommand({ key: "j" })).toEqual({ type: "next" });
    expect(candidateKeyToCommand({ key: "ArrowDown" })).toEqual({ type: "next" });
    expect(candidateKeyToCommand({ key: "K" })).toEqual({ type: "prev" });
    expect(candidateKeyToCommand({ key: "ArrowUp" })).toEqual({ type: "prev" });
    expect(candidateKeyToCommand({ key: "x" })).toEqual({ type: "toggleSelect" });
    expect(candidateKeyToCommand({ key: "?" })).toEqual({ type: "help" });
    expect(candidateKeyToCommand({ key: "Escape" })).toEqual({ type: "escape" });
  });

  it("ignores other keys, modifiers and typing in fields", () => {
    const input = { tagName: "input" } as unknown as EventTarget;
    expect(candidateKeyToCommand({ key: "y" })).toBeNull();
    expect(candidateKeyToCommand({ key: "Enter" })).toBeNull();
    expect(candidateKeyToCommand({ key: "a", ctrlKey: true })).toBeNull();
    expect(candidateKeyToCommand({ key: "r", metaKey: true })).toBeNull();
    expect(candidateKeyToCommand({ key: "a", target: input })).toBeNull();
    expect(candidateKeyToCommand({ key: "Escape", target: input })).toEqual({ type: "escape" });
  });
});

describe("candidateReducer", () => {
  it("moves within bounds", () => {
    let state = loaded([1, 2, 3]);
    state = candidateReducer(state, { type: "prev" });
    expect(state.index).toBe(0);
    state = candidateReducer(candidateReducer(candidateReducer(state, { type: "next" }), { type: "next" }), {
      type: "next",
    });
    expect(state.index).toBe(2);
    expect(candidateReducer(state, { type: "focus", id: 1 }).index).toBe(0);
    expect(candidateReducer(state, { type: "focus", id: 99 })).toBe(state);
    expect(candidateReducer(initialCandidateState, { type: "next" }).index).toBe(0);
  });

  it("keeps the current row when the list reloads", () => {
    let state = candidateReducer(loaded([1, 2, 3]), { type: "focus", id: 3 });
    state = candidateReducer(state, { type: "toggle", id: 2 });
    state = candidateReducer(state, { type: "load", ids: [3, 4] });
    expect(state.index).toBe(0);
    expect(state.selected).toEqual([]);
    state = candidateReducer(state, { type: "load", ids: [7] });
    expect(state.index).toBe(0);
  });

  it("targets the selection, else the current row", () => {
    let state = loaded([1, 2, 3]);
    expect(targetIds(state)).toEqual([1]);
    state = candidateReducer(state, { type: "toggle", id: 3 });
    state = candidateReducer(state, { type: "toggle", id: 2 });
    expect(targetIds(state)).toEqual([3, 2]);
    state = candidateReducer(state, { type: "toggle", id: 3 });
    expect(targetIds(state)).toEqual([2]);
    expect(candidateReducer(state, { type: "toggle", id: 42 })).toBe(state);
    expect(candidateReducer(state, { type: "selectAll" }).selected).toEqual([1, 2, 3]);
    expect(candidateReducer(state, { type: "clearSelection" }).selected).toEqual([]);
    expect(targetIds(initialCandidateState)).toEqual([]);
  });

  it("toggles the current row with X", () => {
    let state = candidateReducer(loaded([1, 2]), { type: "next" });
    state = candidateReducer(state, { type: "toggleCurrent" });
    expect(state.selected).toEqual([2]);
    state = candidateReducer(state, { type: "toggleCurrent" });
    expect(state.selected).toEqual([]);
    expect(candidateReducer(initialCandidateState, { type: "toggleCurrent" })).toBe(initialCandidateState);
  });

  it("advances past a single decision and marks it pending", () => {
    let state = loaded([1, 2, 3]);
    state = candidateReducer(state, { type: "decided", ids: [1], status: "rejected" });
    expect(state.index).toBe(1);
    expect(state.statuses).toEqual({ 1: "rejected" });
    expect(state.pending).toEqual([1]);
    state = candidateReducer(state, { type: "settled", ids: [1] });
    expect(state.pending).toEqual([]);
  });

  it("skips rows covered by a bulk decision and stays on the last row", () => {
    let state = loaded([1, 2, 3, 4]);
    state = candidateReducer(state, { type: "toggle", id: 1 });
    state = candidateReducer(state, { type: "toggle", id: 2 });
    state = candidateReducer(state, { type: "decided", ids: targetIds(state), status: "ignored" });
    expect(state.index).toBe(2);
    expect(state.selected).toEqual([]);
    state = candidateReducer(state, { type: "focus", id: 4 });
    state = candidateReducer(state, { type: "decided", ids: [4], status: "approved" });
    expect(state.index).toBe(3);
    expect(targetIds(state)).toEqual([]);
  });

  it("leaves the position alone when the current row was not decided", () => {
    let state = candidateReducer(loaded([1, 2, 3]), { type: "decided", ids: [3], status: "hoster" });
    expect(state.index).toBe(0);
    state = candidateReducer(state, { type: "decided", ids: [1], status: "rejected" });
    expect(state.index).toBe(1);
  });

  it("reverts failed decisions to the previous optimistic status", () => {
    let state = loaded([1, 2]);
    state = candidateReducer(state, { type: "decided", ids: [1], status: "rejected" });
    state = candidateReducer(state, { type: "decided", ids: [1, 2], status: "ignored" });
    state = candidateReducer(state, { type: "failed", ids: [1, 2], previous: { 1: "rejected", 2: undefined } });
    expect(state.statuses).toEqual({ 1: "rejected" });
    expect(state.pending).toEqual([]);
  });
});
