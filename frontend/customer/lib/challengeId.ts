/**
 * The shape a claimed-account id has in a path parameter (ADR 0055).
 *
 * A route parameter is whatever was in the URL and it reaches `relay` inside a template string, so
 * it is checked against exactly what the server generates (`chal_` + 32 hex, ADR 0033) before it
 * becomes part of a Django path. `assertCustomerPath` is the second line, not the first — and the
 * refusal is the same bare 404 Django gives for an unknown or another organisation's claim, so this
 * check cannot tell a malformed id from an unknown one either.
 *
 * Its own module rather than `@shared/app/caseFile`'s `parsePublicId`: only this app has the screen,
 * and widening a shared union for it would touch the SPA for nothing.
 */
import { RELAY_RESPONSE_HEADERS } from "./bff";

const CHALLENGE_ID = /^chal_[0-9a-f]{32}$/;

export function parseChallengeId(value: string | undefined): string | null {
  return value !== undefined && CHALLENGE_ID.test(value) ? value : null;
}

export function notFound(): Response {
  return new Response(JSON.stringify({ detail: "not_found" }), {
    status: 404,
    headers: { "Content-Type": "application/json", ...RELAY_RESPONSE_HEADERS },
  });
}
