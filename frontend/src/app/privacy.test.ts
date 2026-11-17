/**
 * The privacy rule for the customer UI, checked against the source of the customer UI itself.
 *
 * Issue #53 (pirate thumbnails) is undecided, so the customer UI shows no copy of the
 * material in any form — and, more strongly, contains nothing *able* to load a remote image. The
 * eslint config bans the same constructs while linting; this test states the ban in a place that
 * survives an eslint config change, and covers what a linter cannot see (a CSS `url()` in a class
 * string, a media path in a template literal).
 *
 * If a screen ever legitimately needs an image, it does not get one by relaxing this list: it gets
 * a component that takes an id for our own passthrough and cannot express any other URL, and this
 * test gets amended to say exactly that.
 */
import { describe, expect, it } from "vitest";

// The customer UI's own source, tests excluded — a test that names a forbidden construct in order
// to forbid it would otherwise fail itself.
const sources = import.meta.glob<string>(
  ["./**/*.ts", "./**/*.tsx", "!./**/*.test.ts", "!./**/*.test.tsx"],
  { eager: true, query: "?raw", import: "default" },
);

const FORBIDDEN: { pattern: RegExp; what: string }[] = [
  {
    pattern: /<\s*(img|image|picture|source|video|audio|iframe|embed|object|canvas)[\s/>]/i,
    what: "a media element",
  },
  { pattern: /\b(src|srcSet|poster)\s*=\s*[{"']/, what: "a media source attribute" },
  { pattern: /\bmediaImageUrl\b/, what: "the media passthrough helper" },
  // Lowercase only: this is CSS `url(...)`, not JavaScript's `new URL(...)`.
  { pattern: /\burl\(/, what: "a CSS url()" },
  { pattern: /\bbackgroundImage\b/, what: "a CSS background image" },
  { pattern: /new\s+Image\b/, what: "an Image object" },
  { pattern: /\/api\/v1\/media\//, what: "the media endpoint" },
  { pattern: /\bMediaGrid\b/, what: "the staff media grid" },
];

describe("the customer UI cannot show a pirated copy", () => {
  it("has customer sources to check", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(3);
  });

  it.each(Object.entries(sources))("%s loads no remote media", (_file, source) => {
    const found = FORBIDDEN.filter((rule) => rule.pattern.test(source)).map((rule) => rule.what);
    expect(found).toEqual([]);
  });
});
