/** Source candidate review: keyboard map and list state. Pure: event in, state/command out. */

import type { KeyInput } from "./keyboard";

export type CandidateAction = "approve" | "reject" | "hoster" | "ignore" | "reset";

export type CandidateCommand =
  | { type: "decide"; action: CandidateAction }
  | { type: "next" }
  | { type: "prev" }
  | { type: "toggleSelect" }
  | { type: "help" }
  | { type: "escape" };

export const CANDIDATE_SHORTCUTS: ReadonlyArray<{ keys: string; action: string }> = [
  { keys: "A", action: "Approve: create a disabled Site (current row, or the selection)" },
  { keys: "R", action: "Reject" },
  { keys: "H", action: "Mark as file hoster" },
  { keys: "I", action: "Ignore" },
  { keys: "P", action: "Back to proposed (undoes an approval and deletes its unused disabled Site)" },
  { keys: "J / ↓", action: "Next candidate" },
  { keys: "K / ↑", action: "Previous candidate" },
  { keys: "X", action: "Select / unselect the current row" },
  { keys: "?", action: "Show / hide this help" },
  { keys: "Esc", action: "Close dialogs, clear the selection" },
];

const ACTION_KEYS: Record<string, CandidateAction> = {
  a: "approve",
  r: "reject",
  h: "hoster",
  i: "ignore",
  p: "reset",
};

function isEditable(target: EventTarget | null | undefined): boolean {
  if (!target || typeof target !== "object") return false;
  const element = target as { tagName?: unknown; isContentEditable?: unknown };
  const tag = typeof element.tagName === "string" ? element.tagName.toUpperCase() : "";
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || element.isContentEditable === true;
}

export function candidateKeyToCommand(event: KeyInput): CandidateCommand | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  if (event.key === "Escape") return { type: "escape" };
  if (isEditable(event.target)) return null;
  if (event.key === "ArrowDown") return { type: "next" };
  if (event.key === "ArrowUp") return { type: "prev" };
  if (event.key === "?") return { type: "help" };
  const key = event.key.length === 1 ? event.key.toLowerCase() : "";
  const action = ACTION_KEYS[key];
  if (action) return { type: "decide", action };
  if (key === "j") return { type: "next" };
  if (key === "k") return { type: "prev" };
  if (key === "x") return { type: "toggleSelect" };
  return null;
}

export interface CandidateState {
  /** Row ids in display order. */
  ids: number[];
  /** Position of the current row in `ids`. */
  index: number;
  selected: number[];
  /** Optimistic statuses of decided rows, until the list is reloaded. */
  statuses: Record<number, string>;
  /** Ids with a request in flight. */
  pending: number[];
}

export type CandidateEvent =
  | { type: "load"; ids: number[] }
  | { type: "next" }
  | { type: "prev" }
  | { type: "focus"; id: number }
  | { type: "toggle"; id: number }
  | { type: "toggleCurrent" }
  | { type: "selectAll" }
  | { type: "clearSelection" }
  | { type: "decided"; ids: number[]; status: string }
  | { type: "settled"; ids: number[] }
  | { type: "failed"; ids: number[]; previous: Record<number, string | undefined> };

export const initialCandidateState: CandidateState = { ids: [], index: 0, selected: [], statuses: {}, pending: [] };

function lastIndex(ids: number[]): number {
  return Math.max(ids.length - 1, 0);
}

function without(values: number[], removed: number[]): number[] {
  const drop = new Set(removed);
  return values.filter((value) => !drop.has(value));
}

export function candidateReducer(state: CandidateState, event: CandidateEvent): CandidateState {
  switch (event.type) {
    case "load": {
      const current = state.ids[state.index];
      const kept = current === undefined ? -1 : event.ids.indexOf(current);
      return {
        ...state,
        ids: event.ids,
        index: kept >= 0 ? kept : Math.min(state.index, lastIndex(event.ids)),
        selected: state.selected.filter((id) => event.ids.includes(id)),
      };
    }
    case "next":
      return { ...state, index: Math.min(state.index + 1, lastIndex(state.ids)) };
    case "prev":
      return { ...state, index: Math.max(state.index - 1, 0) };
    case "focus": {
      const index = state.ids.indexOf(event.id);
      return index < 0 ? state : { ...state, index };
    }
    case "toggle":
      if (!state.ids.includes(event.id)) return state;
      return {
        ...state,
        selected: state.selected.includes(event.id)
          ? state.selected.filter((id) => id !== event.id)
          : [...state.selected, event.id],
      };
    case "toggleCurrent": {
      const current = state.ids[state.index];
      return current === undefined ? state : candidateReducer(state, { type: "toggle", id: current });
    }
    case "selectAll":
      return { ...state, selected: [...state.ids] };
    case "clearSelection":
      return { ...state, selected: [] };
    case "decided": {
      const decided = new Set(event.ids);
      const statuses = { ...state.statuses };
      for (const id of event.ids) statuses[id] = event.status;
      let index = state.index;
      const current = state.ids[state.index];
      if (current !== undefined && decided.has(current)) {
        // Move on to the first row after the current one that this decision did not cover.
        const after = state.ids.findIndex((id, position) => position > state.index && !decided.has(id));
        if (after >= 0) index = after;
      }
      return {
        ...state,
        index,
        statuses,
        selected: [],
        pending: [...without(state.pending, event.ids), ...event.ids],
      };
    }
    case "settled":
      return { ...state, pending: without(state.pending, event.ids) };
    case "failed": {
      const failed = new Set(event.ids);
      const statuses: Record<number, string> = Object.fromEntries(
        Object.entries(state.statuses).filter(([id]) => !failed.has(Number(id))),
      );
      for (const id of event.ids) {
        const previous = event.previous[id];
        if (previous !== undefined) statuses[id] = previous;
      }
      return { ...state, statuses, pending: without(state.pending, event.ids) };
    }
  }
}

/** Rows an action applies to: the selection, or else the current row. */
export function targetIds(state: CandidateState): number[] {
  if (state.selected.length > 0) return state.selected.filter((id) => !state.pending.includes(id));
  const current = state.ids[state.index];
  return current === undefined || state.pending.includes(current) ? [] : [current];
}

export const ACTION_STATUS: Record<CandidateAction, string> = {
  approve: "approved",
  reject: "rejected",
  hoster: "hoster",
  ignore: "ignored",
  reset: "proposed",
};
