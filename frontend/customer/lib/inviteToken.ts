/**
 * The shape the invite route handlers accept in a path parameter (issue #115, ADR 0052).
 *
 * The token reaches `relay` inside a template string, and `assertCustomerPath` only refuses the
 * obviously dangerous spellings. This makes the parameter exactly what the server generates — url
 * safe base64 of 32 random bytes — so nothing a browser puts in the URL can become part of a
 * different Django path. It is the same rule and the same reason as `lib/uploadIds.ts`.
 *
 * A token that does not match gets the **same** neutral answer as a token that does not work, so
 * this check cannot be used to tell a malformed token from an unknown one.
 */

import { RELAY_RESPONSE_HEADERS } from "./bff";

const TOKEN = /^[A-Za-z0-9_-]{16,128}$/;

export function isInviteToken(value: string): boolean {
  return TOKEN.test(value);
}

/** The one refusal, byte for byte what Django answers for every unusable token (ADR 0052 §6). */
export function inviteNotFound(): Response {
  return new Response(JSON.stringify({ detail: "invite_not_available" }), {
    status: 404,
    headers: { "Content-Type": "application/json", ...RELAY_RESPONSE_HEADERS },
  });
}
