/**
 * The app's own route handlers (`frontend/customer/app/api/**`), run in the page.
 *
 * An island posts to `/app/api/…` as it does in production. `fetch` hands that request to the
 * real handler for the path, whose `relay` is the demo's (`django.ts`). Anything else goes to the
 * browser's own `fetch` untouched — and the Content-Security-Policy keeps that on this origin.
 */
import { say } from "../mock/notify";
import { BASE_PATH, type RoutePattern, matchRoute, routeSegments } from "./paths";

type Handler = (request: Request, context: { params: Promise<Record<string, string>> }) => Response | Promise<Response>;

const API_PREFIX = `${BASE_PATH}/api/`;

const handlers: RoutePattern<Record<string, unknown>>[] = Object.entries(
  import.meta.glob<Record<string, unknown>>("../../../frontend/customer/app/api/**/route.ts", { eager: true }),
).map(([file, module]) => ({ segments: routeSegments(file, "/customer/app/api/", "/route.ts"), value: module }));

function refusal(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } });
}

export function isBffPath(pathname: string): boolean {
  return pathname.startsWith(API_PREFIX);
}

export async function answerBff(request: Request): Promise<Response> {
  const { pathname } = new URL(request.url);
  const match = matchRoute(handlers, pathname.slice(API_PREFIX.length));
  const handler = match?.value[request.method.toUpperCase()];
  if (!match || typeof handler !== "function") {
    if (request.method !== "GET") say("notInDemo");
    return refusal(404, "not_found");
  }
  try {
    return await (handler as Handler)(request, { params: Promise.resolve(match.params) });
  } catch (error) {
    console.warn("[demo] route handler failed for", request.method, pathname, error);
    say("notInDemo");
    return refusal(503, "demo_unavailable");
  }
}

export function installBff(): void {
  const realFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), window.location.href), init);
    const url = new URL(request.url);
    if (url.origin === window.location.origin && isBffPath(url.pathname)) return answerBff(request);
    return realFetch(input, init);
  };
}
