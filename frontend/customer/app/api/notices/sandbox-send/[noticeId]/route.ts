/**
 * `POST /app/api/notices/sandbox-send/{notice_…}` -> the same path under `/api/v1/app` (issue #33,
 * ADR 0035, ADR 0046).
 *
 * The sandbox: the draft mailed to an address the customer names, so they can see what a
 * recipient would receive. Never a real recipient.
 *
 * `sandbox-send` stands before the id — in this path and in Django's — because it is the one
 * customer route the public host must refuse outright, and `public_surface.ROLE_ONLY_PREFIXES` is
 * a prefix allowlist. On production the upstream answers 404 and so does this relay.
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
  return relay(request, `/api/v1/app/notices/sandbox-send/${noticeId}`, { method: "POST" });
}
