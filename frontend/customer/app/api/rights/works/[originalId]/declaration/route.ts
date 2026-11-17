/**
 * `POST /app/api/rights/works/{orig_…}/declaration` -> the self-declaration (ADR 0034).
 *
 * The id goes through `parsePublicId` before it is put into a path: a route parameter is whatever
 * was in the URL, and `assertCustomerPath` is the second line, not the first.
 */
import { parsePublicId } from "@shared/app/caseFile";

import { relay } from "../../../../../../lib/django";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ originalId: string }> },
): Promise<Response> {
  const { originalId } = await params;
  const id = parsePublicId("orig", originalId);
  if (id === null) return Response.json({ detail: "not found" }, { status: 404 });
  return relay(request, `/api/v1/app/rights/works/${id}/declaration`, { method: "POST" });
}
