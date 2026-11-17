/** Keyboard map of the review screens. Pure: event in, command out. */

export type Command =
  | { type: "confirm" }
  | { type: "reject" }
  | { type: "skip" }
  | { type: "back" }
  | { type: "undo" }
  | { type: "open" }
  | { type: "toggleSelect" }
  | { type: "help" }
  | { type: "escape" }
  /** Reference queue: cycle the confidence band filter. */
  | { type: "band" }
  /** 0 = all references, 1-9 = the n-th reference in the filter list. */
  | { type: "reference"; slot: number };

export interface KeyInput {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  target?: EventTarget | null;
}

export const SHORTCUTS: ReadonlyArray<{ keys: string; action: string }> = [
  { keys: "Y", action: "Confirm (current item, or the selection)" },
  { keys: "N", action: "Reject (current item, or the selection)" },
  { keys: "S / →", action: "Skip to the next item" },
  { keys: "←", action: "Back to the previous item" },
  { keys: "U", action: "Undo the last decision" },
  { keys: "O", action: "Open the page URL in a new tab" },
  { keys: "X", action: "Select / unselect the current item (shift-click rows for a range)" },
  { keys: "1 – 9", action: "Filter by the n-th reference (0 = all references)" },
  { keys: "B", action: "Cycle the confidence band filter: high + review → high → review → low" },
  { keys: "?", action: "Show / hide this help" },
  { keys: "Esc", action: "Close dialogs, clear the selection" },
];

function isEditable(target: EventTarget | null | undefined): boolean {
  if (!target || typeof target !== "object") return false;
  const element = target as { tagName?: unknown; isContentEditable?: unknown };
  const tag = typeof element.tagName === "string" ? element.tagName.toUpperCase() : "";
  return (
    tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || element.isContentEditable === true
  );
}

export function keyToCommand(event: KeyInput): Command | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  if (event.key === "Escape") return { type: "escape" };
  if (isEditable(event.target)) return null;
  switch (event.key) {
    case "y":
    case "Y":
      return { type: "confirm" };
    case "n":
    case "N":
      return { type: "reject" };
    case "s":
    case "S":
    case "ArrowRight":
      return { type: "skip" };
    case "ArrowLeft":
      return { type: "back" };
    case "u":
    case "U":
      return { type: "undo" };
    case "o":
    case "O":
      return { type: "open" };
    case "x":
    case "X":
      return { type: "toggleSelect" };
    case "b":
    case "B":
      return { type: "band" };
    case "?":
      return { type: "help" };
  }
  if (/^[0-9]$/.test(event.key)) return { type: "reference", slot: Number(event.key) };
  return null;
}
