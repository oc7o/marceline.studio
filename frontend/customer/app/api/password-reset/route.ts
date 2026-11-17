/**
 * `POST /app/api/password-reset` -> `POST /api/v1/app/auth/password-reset` (ADR 0054, issue #149).
 *
 * "Send me a link." Django answers the same 202 whatever the address is, so there is nothing here to
 * branch on and nothing to add.
 */
import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/auth/password-reset", { method: "POST" });
}
