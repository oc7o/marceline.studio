/**
 * `POST /app/api/rights/works/{orig_…}/authority` -> the customer's own declaration that the
 * documents on file give them the right or authority they claimed (ADR 0034 amendment
 * 2026-09-18). Nobody else can make it, here or anywhere else.
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
  return relay(request, `/api/v1/app/rights/works/${id}/authority`, { method: "POST" });
}
