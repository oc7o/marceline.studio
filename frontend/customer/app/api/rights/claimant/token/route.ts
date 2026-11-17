/** `POST /app/api/rights/claimant/token` -> the account-control token request (ADR 0034). */
import { relay } from "../../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/rights/claimant/token", { method: "POST" });
}
