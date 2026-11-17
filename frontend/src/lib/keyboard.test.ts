import { describe, expect, it } from "vitest";

import { keyToCommand } from "./keyboard";

describe("keyToCommand", () => {
  it.each([
    ["y", "confirm"],
    ["Y", "confirm"],
    ["n", "reject"],
    ["s", "skip"],
    ["ArrowRight", "skip"],
    ["ArrowLeft", "back"],
    ["u", "undo"],
    ["o", "open"],
    ["x", "toggleSelect"],
    ["b", "band"],
    ["B", "band"],
    ["?", "help"],
    ["Escape", "escape"],
  ])("maps %s to %s", (key, type) => {
    expect(keyToCommand({ key })).toEqual({ type });
  });

  it("maps digits to reference slots", () => {
    expect(keyToCommand({ key: "3" })).toEqual({ type: "reference", slot: 3 });
    expect(keyToCommand({ key: "0" })).toEqual({ type: "reference", slot: 0 });
  });

  it("ignores unknown keys and modifier combinations", () => {
    expect(keyToCommand({ key: "q" })).toBeNull();
    expect(keyToCommand({ key: "y", ctrlKey: true })).toBeNull();
    expect(keyToCommand({ key: "n", metaKey: true })).toBeNull();
    expect(keyToCommand({ key: "u", altKey: true })).toBeNull();
  });

  it("ignores typing in form fields but still closes with Escape", () => {
    const input = { tagName: "INPUT" } as unknown as EventTarget;
    const textarea = { tagName: "textarea" } as unknown as EventTarget;
    expect(keyToCommand({ key: "y", target: input })).toBeNull();
    expect(keyToCommand({ key: "1", target: textarea })).toBeNull();
    expect(keyToCommand({ key: "Escape", target: input })).toEqual({ type: "escape" });
  });
});
