/**
 * Typed API client. `schema.d.ts` is generated from backend/openapi.json (`make frontend-api`);
 * never edit it by hand. Requests are same-origin only (Vite proxy in dev, Caddy later).
 */
import createClient, { type Middleware } from "openapi-fetch";

import { apiErrorFrom } from "./errors";
import type { components, paths } from "./schema";

export { ApiError, errorMessage, parseRetryAfter } from "./errors";

export type Schemas = components["schemas"];
export type ReferenceItem = Schemas["ReferenceItemOut"];
export type ReferenceSummary = Schemas["ReferenceSummaryOut"];
export type MediaRef = Schemas["MediaRefOut"];
export type ClusterItem = Schemas["ClusterOut"];
export type ClusterMember = Schemas["ClusterMemberOut"];
export type LinkedOriginal = Schemas["LinkedOriginalOut"];
export type OriginalLink = Schemas["OriginalLinkOut"];
export type PageInfo = Schemas["PageOut"];
export type RecheckResult = Schemas["RecheckOut"];
export type SafetyItem = Schemas["SafetyItemOut"];
export type Decision = Schemas["DecisionIn"]["decision"];

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function readCookie(name: string, cookies: string): string | null {
  for (const part of cookies.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

/** Django's CSRF check: echo the `csrftoken` cookie (set by GET /auth/me) as a header. */
export const csrfMiddleware: Middleware = {
  onRequest({ request }) {
    if (UNSAFE_METHODS.has(request.method)) {
      const token = readCookie("csrftoken", document.cookie);
      if (token) request.headers.set("X-CSRFToken", token);
    }
    return request;
  },
};

export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
api.use(csrfMiddleware);

/** Await an openapi-fetch call; throw `ApiError` for every non-2xx response. */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await request;
  if (!response.ok || error !== undefined) throw apiErrorFrom(response, error);
  return data as T;
}

/** The only image URL the UI ever renders: our own passthrough, by media id. */
export function mediaImageUrl(mediaId: number): string {
  return `/api/v1/media/${Math.trunc(mediaId)}/image`;
}
