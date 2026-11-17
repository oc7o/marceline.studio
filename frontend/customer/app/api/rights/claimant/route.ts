/** `PUT /app/api/rights/claimant` -> `PUT /api/v1/app/rights/claimant` (ADR 0034). */
import { relay } from "../../../../lib/django";

export const dynamic = "force-dynamic";

export function PUT(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/rights/claimant", { method: "PUT" });
}
