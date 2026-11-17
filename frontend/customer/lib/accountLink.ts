/**
 * The shape the account-link route handlers accept in a path parameter (issue #149, ADR 0054).
 *
 * The token reaches `relay` inside a template string, and `assertCustomerPath` only refuses the
 * obviously dangerous spellings. This makes the parameter exactly what the server generates — url
 * safe base64 of 32 random bytes — so nothing a browser puts in the URL can become part of a
 * different Django path. Same rule, same reason and the same regex as `lib/inviteToken.ts`, which
 * guards the other doorway; the two are separate because their refusal **bodies** differ, and each
 * must be byte for byte what Django answers for its own surface.
 *
 * A token that does not match gets the same neutral answer as a token that does not work, so this
 * check cannot be used to tell a malformed link from an unknown one.
 */

import { RELAY_RESPONSE_HEADERS } from "./bff";

const TOKEN = /^[A-Za-z0-9_-]{16,128}$/;

export function isAccountToken(value: string): boolean {
  return TOKEN.test(value);
}

/** The one refusal, byte for byte what Django answers for every unusable link (ADR 0054). */
export function linkNotFound(): Response {
  return new Response(JSON.stringify({ detail: "link_not_available" }), {
    status: 404,
    headers: { "Content-Type": "application/json", ...RELAY_RESPONSE_HEADERS },
  });
}
