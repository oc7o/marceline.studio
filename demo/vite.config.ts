import { copyFileSync, cpSync, createReadStream, existsSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

import { findings, originals } from "./src/mock/state";

const here = import.meta.dirname;
const customer = resolve(here, "../frontend/customer");
const shared = resolve(here, "../frontend/src");
const runtime = resolve(here, "src/runtime");
const fonts = resolve(customer, "public/fonts");

/*
  As strict as the app allows, with two sources that are not requests:
  - 'wasm-unsafe-eval': "Add an original" compiles the PDQ hasher (WebAssembly embedded in the bundle);
  - media-src blob: — the same screen decodes the visitor's own video from an object URL.
  The two links in the banner are navigations, which no directive here restricts.
*/
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "img-src 'self'",
  "media-src 'self' blob:",
  "connect-src 'self'",
  "script-src 'self' 'wasm-unsafe-eval'",
  "style-src 'self'",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
].join("; ");

/**
 * The one module of the app the demo replaces: `lib/django.ts`, the only place the app talks to
 * Django. Everything that imports it — the reads, the pages, the route handlers — gets
 * `src/runtime/django.ts` instead, and stays as it is.
 */
function swapDataLayer(): Plugin {
  const real = resolve(customer, "lib/django");
  return {
    name: "demo-swap-data-layer",
    enforce: "pre",
    resolveId(source, importer) {
      if (!importer || !source.startsWith(".")) return null;
      return resolve(dirname(importer), source) === real ? resolve(runtime, "django.ts") : null;
    },
  };
}

/**
 * The typefaces are the app's own files, served where its stylesheet asks for them
 * (`/app/fonts/…`, with LICENSES.txt beside them) rather than kept here as a second copy. The body
 * face is preloaded, as the app's root layout does.
 */
function appFonts(): Plugin {
  const body = readdirSync(fonts).find((name) => /^instrument-sans-latin\.[0-9a-f]+\.woff2$/.test(name));
  return {
    name: "demo-app-fonts",
    configureServer(server) {
      server.middlewares.use("/app/fonts", (request, response, next) => {
        const file = join(fonts, (request.url ?? "").split("?")[0] ?? "");
        if (!file.startsWith(fonts) || !existsSync(file)) return next();
        response.setHeader("content-type", file.endsWith(".woff2") ? "font/woff2" : "text/plain; charset=utf-8");
        createReadStream(file).pipe(response);
      });
    },
    transformIndexHtml: () =>
      body
        ? [
            {
              tag: "link",
              attrs: { rel: "preload", href: `/app/fonts/${body}`, as: "font", type: "font/woff2", crossorigin: "anonymous" },
              injectTo: "head",
            },
          ]
        : [],
    closeBundle() {
      cpSync(fonts, resolve(here, "dist/app/fonts"), { recursive: true });
    },
  };
}

/*
  Sentences of the app's dictionaries that belong to two flows the demo does not have — redeeming an
  invite, and sending a file to a server with its declaration — and that have no place on a public
  sample page. They are on no screen here; this keeps them out of the files the page downloads as
  well. A key that is no longer found fails the build rather than quietly shipping again.
*/
const UNUSED_SENTENCES: Record<string, string[]> = {
  "addOriginal.ts": ["solePerformer", "article9Consent"],
  "onboarding.ts": ["ageHeading", "ageLead", "ageTick", "ageRequired"],
};

function leaveOutUnusedSentences(): Plugin {
  return {
    name: "demo-unused-sentences",
    enforce: "pre",
    transform(code, id) {
      const [file] = id.split("?");
      const match = /\/frontend\/src\/i18n\/(?:en|de)\/([A-Za-z]+\.ts)$/.exec(file ?? "");
      const keys = match?.[1] ? UNUSED_SENTENCES[match[1]] : undefined;
      if (!keys) return null;
      let out = code;
      for (const key of keys) {
        const value = new RegExp(`(\\b${key}:\\s*)(?:"(?:[^"\\\\]|\\\\.)*"|\`(?:[^\`\\\\]|\\\\.)*\`)`);
        if (!value.test(out)) this.error(`${id}: no string under "${key}" to leave out`);
        out = out.replace(value, '$1""');
      }
      return { code: out, map: null };
    },
  };
}

