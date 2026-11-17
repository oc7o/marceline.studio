import { copyFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const here = import.meta.dirname;

/*
  frontend/vite.config.ts's policy, widened by exactly two sources, neither of which is a request:
  - 'wasm-unsafe-eval': "Add an original" compiles the PDQ hasher (WebAssembly embedded in the bundle);
  - media-src blob: — the same screen decodes the visitor's own video from an object URL.
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

/*
  GitHub Pages has no rewrite rules. It answers an unknown path with /404.html, so that file is the
  app itself: the router reads the address it was asked for and the deep link survives a reload.
  /app/index.html is the same page again, so the address most visitors arrive at answers 200.
*/
function spaFallback(): Plugin {
  return {
    name: "demo-spa-fallback",
    apply: "build",
    closeBundle() {
      const dist = resolve(here, "dist");
      copyFileSync(resolve(dist, "index.html"), resolve(dist, "404.html"));
      mkdirSync(resolve(dist, "app"), { recursive: true });
      copyFileSync(resolve(dist, "index.html"), resolve(dist, "app/index.html"));
    },
  };
}

// The screens live in ../frontend/src, which has no node_modules of its own: every package they
// import is resolved from this project instead.
const SHARED_PACKAGES = [
  "@tanstack/react-query",
  "@tanstack/react-router",
  "i18next",
  "openapi-fetch",
  "react",
  "react-dom",
  "react-i18next",
];

export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss(), contentSecurityPolicy(), spaFallback()],
  resolve: {
    dedupe: SHARED_PACKAGES,
    alias: [{ find: /^tailwindcss$/, replacement: resolve(here, "node_modules/tailwindcss/index.css") }],
  },
  server: { host: "127.0.0.1", port: 5174, fs: { allow: [resolve(here, "..")] } },
  build: { sourcemap: false },
});
