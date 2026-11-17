import { describe, expect, it } from "vitest";

import { pageFromParam, pageHref, pageLink, paginate } from "./pagination";

describe("paginate", () => {
  const items = Array.from({ length: 45 }, (_, index) => index + 1);

  it("takes the first page by default shape", () => {
    const page = paginate(items, 1, 20);
    expect(page.items).toEqual(items.slice(0, 20));
    expect(page).toMatchObject({ page: 1, pageCount: 3, start: 1, end: 20, total: 45 });
  });

  it("takes the last, short page", () => {
    const page = paginate(items, 3, 20);
    expect(page.items).toEqual(items.slice(40, 45));
    expect(page).toMatchObject({ page: 3, pageCount: 3, start: 41, end: 45, total: 45 });
  });

  it("clamps a page past the end to the last real page", () => {
    const page = paginate(items, 99, 20);
    expect(page.page).toBe(3);
    expect(page.items).toEqual(items.slice(40, 45));
  });

  it("clamps a page below 1 to the first page", () => {
    expect(paginate(items, 0, 20).page).toBe(1);
    expect(paginate(items, -5, 20).page).toBe(1);
  });

  it("is one page of everything when the list fits", () => {
    const page = paginate(items.slice(0, 5), 1, 20);
    expect(page).toMatchObject({ pageCount: 1, start: 1, end: 5, total: 5 });
  });

  it("is page 1 of nothing for an empty list, never a division by zero", () => {
    const page = paginate([], 1, 20);
    expect(page).toMatchObject({ items: [], page: 1, pageCount: 1, start: 0, end: 0, total: 0 });
  });
});

describe("pageFromParam", () => {
  it("reads a whole number", () => {
    expect(pageFromParam("3")).toBe(3);
  });

  it("takes the first value of a repeated query parameter", () => {
    expect(pageFromParam(["2", "9"])).toBe(2);
  });

  it("defaults to 1 for anything that is not a positive whole number", () => {
    expect(pageFromParam(undefined)).toBe(1);
    expect(pageFromParam("")).toBe(1);
    expect(pageFromParam("0")).toBe(1);
    expect(pageFromParam("-1")).toBe(1);
    expect(pageFromParam("2.5")).toBe(2);
    expect(pageFromParam("banana")).toBe(1);
  });
});

describe("pageHref", () => {
  it("is the bare path for page 1", () => {
    expect(pageHref("/app/removals", 1)).toBe("/app/removals");
  });

  it("carries the page number otherwise", () => {
    expect(pageHref("/app/removals", 2)).toBe("/app/removals?page=2");
  });

  it("is undefined when there is no such page — the dead end of a Pagination step", () => {
    expect(pageHref("/app/removals", null)).toBeUndefined();
  });
});

describe("pageLink", () => {
  it("carries the href when there is a page", () => {
    expect(pageLink("More", "/app?page=2")).toEqual({ label: "More", href: "/app?page=2" });
  });

  it("has no href key at all when there is no page — never href: undefined", () => {
    const link = pageLink("Back", undefined);
    expect(link).toEqual({ label: "Back" });
    expect("href" in link).toBe(false);
  });
});
