/**
 * `POST /app/api/verify-email` -> `POST /api/v1/app/auth/verify-email` (ADR 0054, issue #149).
 *
 * The signed-in resend the banner's page offers. 202 whether a mail went out or the resend interval
 * swallowed it: a screen that could tell the difference would have to explain it.
 */
import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/auth/verify-email", { method: "POST" });
}
