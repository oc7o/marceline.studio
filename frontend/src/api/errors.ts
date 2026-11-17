/**
 * `ApiError` and its formatting, split out of `client.ts` (ADR 0028 performance budget, issue
 * #82): this module has no dependency on `openapi-fetch` or the generated schema, so code that
 * only needs to recognise or format an error — `main.tsx`'s global query-cache handler in
 * particular — never pulls the API client (and its CSRF-aware `createClient()` call) into the
 * entry chunk. `client.ts` re-exports everything here for existing imports.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  /** Seconds to wait, from the response's `Retry-After` header; null unless the server sent one. */
  readonly retryAfterSeconds: number | null;

  constructor(status: number, message: string, body: unknown, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/**
 * `Retry-After` as whole seconds. The login endpoints send a number of seconds (ADR 0023); the
 * HTTP-date form is accepted too, because the header is allowed to use it.
 */
export function parseRetryAfter(value: string | null | undefined, now: number = Date.now()): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const at = Date.parse(trimmed);
  return Number.isNaN(at) ? null : Math.max(0, Math.ceil((at - now) / 1000));
}

function detailOf(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown; loc?: unknown };
      const where = Array.isArray(first.loc) ? `${first.loc.join(".")}: ` : "";
      return `${where}${typeof first.msg === "string" ? first.msg : "invalid input"}`;
    }
  }
  return `Request failed (${status})`;
}

/** Build the `ApiError` for a non-2xx `openapi-fetch` response. */
export function apiErrorFrom(response: Response, error: unknown): ApiError {
  return new ApiError(
    response.status,
    detailOf(error, response.status),
    error,
    parseRetryAfter(response.headers.get("Retry-After")),
  );
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const conflicts = (error.body as { conflicts?: Array<{ kind: string; id: number; reason: string }> })
      ?.conflicts;
    if (conflicts?.length) {
      const first = conflicts[0];
      return `${error.message}: ${first ? `${first.kind} ${first.id} ${first.reason}` : ""}`;
    }
    return error.message;
  }
  return error instanceof Error ? error.message : "Something went wrong";
}
