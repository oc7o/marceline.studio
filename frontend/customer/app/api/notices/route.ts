/**
 * `POST /app/api/notices` -> `POST /api/v1/app/notices` (issue #33, ADR 0035).
 *
 * Creating a draft: the customer's own pages, recipient, claimant details and confirmations.
 * Marceline prepares the text from facts it holds; nothing is sent to a real recipient by us.
 */
import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/notices", { method: "POST" });
}
