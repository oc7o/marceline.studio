/**
 * The rules of the backend-for-frontend (ADR 0036), as pure functions over headers.
 *
 * `django.ts` does the fetching; everything that can be decided without a network is decided
 * here, so each rule is a test rather than a deployment.
 */

/** Only the customer API is reachable through this server. Never the admin, never the review API. */
export const CUSTOMER_API_PREFIX = "/api/v1/app/";

export class DjangoError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, message: string, body: unknown, retryAfterSeconds: number | null) {
    super(message);
    this.name = "DjangoError";
    this.status = status;
    this.body = body;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/**
 * The path a BFF call may use. A relative path under the customer API and nothing else: no scheme,
 * no host, no `..`, no `//` — so no route handler and no screen can be talked into proxying
 * `/admin/` or another host, whatever ends up in a parameter.
 */
export function assertCustomerPath(path: string): string {
  if (!path.startsWith(CUSTOMER_API_PREFIX)) {
    throw new Error(`Not a customer API path: ${path}`);
  }
  if (path.includes("..") || path.includes("//") || /[\s\\]/.test(path)) {
    throw new Error(`Refusing a suspicious customer API path: ${path}`);
  }
  return path;
}

/**
 * Headers the browser sent that Django needs, and nothing else.
 *
 * - `Origin` is the **public** origin, because Django compares it with `CSRF_TRUSTED_ORIGINS` and
 *   the call arrives on an internal address. The matching `Host` header is **not** set here: it is
 *   a forbidden header name in the Fetch standard and Node's `fetch` drops it silently (measured
 *   2026-09-17 with `infra/prod/check-customer.sh` — Django saw `web:8000`). Production's
 *   `DJANGO_ALLOWED_HOSTS` therefore names the internal service as well as the public hostname,
 *   which nothing outside the private network can send anyway.
 * - `Cookie` and `X-CSRFToken` are the browser's own: the CSRF double-submit stays between the
 *   browser and Django, and this server has no session of its own to substitute.
 * - `X-Forwarded-For` is passed through **unchanged**. Caddy replaced whatever the client sent
 *   with the address it saw, so the rightmost entry is the client and `LOGIN_TRUSTED_PROXIES=1`
 *   still resolves it (ADR 0023's login throttle). Adding a hop here would break that.
 * - `X-Forwarded-Proto` defaults to `https`, so Django keeps treating a request that arrived over
 *   TLS as secure even though this hop is plain HTTP on a private network.
 *
 * Deliberately not forwarded: `Accept-Encoding` (this hop is local; let Node handle it),
 * `Referer`, `User-Agent` and everything else a page or an API answer has no use for.
 */
export function forwardedHeaders(
  incoming: Headers,
  options: { publicOrigin: string; contentType?: string | null; contentLength?: string | null },
): Headers {
  const out = new Headers();
  out.set("Origin", options.publicOrigin);
  out.set("Accept", "application/json");
  out.set("X-Forwarded-Proto", incoming.get("x-forwarded-proto") ?? "https");
  const forwardedFor = incoming.get("x-forwarded-for");
  if (forwardedFor) out.set("X-Forwarded-For", forwardedFor);
  const cookie = incoming.get("cookie");
  if (cookie) out.set("Cookie", cookie);
  const csrf = incoming.get("x-csrftoken");
  if (csrf) out.set("X-CSRFToken", csrf);
  const language = incoming.get("accept-language");
  if (language) out.set("Accept-Language", language);
  if (options.contentType) out.set("Content-Type", options.contentType);
  // Content-Length has to travel with the body. Without it undici sends the relayed stream with
  // chunked transfer-encoding, and Django on WSGI reads a request body by CONTENT_LENGTH: no
  // length, no body. Every write then reached Django empty and came back 422 "field required" —
  // sign-in included (2026-09-18). We never transform the body, so the incoming length is correct.
  if (options.contentLength) out.set("Content-Length", options.contentLength);
  return out;
}

/**
 * Is this write allowed to leave this server?
 *
 * Django's CSRF check is unchanged and is still the one that counts; this is the check ADR 0028
 * (a) says a route handler has to do for itself, in code we can read: a browser's
 * `Sec-Fetch-Site` must not say `cross-site`, and an `Origin`, when the browser sent one, must be
 * ours. A request with neither header is not a browser navigation — it is a script, and Django's
 * CSRF token is what stops it.
 */
export function writeAllowed(incoming: Headers, publicOrigin: string): boolean {
  const site = incoming.get("sec-fetch-site");
  if (site !== null && site !== "same-origin" && site !== "none") return false;
  const origin = incoming.get("origin");
  if (origin !== null && origin !== publicOrigin) return false;
  return true;
}

/**
 * Headers this server puts on every relayed answer, whatever the upstream said.
 *
 * The relay rebuilds the answer's header set from a small allowlist, so anything Django set for
 * the browser's benefit — the invoice PDF's own `Cache-Control: private, no-store` and
 * `X-Content-Type-Options: nosniff` — was dropped on the way out, and no relayed answer carried a
 * store directive at all. Every one of them is about one account: a signed-in session, an
 * organisation's rights record, its invoice as a PDF. A response with no freshness information is
 * heuristically cacheable, and this hop's answer goes on to a browser and to whatever is between.
 * So the rule is stated here rather than forwarded: none of it may be stored, and none of it may
 * have its type guessed.
 */
export const RELAY_RESPONSE_HEADERS: Readonly<Record<string, string>> = {
  "Cache-Control": "private, no-store",
  "X-Content-Type-Options": "nosniff",
};

/** `Retry-After` as whole seconds (ADR 0023 sends a number; the HTTP-date form is allowed too). */
export function parseRetryAfter(value: string | null, now: number = Date.now()): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const at = Date.parse(trimmed);
  return Number.isNaN(at) ? null : Math.max(0, Math.ceil((at - now) / 1000));
}

/** The message an error carries internally. Never shown to a customer — the UI owns the words. */
export function detailOf(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return `Request failed (${status})`;
}
