/**
 * `POST /app/api/notices/{notice_…}/withdraw` -> the same path under `/api/v1/app` (issue #33,
 * ADR 0035).
 *
 * Withdrawing a draft, with the customer's reason.
 */
import { relay } from "../../../../../lib/django";

export const dynamic = "force-dynamic";

const NOTICE_ID = /^notice_[0-9a-f]{32}$/;

export async function POST(
  request: Request,
  { params }: { params: Promise<{ noticeId: string }> },
): Promise<Response> {
  const { noticeId } = await params;
  if (!NOTICE_ID.test(noticeId)) return Response.json({ detail: "not found" }, { status: 404 });
  return relay(request, `/api/v1/app/notices/${noticeId}/withdraw`, { method: "POST" });
}
