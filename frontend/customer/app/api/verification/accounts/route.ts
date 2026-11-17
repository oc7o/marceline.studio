/** `POST /app/api/verification/accounts` -> claim the platform account and get a code (ADR 0055). */
import { relay } from "../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/verification/accounts", { method: "POST" });
}
