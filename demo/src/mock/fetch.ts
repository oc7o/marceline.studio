/**
 * The in-browser API: a `fetch` that answers every `/api/…` request from memory.
 *
 * It has to be installed before `frontend/src/api/client.ts` is first imported, because
 * openapi-fetch keeps the `globalThis.fetch` it sees at that moment (`boot.ts` does this).
 * Anything that is not an API call goes to the real `fetch` untouched.
 */
import { handleCustomerApi } from "./customerApi";
import { say } from "./notify";
import { type MockReply, type MockRequest, refuse } from "./reply";

// Long enough for a loading state to be seen, short enough never to be waited for.
const LATENCY_MS = 90;

async function readBody(request: Request): Promise<unknown> {
  if (request.method === "GET" || request.method === "HEAD") return undefined;
  const type = request.headers.get("content-type") ?? "";
  try {
    if (type.includes("multipart/form-data")) return await request.formData();
    const text = await request.text();
    return text ? (JSON.parse(text) as unknown) : undefined;
  } catch {
    return undefined;
  }
}

async function answer(request: Request, url: URL): Promise<Response> {
  const mock: MockRequest = {
    method: request.method.toUpperCase(),
    path: url.pathname,
    query: url.searchParams,
    body: await readBody(request),
  };
  let reply: MockReply;
  try {
    reply = handleCustomerApi(mock) ?? unknown(mock);
  } catch (error) {
    console.warn("[demo] mock API failed for", mock.method, mock.path, error);
    reply = unknown(mock);
  }
  await new Promise((resolve) => setTimeout(resolve, LATENCY_MS));
  return new Response(JSON.stringify(reply.body), {
    status: reply.status,
    headers: { "content-type": "application/json" },
  });
}

/** Something the demo has no answer for: a read is "not found", a write is declined, both politely. */
function unknown(request: MockRequest): MockReply {
  if (request.method === "GET") return refuse(404, "not_found");
  say("notInDemo");
  return refuse(503, "demo_unavailable");
}

export function installMockApi(): void {
  const realFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url, window.location.origin);
    if (url.origin === window.location.origin && url.pathname.startsWith("/api/")) {
      return answer(request, url);
    }
    return realFetch(input, init);
  };
}
