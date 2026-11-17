/**
 * The one proxy — Next 16's name for what 15 called middleware — and it does exactly one thing: a
 * per-request CSP nonce (ADR 0036).
 *
 * **No authorisation happens here or anywhere near here.** CVE-2025-29927 let a crafted header
 * skip Next.js middleware entirely; the rename does not change that risk, so nothing that decides
 * who may see what is allowed to live in this file: Django decides access on every call, and the
 * signed-in layout asks Django, not this.
 *
 * A nonce is needed because the App Router streams its payload through inline `<script>` elements.
 * Without one the customer page would need `script-src 'unsafe-inline'`, which is a step back from
 * what ADR 0028 set for the SPA. Next reads the nonce out of the request's own CSP header and puts
 * it on every script it emits; `'strict-dynamic'` then covers the chunks those scripts load.
 *
 * The file is `proxy.ts` and the export is `proxy` because Next 16 deprecated the `middleware`
 * convention (`npx @next/codemod middleware-to-proxy`). Same mechanism, same position in the
 * request path, same rule about what may not live here.
 */
import { NextResponse, type NextRequest } from "next/server";

export const config = {
  // Everything but the immutable build output, which is plain files and carries no script of ours.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

export function proxy(request: NextRequest): NextResponse {
  const nonce = crypto.randomUUID().replaceAll("-", "");
  const policy = [
    "default-src 'self'",
    // The customer UI renders no images at all (issue #53). Not 'self' — none.
    "img-src 'none'",
    // `wasm-unsafe-eval` is required to instantiate WebAssembly, and "add an original" hashes
    // frames with Meta's PDQ compiled to WASM (ADR 0017). It permits WebAssembly only — not
    // `eval` — and without it the fingerprinting fails in the browser rather than in a test.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' 'wasm-unsafe-eval'${
      process.env.NODE_ENV === "production" ? "" : " 'unsafe-eval'"
    }`,
    // The fingerprinting Worker is a chunk of ours; `strict-dynamic` would otherwise drop `self`.
    "worker-src 'self' blob:",
    // The chosen video is decoded from a `blob:` URL of the local file, in a `<video>` element
    // this app never renders but the fingerprinting code creates (`@shared/lib/fingerprintClient`).
    // Nothing remote: `blob:` is this browser's own memory, and the file never leaves it.
    "media-src blob:",
    // React and Next inline the critical stylesheet; there is no third-party stylesheet.
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("content-security-policy", policy);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", policy);
  return response;
}
