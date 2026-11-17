/**
 * The route tree's own rules (ADR 0036).
 *
 * Three things a reviewer would otherwise have to check by hand, and one of them is the reason the
 * migration can happen while the Vite SPA is still the rollback:
 *
 * 1. **Every screen is behind the session gate by construction.** A page outside the
 *    `(signed-in)` route group has no gate at all, so exactly five are allowed there and each is
 *    named: sign-in (ADR 0026), the invite redemption (ADR 0052), and the three mailed account
 *    actions (ADR 0054) — all reached by somebody who has no session, and in the reset's case
 *    cannot get one.
 * 2. **Every BFF route only reaches the customer API.** A handler that names another Django path
 *    would turn this server into a tunnel; `assertCustomerPath` refuses at runtime, and this
 *    refuses at build time.
 * 3. **The two apps do not drift apart.** Every `/app` route the Vite SPA still serves has a page
 *    here, so a customer screen added to the SPA while both exist cannot be quietly missing from
 *    the app that is about to replace it. When a route is added there, add it here — that is the
 *    point of the failure.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = path.resolve(import.meta.dirname, "..");
const APP = path.join(ROOT, "app");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

const files = walk(APP).map((file) => path.relative(APP, file));
const pages = files.filter((file) => path.basename(file) === "page.tsx");
const routeHandlers = files.filter((file) => path.basename(file) === "route.ts");

/** The URL a page file serves, route groups removed and `basePath` added. */
function urlOf(pageFile: string): string {
  const segments = path
    .dirname(pageFile)
    .split(path.sep)
    .filter((segment) => segment !== "." && !segment.startsWith("("));
  return `/app${segments.length ? `/${segments.join("/")}` : ""}`;
}

