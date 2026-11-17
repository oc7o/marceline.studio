import { describe, expect, it } from "vitest";

import { OWNER_UNASSIGNED, OWNER_UNCHOSEN, ownerChoice } from "./ownerChoice";

describe("ownerChoice", () => {
  it("blocks the upload until an owner is chosen", () => {
    expect(ownerChoice(OWNER_UNCHOSEN)).toBeUndefined();
  });

  it("sends null for an explicitly unassigned original", () => {
    expect(ownerChoice(OWNER_UNASSIGNED)).toBeNull();
  });

  it("sends the organisation id", () => {
    expect(ownerChoice("7")).toBe(7);
  });

  it("never turns a malformed value into an owner", () => {
    expect(ownerChoice("0")).toBeUndefined();
    expect(ownerChoice("-3")).toBeUndefined();
    expect(ownerChoice("1.5")).toBeUndefined();
    expect(ownerChoice("abc")).toBeUndefined();
  });
});
