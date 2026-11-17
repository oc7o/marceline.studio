"use client";
/**
 * How an island sends a write (ADR 0036): one POST/PUT/DELETE to this server's own route handler,
 * the CSRF token from the cookie, and `ApiError` on anything that is not 2xx — the same class the
 * shared logic (`@shared/app/rights`, `@shared/app/addOriginal`) already maps to words.
 *
 * `useWrite` holds only what the screen renders: busy, the error and the answer. There is no cache
 * to invalidate — after a successful write the island asks the server for the page again
 * (`router.refresh()`), and the server is the one place that knows what the new state is.
 */
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { ApiError } from "@shared/api/errors";

import { csrfToken } from "./csrf";

/** Send one write to a BFF route. `body` is JSON unless it is already a `FormData`. */
export async function send(
  path: string,
  method: "POST" | "PUT" | "DELETE",
  body?: unknown,
): Promise<unknown> {
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  const headers: Record<string, string> = { "X-CSRFToken": csrfToken() ?? "" };
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed: unknown = null;
  if (text !== "") {
    try {
      parsed = JSON.parse(text) as unknown;
    } catch {
      parsed = text;
    }
  }
  if (!response.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed && typeof parsed.detail === "string"
        ? parsed.detail
        : `Request failed (${response.status})`;
    const retryAfter = response.headers.get("Retry-After");
    throw new ApiError(
      response.status,
      detail,
      parsed,
      retryAfter && /^\d+$/.test(retryAfter) ? Number(retryAfter) : null,
    );
  }
  return parsed;
}

export interface WriteState {
  pending: boolean;
  error: unknown;
  /** The server's answer to the last successful write (202 `{pending}` on production). */
  data: unknown;
  run: (path: string, method: "POST" | "PUT" | "DELETE", body?: unknown) => Promise<boolean>;
}

export function useWrite(): WriteState {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [data, setData] = useState<unknown>(null);

  const run = useCallback(
    async (path: string, method: "POST" | "PUT" | "DELETE", body?: unknown) => {
      setPending(true);
      setError(null);
      try {
        const answer = await send(path, method, body);
        setData(answer);
        // Never retried: a refusal is an answer, and a declaration must not be sent twice.
        router.refresh();
        return true;
      } catch (caught) {
        setError(caught);
        return false;
      } finally {
        setPending(false);
      }
    },
    [router],
  );

  return { pending, error, data, run };
}
