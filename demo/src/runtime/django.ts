/**
 * Stands in for `frontend/customer/lib/django.ts` (the Vite config swaps the module): the same
 * four exports, answered from `mock/` instead of a Django on the private side. Everything above it
 * — the reads, the pages, the route handlers under `app/api` — is the app's own code, unchanged.
 */
import {
  DjangoError,
  RELAY_RESPONSE_HEADERS,
  assertCustomerPath,
  detailOf,
} from "../../../frontend/customer/lib/bff";
import { answerCustomerApi } from "../mock/customerApi";

export { DjangoError };

// Long enough for a button's busy state to be seen, short enough never to be waited for.
const WRITE_LATENCY_MS = 140;

export function djangoGet<T>(path: string): Promise<T> {
  assertCustomerPath(path);
  const url = new URL(path, window.location.origin);
  const reply = answerCustomerApi({ method: "GET", path: url.pathname, query: url.searchParams, body: undefined });
  if (reply.status >= 300) {
    return Promise.reject(new DjangoError(reply.status, detailOf(reply.body, reply.status), reply.body, null));
  }
  return Promise.resolve(reply.body as T);
}

export function isSignedOut(error: unknown): boolean {
  return error instanceof DjangoError && error.status === 401;
}

export function isNotFound(error: unknown): boolean {
  return error instanceof DjangoError && error.status === 404;
}

async function bodyOf(request: Request): Promise<unknown> {
  const type = request.headers.get("content-type") ?? "";
  try {
    if (type.includes("multipart/form-data")) return await request.formData();
    const text = await request.text();
    return text ? (JSON.parse(text) as unknown) : undefined;
  } catch {
    return undefined;
  }
}

export async function relay(
  request: Request,
  path: string,
  init: { method: "GET" | "POST" | "PUT" | "DELETE" },
): Promise<Response> {
  assertCustomerPath(path);
  const reading = init.method === "GET";
  const reply = answerCustomerApi({
    method: init.method,
    path,
    query: new URL(request.url).searchParams,
    body: reading || init.method === "DELETE" ? undefined : await bodyOf(request),
  });
  if (!reading) await new Promise((resolve) => setTimeout(resolve, WRITE_LATENCY_MS));
  if (reply.file) {
    return new Response(reply.file.content, {
      status: 200,
      headers: {
        ...RELAY_RESPONSE_HEADERS,
        "content-type": reply.file.type,
        "content-disposition": `attachment; filename="${reply.file.name}"`,
      },
    });
  }
  return new Response(JSON.stringify(reply.body), {
    status: reply.status,
    headers: { ...RELAY_RESPONSE_HEADERS, "content-type": "application/json" },
  });
}