function contentSecurityPolicy(): Plugin {
  return {
    name: "demo-csp",
    apply: "build",
    transformIndexHtml: () => [
      {
        tag: "meta",
        attrs: { "http-equiv": "Content-Security-Policy", content: CONTENT_SECURITY_POLICY },
        injectTo: "head-prepend",
      },
    ],
  };
}

/**
 * `/app/export` is where the app sends a download that failed, to say why. Nothing leads there in
 * the demo, so it gets no address of its own; typed by hand it is answered like any other unknown
 * address, by the app itself.
 */
const NOT_A_DESTINATION = new Set(["export"]);

/** `app/(signed-in)/removals/page.tsx` -> `removals`; a route with a `[param]` has no fixed address. */
function staticRoutes(directory: string, prefix: string[] = []): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (entry.name === "api" || entry.name.startsWith("[") || NOT_A_DESTINATION.has(entry.name)) continue;
      const group = /^\(.*\)$/.test(entry.name);
      found.push(...staticRoutes(join(directory, entry.name), group ? prefix : [...prefix, entry.name]));
    } else if (entry.name === "page.tsx") {
      found.push(prefix.join("/"));
    }
  }
  return found;
}

/** The addresses of the sample rows: each original, its notices, and each copy's record. */
function sampleRoutes(): string[] {
  return [
    ...originals.flatMap(({ id }) => [`originals/${id}`, `originals/${id}/notices`]),
    ...findings.map((row) => `originals/${row.original_id}/copies/${row.page_id}`),
  ];
}

/*
  GitHub Pages has no rewrite rules. Every address is the same document, and the router reads the
  address it was asked for: `/` (which leads into the app), `/app`, each fixed route of the app and
  each sample row's own address answer 200 from a copy of it. Anything else — an original added in
  the tab, a mailed link, a typo — is answered with /404.html, the same document again, so the app
  still opens and says what it would say.
*/
function everyAddress(): Plugin {
  return {
    name: "demo-every-address",
    apply: "build",
    closeBundle() {
      const dist = resolve(here, "dist");
      const document = resolve(dist, "index.html");
      copyFileSync(document, resolve(dist, "404.html"));
      for (const route of [...staticRoutes(resolve(customer, "app")), ...sampleRoutes()]) {
        const directory = resolve(dist, "app", route);
        mkdirSync(directory, { recursive: true });
        copyFileSync(document, resolve(directory, "index.html"));
      }
    },
  };
}

export default defineConfig({
  base: "/",
  plugins: [swapDataLayer(), leaveOutUnusedSentences(), react(), tailwindcss(), appFonts(), contentSecurityPolicy(), everyAddress()],
  resolve: {
    // The screens live in ../frontend, which has no node_modules on a fresh clone: every package
    // they import is resolved from this project instead.
    dedupe: ["react", "react-dom"],
    alias: [
      { find: /^tailwindcss$/, replacement: resolve(here, "node_modules/tailwindcss/index.css") },
      { find: /^@shared\//, replacement: `${shared}/` },
      { find: /^next\/link$/, replacement: resolve(runtime, "next-link.tsx") },
      { find: /^next\/navigation$/, replacement: resolve(runtime, "next-navigation.ts") },
      { find: /^next\/headers$/, replacement: resolve(runtime, "next-headers.ts") },
      { find: /^server-only$/, replacement: resolve(runtime, "server-only.ts") },
    ],
  },
  server: { host: "127.0.0.1", port: 5174, fs: { allow: [resolve(here, "..")] } },
  // The app's own files live under /app, as in production; only the entry documents sit outside.
  build: { sourcemap: false, assetsDir: "app/assets" },
});
