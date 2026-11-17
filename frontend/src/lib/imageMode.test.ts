import { describe, expect, it } from "vitest";

import { DEFAULT_IMAGE_MODE, IMAGE_MODE_KEY, readImageMode, writeImageMode } from "./imageMode";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    values,
  };
}

describe("image mode", () => {
  it("defaults to privacy", () => {
    expect(DEFAULT_IMAGE_MODE).toBe("privacy");
    expect(readImageMode(memoryStorage())).toBe("privacy");
    expect(readImageMode(null)).toBe("privacy");
  });

  it("falls back to privacy for unknown values and blocked storage", () => {
    expect(readImageMode(memoryStorage({ [IMAGE_MODE_KEY]: "yes" }))).toBe("privacy");
    const blocked = {
      getItem: () => {
        throw new Error("SecurityError");
      },
    };
    expect(readImageMode(blocked)).toBe("privacy");
  });

  it("remembers load mode", () => {
    const storage = memoryStorage();
    writeImageMode(storage, "load");
    expect(storage.values.get(IMAGE_MODE_KEY)).toBe("load");
    expect(readImageMode(storage)).toBe("load");
  });
});
