import { describe, expect, it } from "vitest";

import { claimReasonLabel } from "./claimReasons";

describe("claimReasonLabel", () => {
  it("labels every reason code the API sends", () => {
    expect(claimReasonLabel("owned_by_other")).toBe("Same file, owned by another organisation");
    expect(claimReasonLabel("unowned_existing")).toBe("Same file, registered but owned by nobody");
    expect(claimReasonLabel("hash_overlap")).toBe("Hashes largely duplicate another original");
  });

  it("falls back to the raw code for anything unknown", () => {
    expect(claimReasonLabel("something_new")).toBe("something_new");
  });
});
