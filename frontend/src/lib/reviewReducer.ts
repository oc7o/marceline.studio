/**
 * Review flow state, independent of how items are fetched.
 *
 * Items live in TanStack Query; this reducer keeps the cursor position, local status overrides
 * (decisions are applied optimistically), the selection and the undo history. Undo payloads are
 * whatever a screen needs to reset a decision server-side (e.g. the changed leak/match ids).
 */

export type ItemStatus = "proposed" | "confirmed" | "rejected" | "conflict";
export type FinalDecision = "confirm" | "reject";

export const DECISION_STATUS: Record<FinalDecision, ItemStatus> = {
  confirm: "confirmed",
  reject: "rejected",
};

export interface DecisionRecord<U> {
  id: number;
  keys: string[];
  decision: FinalDecision;
  previous: Record<string, ItemStatus>;
  index: number;
  /** Undo payload; null while the request is still pending. */
  undo: U | null;
}

export interface ReviewState<U> {
  index: number;
  overrides: Record<string, ItemStatus>;
  selected: string[];
  anchor: string | null;
  history: DecisionRecord<U>[];
}

export type ReviewAction<U> =
  | { type: "next"; total: number }
  | { type: "prev" }
  | { type: "goto"; index: number; total: number }
  | {
      type: "decided";
      id: number;
      keys: string[];
      decision: FinalDecision;
      previous: Record<string, ItemStatus>;
      /** Move to the next item (single-item decisions); bulk decisions clear the selection. */
      advance: boolean;
      total: number;
    }
  | { type: "settled"; id: number; undo: U }
  | { type: "failed"; id: number }
  | { type: "undone" }
  | { type: "setStatus"; keys: string[]; status: ItemStatus }
  | { type: "toggleSelect"; key: string }
  | { type: "selectRange"; order: string[]; key: string }
  | { type: "clearSelection" };

export const HISTORY_LIMIT = 50;

export function initialReviewState<U>(): ReviewState<U> {
  return { index: 0, overrides: {}, selected: [], anchor: null, history: [] };
}

/** `total` itself is a valid index: the "end of the loaded queue" position. */
function clampIndex(index: number, total: number): number {
  return Math.max(0, Math.min(index, Math.max(total, 0)));
}

function restore<U>(
  overrides: Record<string, ItemStatus>,
  record: DecisionRecord<U>,
): Record<string, ItemStatus> {
  const untouched = Object.entries(overrides).filter(([key]) => !record.keys.includes(key));
  const previous = record.keys.flatMap((key) => {
    const status = record.previous[key];
    return status === undefined ? [] : [[key, status] as const];
  });
  return Object.fromEntries([...untouched, ...previous]);
}

export function reviewReducer<U>(state: ReviewState<U>, action: ReviewAction<U>): ReviewState<U> {
  switch (action.type) {
    case "next":
      return { ...state, index: clampIndex(state.index + 1, action.total) };
    case "prev":
      return { ...state, index: Math.max(0, state.index - 1) };
    case "goto":
      return { ...state, index: clampIndex(action.index, action.total) };
    case "decided": {
      const status = DECISION_STATUS[action.decision];
      const overrides = { ...state.overrides };
      for (const key of action.keys) overrides[key] = status;
      const record: DecisionRecord<U> = {
        id: action.id,
        keys: action.keys,
        decision: action.decision,
        previous: action.previous,
        index: state.index,
        undo: null,
      };
      return {
        ...state,
        overrides,
        index: action.advance ? clampIndex(state.index + 1, action.total) : state.index,
        selected: action.advance ? state.selected : [],
        anchor: action.advance ? state.anchor : null,
        history: [...state.history, record].slice(-HISTORY_LIMIT),
      };
    }
    case "settled":
      return {
        ...state,
        history: state.history.map((record) =>
          record.id === action.id ? { ...record, undo: action.undo } : record,
        ),
      };
    case "failed": {
      const record = state.history.find((item) => item.id === action.id);
      if (!record) return state;
      return {
        ...state,
        overrides: restore(state.overrides, record),
        index: record.index,
        history: state.history.filter((item) => item.id !== action.id),
      };
    }
    case "undone": {
      const record = state.history.at(-1);
      if (!record || record.undo === null) return state;
      return {
        ...state,
        overrides: restore(state.overrides, record),
        index: record.index,
        history: state.history.slice(0, -1),
      };
    }
    case "setStatus": {
      const overrides = { ...state.overrides };
      for (const key of action.keys) overrides[key] = action.status;
      return { ...state, overrides };
    }
    case "toggleSelect": {
      const selected = state.selected.includes(action.key)
        ? state.selected.filter((key) => key !== action.key)
        : [...state.selected, action.key];
      return { ...state, selected, anchor: action.key };
    }
    case "selectRange": {
      const end = action.order.indexOf(action.key);
      const start = state.anchor === null ? -1 : action.order.indexOf(state.anchor);
      if (end < 0) return state;
      if (start < 0) return reviewReducer(state, { type: "toggleSelect", key: action.key });
      const [from, to] = start < end ? [start, end] : [end, start];
      const range = action.order.slice(from, to + 1);
      const selected = [...state.selected, ...range.filter((key) => !state.selected.includes(key))];
      return { ...state, selected, anchor: action.key };
    }
    case "clearSelection":
      return { ...state, selected: [], anchor: null };
  }
}

/** The last decision that can be undone now (settled: the server accepted it). */
export function undoable<U>(state: ReviewState<U>): DecisionRecord<U> | null {
  const record = state.history.at(-1);
  return record && record.undo !== null ? record : null;
}

export function statusOf<U>(
  state: ReviewState<U>,
  item: { key: string; status: ItemStatus },
): ItemStatus {
  return state.overrides[item.key] ?? item.status;
}
