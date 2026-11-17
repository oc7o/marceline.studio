/**
 * `POST /app/api/uploads/{upl_…}/complete` -> the same path under `/api/v1/app` (ADR 0044).
 *
 * The customer's own SHA-256 of the file they chose. Django compares it with the digest it
 * computed while the chunks arrived and queues the job only if the two agree; a mismatch empties
 * the spool and queues nothing.
 */
import { relay } from "../../../../../lib/django";
import { isUploadId, notFound } from "../../../../../lib/uploadIds";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ uploadId: string }> },
): Promise<Response> {
  const { uploadId } = await params;
  if (!isUploadId(uploadId)) return notFound();
  return relay(request, `/api/v1/app/uploads/${uploadId}/complete`, { method: "POST" });
}
