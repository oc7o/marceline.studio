/**
 * The shapes the upload route handlers accept in a path parameter (issue #59, ADR 0044).
 *
 * A parameter reaches `relay` inside a template string, and `assertCustomerPath` only refuses the
 * obviously dangerous spellings. These two make the parameters exactly what the server generates —
 * an opaque id and a non-negative integer — so nothing a browser puts in the URL can become part
 * of a different Django path.
 */

import { RELAY_RESPONSE_HEADERS } from "./bff";

const UPLOAD_ID = /^upl_[0-9a-f]{32}$/;
const INDEX = /^(0|[1-9][0-9]{0,8})$/;

export function isUploadId(value: string): boolean {
  return UPLOAD_ID.test(value);
}

export function isChunkIndex(value: string): boolean {
  return INDEX.test(value);
}

/**
 * The same bare 404 Django answers for an unknown or someone else's upload (ADR 0021).
 *
 * It carries `RELAY_RESPONSE_HEADERS` like every relayed answer: a refusal about one customer's
 * upload is no more storable, and no more sniffable, than the answer would have been.
 */
export function notFound(): Response {
  return new Response(JSON.stringify({ detail: "not_found" }), {
    status: 404,
    headers: { "Content-Type": "application/json", ...RELAY_RESPONSE_HEADERS },
  });
}
