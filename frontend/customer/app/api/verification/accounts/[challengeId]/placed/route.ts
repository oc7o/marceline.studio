/**
 * `POST /app/api/verification/accounts/{chal_…}/placed` -> "I have placed the code" (ADR 0055).
 *
 * The id goes through `parseChallengeId` before it is put into a path, and a malformed one gets the
 * same bare 404 as an unknown or another organisation's claim.
 */
import { relay } from "../../../../../../lib/django";
import { notFound, parseChallengeId } from "../../../../../../lib/challengeId";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ challengeId: string }> },
): Promise<Response> {
  const { challengeId } = await params;
  const id = parseChallengeId(challengeId);
  if (id === null) return notFound();
  return relay(request, `/api/v1/app/verification/accounts/${id}/placed`, { method: "POST" });
}
