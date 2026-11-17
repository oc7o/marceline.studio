/**
 * `GET /app/api/notices/{notice_…}/export/{txt|eml|annex}` -> the same path under `/api/v1/app`
 * (issue #33, ADR 0035 amendment 2026-09-18).
 *
 * The second binary a customer downloads, after the invoice PDF, and a route handler for the same
 * reason: the browser follows this link itself. Django names the file in `Content-Disposition`,
 * which `relay` passes through unchanged, and it is Django that decides whether this draft may be
 * downloaded at all.
 *
 * The format is checked here as well — not for safety (Django answers an unknown one with the
 * same 404 as an unknown notice) but so a typo in a link never reaches the private side.
 */
import { relay } from "../../../../../../lib/django";

export const dynamic = "force-dynamic";

const NOTICE_ID = /^notice_[0-9a-f]{32}$/;
const FORMATS = new Set(["txt", "eml", "annex"]);

export async function GET(
  request: Request,
  { params }: { params: Promise<{ noticeId: string; format: string }> },
): Promise<Response> {
  const { noticeId, format } = await params;
  if (!NOTICE_ID.test(noticeId) || !FORMATS.has(format)) {
    return Response.json({ detail: "not found" }, { status: 404 });
  }
  return relay(request, `/api/v1/app/notices/${noticeId}/export/${format}`, { method: "GET" });
}
