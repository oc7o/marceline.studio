/**
 * `POST /app/api/uploads/{upl_…}/chunks/{n}` -> the same path under `/api/v1/app` (ADR 0044).
 *
 * The only route in this app whose body is customer **media**. It is streamed through by `relay`,
 * so this server holds nothing: one chunk passes from the browser's socket to Django's. Nothing is
 * buffered, logged or written here, and there is no route that reads a chunk back.
 */
import { relay } from "../../../../../../lib/django";
import { isChunkIndex, isUploadId, notFound } from "../../../../../../lib/uploadIds";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ uploadId: string; index: string }> },
): Promise<Response> {
  const { uploadId, index } = await params;
  if (!isUploadId(uploadId) || !isChunkIndex(index)) return notFound();
  return relay(request, `/api/v1/app/uploads/${uploadId}/chunks/${index}`, { method: "POST" });
}
