/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

// Hosts the dev server answers to. The operator reaches the server over Tailscale.
const ALLOWED_HOSTS = ["dev.example.internal", "localhost", "127.0.0.1"];

// Production builds pin every fetch and image to our own origin: no hotlinks, no CDNs.
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "img-src 'self'",
  "connect-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "marceline-csp",
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

export default defineConfig(({ mode }) => {
  // WEB_BIND comes from the repo's .env (the same address compose publishes Django on).
  const env = { ...loadEnv(mode, "..", ""), ...process.env };
  const bind = env.WEB_BIND || "127.0.0.1";
  const apiTarget = env.API_PROXY_TARGET || `http://${bind}:8000`;

  const server = {
    host: bind,
    port: 5173,
    strictPort: true,
    allowedHosts: ALLOWED_HOSTS,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        // Dev only: the browser's Origin is the Vite host, Django compares it with the proxied
        // Host. The CSRF token check itself is unchanged.
        configure: (proxy: { on: (event: "proxyReq", cb: (req: ProxyRequest) => void) => void }) => {
          proxy.on("proxyReq", (proxyReq) => {
            if (proxyReq.getHeader("origin")) proxyReq.setHeader("origin", apiTarget);
          });
        },
      },
    },
  };

  return {
    plugins: [react(), tailwindcss(), contentSecurityPolicy()],
    server,
    preview: server,
    // The manifest drives scripts/check-bundle-budget.mjs (ADR 0028's performance budget, part of
    // `make frontend-check`): it needs each chunk's real dependency graph, not a guess from names.
    build: { sourcemap: false, manifest: true },
    test: {
      include: ["src/**/*.test.{ts,tsx}"],
      environment: "node",
      // Raises Testing Library's default async-query timeout project-wide (issue #122); see the
      // file's own comment for why a lazily routed screen needs it under a loaded check.
      setupFiles: ["./src/vitest.setup.ts"],
    },
  };
});

interface ProxyRequest {
  getHeader(name: string): unknown;
  setHeader(name: string, value: string): void;
}
