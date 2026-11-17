/**
 * The backend-for-frontend (ADR 0036): the only place this server talks to Django.
 *
 * `server-only`, so a client component importing it fails the build. Reads are made by server
 * components; writes are forwarded by the route handlers under `app/api/`, which is why this
 * module has both a "call for me" and a "relay this request" entry point.
 *
 * Nothing is cached. Every answer belongs to one customer, and ADR 0028's read-through cache on
 * production is Django's own business, behind `X-Data-As-Of`.
 */
import "server-only";
import { headers } from "next/headers";

import {
  DjangoError,
  RELAY_RESPONSE_HEADERS,
  assertCustomerPath,
  detailOf,
  forwardedHeaders,
  parseRetryAfter,
  writeAllowed,
} from "./bff";

export { DjangoError };

/** Django's address on the private side: the compose service on production, localhost in dev. */
const BASE_URL = (process.env.CUSTOMER_DJANGO_BASE_URL ?? "http://127.0.0.1:8000").replace(
  /\/+$/,
  "",
);

/**
 * The public origin, because Django compares `Origin` with `CSRF_TRUSTED_ORIGINS` and this call
 * arrives on an internal address.
 *
 * Configured explicitly in production and refused when missing: a write forwarded with the wrong
 * origin is a CSRF check that cannot pass, and one forwarded with a *guessed* origin is a check
 * that passes for the wrong reason. In development the incoming request's own host is used,
 * because `make customer-dev` and `make up` are the same machine.
 */
async function publicOrigin(): Promise<string> {
  const origin = process.env.CUSTOMER_PUBLIC_ORIGIN;
  if (origin) return origin;
  if (process.env.NODE_ENV === "production") {
    throw new Error(
      "CUSTOMER_PUBLIC_ORIGIN must be set: Django compares it with CSRF_TRUSTED_ORIGINS, and a " +
        "guessed origin is a CSRF check that passes for the wrong reason.",
    );
  }
  const incoming = await headers();
  const requestHost = incoming.get("host") ?? "127.0.0.1:3000";
  const proto = incoming.get("x-forwarded-proto") ?? "http";
  return `${proto}://${requestHost}`;
}

async function answer(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

/**
 * Read one customer endpoint on behalf of the browser that asked for this page.
 *
 * Throws `DjangoError` for every non-2xx; the caller decides what that means — 404 is
 * `notFound()`, 401 is "signed out", anything else is the screen's load error. The 404-not-403
 * rule (ADR 0028 c) needs nothing here: Django already answers 404 for an unknown original and
 * for someone else's alike, and this passes the status through untouched.
 */
export async function djangoGet<T>(path: string): Promise<T> {
  assertCustomerPath(path);
  const incoming = await headers();
  const origin = await publicOrigin();
  const response = await fetch(`${BASE_URL}${path}`, {
    method: "GET",
    headers: forwardedHeaders(incoming, { publicOrigin: origin }),
    cache: "no-store",
    redirect: "manual",
  });
  const body = await answer(response);
  if (!response.ok) {
    throw new DjangoError(
      response.status,
      detailOf(body, response.status),
      body,
      parseRetryAfter(response.headers.get("Retry-After")),
    );
  }
  return body as T;
}

/** True when the read failed because this browser has no customer session. */
export function isSignedOut(error: unknown): boolean {
  return error instanceof DjangoError && error.status === 401;
}

/** True when the read failed with "there is no such thing" — an unknown id or someone else's. */
export function isNotFound(error: unknown): boolean {
  return error instanceof DjangoError && error.status === 404;
}

/**
 * Relay one browser request to one Django endpoint (the write path).
 *
 * The body is streamed through, so the 16 MiB fingerprint upload does not have to be held twice.
 * Django's answer goes back with its status, its `Set-Cookie` (a login's session, a logout's
 * flush) and its `Retry-After` (ADR 0023's lockout), and nothing else — no upstream header a
 * customer has no use for, and no header of ours that would leak the private side.
 */
export async function relay(
  request: Request,
  path: string,
  init: { method: "GET" | "POST" | "PUT" | "DELETE" },
): Promise<Response> {
  assertCustomerPath(path);
  const origin = await publicOrigin();
  if (init.method !== "GET" && !writeAllowed(request.headers, origin)) {
    return Response.json({ detail: "cross-origin write refused" }, { status: 403 });
  }
  const upstream = await fetch(`${BASE_URL}${path}`, {
    method: init.method,
    headers: forwardedHeaders(request.headers, {
      publicOrigin: origin,
      contentType: request.headers.get("content-type"),
      contentLength: request.headers.get("content-length"),
    }),
    body: init.method === "DELETE" || init.method === "GET" ? undefined : request.body,
    // Required by undici whenever a request carries a streamed body.
    duplex: "half",
    cache: "no-store",
    redirect: "manual",
  } as RequestInit & { duplex: "half" });

  const out = new Headers();
  // Exactly these, and nothing else: the answer's type, the filename of a downloaded invoice PDF
  // and ADR 0023's lockout wait. No upstream header a customer has no use for, and nothing of ours
  // that would describe the private side.
  for (const name of ["content-type", "content-disposition", "retry-after"]) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  // Ours, not the upstream's: every relayed answer is about one account, so none of it may be
  // stored and none of it may have its type guessed (`RELAY_RESPONSE_HEADERS`).
  for (const [name, value] of Object.entries(RELAY_RESPONSE_HEADERS)) out.set(name, value);
  for (const cookie of upstream.headers.getSetCookie()) out.append("Set-Cookie", cookie);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
