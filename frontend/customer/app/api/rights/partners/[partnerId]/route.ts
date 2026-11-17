/**
 * `DELETE /app/api/rights/partners/{id}` -> removing a licensed partner (ADR 0034).
 *
 * The partner id is the API's own opaque string; anything with a slash, a space or a dot pair in
 * it is refused here rather than pasted into a path.
 */
import { relay } from "../../../../../lib/django";

export const dynamic = "force-dynamic";

const PARTNER_ID = /^[A-Za-z0-9_-]{1,64}$/;

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ partnerId: string }> },
): Promise<Response> {
  const { partnerId } = await params;
  if (!PARTNER_ID.test(partnerId)) return Response.json({ detail: "not found" }, { status: 404 });
  return relay(request, `/api/v1/app/rights/partners/${partnerId}`, { method: "DELETE" });
}
