/**
 * `GET`/`DELETE /app/api/uploads/{upl_…}` -> the same path under `/api/v1/app` (ADR 0044).
 *
 * `GET` is how the island resumes (it reads `next_chunk`); `DELETE` cancels, and Django deletes
 * the spooled bytes in the same request.
 */
import { relay } from "../../../../lib/django";
import { isUploadId, notFound } from "../../../../lib/uploadIds";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ uploadId: string }> },
): Promise<Response> {
  const { uploadId } = await params;
  if (!isUploadId(uploadId)) return notFound();
  return relay(request, `/api/v1/app/uploads/${uploadId}`, { method: "GET" });
}

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ uploadId: string }> },
): Promise<Response> {
  const { uploadId } = await params;
  if (!isUploadId(uploadId)) return notFound();
  return relay(request, `/api/v1/app/uploads/${uploadId}`, { method: "DELETE" });
}