describe("the session gate", () => {
  it("found the pages", () => {
    expect(pages.length).toBeGreaterThan(5);
  });

  it("holds every page except the five that cannot be behind it", () => {
    const ungated = pages.filter(
      (file) => !file.startsWith(`(signed-in)${path.sep}`) && !file.startsWith("api" + path.sep),
    );
    // Five pages are outside the gate, and each for a reason that is written down. `sign-in` is the
    // one address a signed-out customer may reach (ADR 0026); `invite/[token]` is where somebody
    // with **no account yet** sets one up (ADR 0052); and the three of ADR 0054 are reached by
    // somebody who cannot sign in — that being the point of a password reset — or from a mail
    // opened in another browser. A sixth one fails this test, which is the point: "outside the
    // gate" is a decision, never a default.
    expect(ungated.sort()).toEqual(
      [
        path.join("forgot-password", "page.tsx"),
        path.join("invite", "[token]", "page.tsx"),
        path.join("reset-password", "[token]", "page.tsx"),
        path.join("sign-in", "page.tsx"),
        path.join("verify-email", "[token]", "page.tsx"),
      ].sort(),
    );
  });

  it("is the layout of that group, and asks Django", () => {
    const layout = readFileSync(path.join(APP, "(signed-in)", "layout.tsx"), "utf8");
    expect(layout).toMatch(/readAccount/);
    expect(layout).toMatch(/redirect\("\/sign-in"\)/);
    // An unreachable Django is a sentence, not a 500 and never an empty overview: "nothing was
    // found" is a statement about the customer's work and only Django may make it.
    expect(layout).toMatch(/common\.unreachable/);
  });

  it("lets no page throw on a failed read, because a throw is a bare 500", () => {
    // `required()` used to rethrow anything that was not a 404; every page now uses `optional()`
    // or `found()`, both of which answer with null.
    for (const file of pages) {
      const source = readFileSync(path.join(APP, file), "utf8");
      expect(source, file).not.toMatch(/\brequired\(/);
    }
  });
});

describe("the BFF routes", () => {
  it("found the handlers", () => {
    expect(routeHandlers.length).toBeGreaterThan(8);
  });

  it.each(routeHandlers)("%s relays only the customer API", (file) => {
    const source = readFileSync(path.join(APP, file), "utf8");
    const relayed = [...source.matchAll(/relay\(\s*request,\s*[`"]([^`"]+)/g)].map(
      (match) => match[1] ?? "",
    );
    expect(relayed.length).toBeGreaterThan(0);
    for (const target of relayed) expect(target.startsWith("/api/v1/app/")).toBe(true);
  });
});

describe("every write an island sends has a handler", () => {
  // The islands address the BFF by string (`/app/api/notices/${id}/approve`). A typo there is a
  // 404 the customer sees as "something went wrong", and no typecheck can see inside the string —
  // so each one is matched against the route files that exist.
  const handlerUrls = new Set(
    routeHandlers.map((file) => {
      const segments = path
        .dirname(file)
        .split(path.sep)
        .map((segment) => (segment.startsWith("[") ? ":param" : segment));
      return `/app/${segments.join("/")}`;
    }),
  );

  const sources = walk(ROOT).filter(
    (file) =>
      /\.tsx?$/.test(file) &&
      !file.includes(".test.") &&
      /^(components|screens|lib)\//.test(path.relative(ROOT, file)),
  );

  /**
   * Every `/app/api/…` path a source names, with `${…}` reduced to a parameter. A path a prose
   * comment mentions (`/app/api/rights/…`) is not one the code sends, so only real URL characters
   * count.
   */
  function bffPaths(source: string): string[] {
    return [...source.matchAll(/["`](\/app\/api\/[^"`]*)["`]/g)]
      .map((match) => (match[1] ?? "").replace(/\$\{[^}]*\}/g, ":param").replace(/\/$/, ""))
      .filter((url) => /^[/a-z0-9:_.-]+$/.test(url));
  }

  it("found both sides", () => {
    expect(handlerUrls.size).toBeGreaterThan(8);
    expect(sources.flatMap((file) => bffPaths(readFileSync(file, "utf8"))).length).toBeGreaterThan(8);
  });

  it.each(sources)("%s names only existing handlers", (file) => {
    const used = bffPaths(readFileSync(file, "utf8"));
    const missing = used.filter((url) => !handlerUrls.has(url));
    expect(missing, `handlers: ${[...handlerUrls].sort().join(", ")}`).toEqual([]);
  });
});

describe("nothing links outside the customer app", () => {
  // The staff screens are on the development VPS and every staff API answers 404 on production
  // (ADR 0028), so a staff link would work in development and break in production — the exact
  // failure ADR 0026 warned about. `next/link`'s hrefs are relative to `basePath`; a plain `<a>`
  // carries it. Absolute URLs are eslint's job; this is the relative half.
  const sources = walk(ROOT).filter(
    (file) =>
      /\.(ts|tsx)$/.test(file) &&
      !file.includes(".test.") &&
      !file.includes("node_modules") &&
      !file.includes(".next") &&
      /^(app|components|screens|lib)\//.test(path.relative(ROOT, file)),
  );

  it("found the sources", () => {
    expect(sources.length).toBeGreaterThan(20);
  });

  it.each(sources)("%s links only within /app", (file) => {
    const source = readFileSync(file, "utf8");
    const plain = [...source.matchAll(/href="(\/[^"]*)"/g)].map((match) => match[1] ?? "");
    const fetched = [...source.matchAll(/fetch\("(\/[^"]*)"/g)].map((match) => match[1] ?? "");
    for (const target of [...plain, ...fetched]) {
      expect(target.startsWith("/app"), `${file}: ${target}`).toBe(true);
    }
  });
});

describe("parity with the Vite SPA while both serve /app", () => {
  // The SPA's customer routes, read out of its code-based router: `path: "/app/..."` and the
  // children of `appRoute` (`path: "originals/$originalId"` and the like).
  const router = readFileSync(path.resolve(ROOT, "..", "src", "router.tsx"), "utf8");
  const spaRoutes = new Set<string>();
  for (const match of router.matchAll(/getParentRoute:\s*\(\)\s*=>\s*appRoute,\s*path:\s*"([^"]*)"/g)) {
    const raw = match[1] ?? "";
    // TanStack writes parameters as `$name`; this app writes them as `[name]`.
    const normalised = raw
      .split("/")
      .filter((segment) => segment !== "" && segment !== "/")
      .map((segment) => (segment.startsWith("$") ? `[${segment.slice(1)}]` : segment))
      .join("/");
    spaRoutes.add(normalised === "" ? "/app" : `/app/${normalised}`);
  }

  it("read the SPA's customer routes", () => {
    expect(spaRoutes.size).toBeGreaterThan(5);
  });

  it("has a page for each of them", () => {
    const here = new Set(pages.map(urlOf));
    const missing = [...spaRoutes].filter((route) => !here.has(route)).sort();
    expect(missing).toEqual([]);
  });
});
