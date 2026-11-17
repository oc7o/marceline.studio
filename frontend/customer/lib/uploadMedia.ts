"use client";
/**
 * Sending a video file to be fingerprinted (issue #59, ADR 0044).
 *
 * The other half of "add an original": instead of hashing in this browser, the file is sent in
 * fixed chunks and the server computes the fingerprints and deletes it. Chosen per file; local
 * hashing stays the default.
 *
 * This module is **loaded on demand**, like the fingerprinting client, so a customer who never
 * uses it never downloads it. Each chunk is read, hashed into a running SHA-256 and sent, then
 * dropped — the file is never held in memory, whatever its size — and the digest the server
 * verifies is the digest of exactly the bytes that were sent.
 *
 * It only sends. Nothing here decides anything, and the file's **name never leaves the browser**:
 * the form pre-fills the title with it, and the title is what travels.
 */
import type { Upload } from "@shared/app/uploadStatus";
import { Sha256 } from "@shared/lib/sha256";

import { csrfToken } from "./csrf";
import { send } from "./write";

export interface UploadStarted {
  id: string;
  chunk_bytes: number;
  next_chunk: number;
  declared_bytes: number;
}

export interface UploadProgress {
  /** Bytes acknowledged by the server so far. */
  sent: number;
  total: number;
}

export interface UploadOptions {
  title: string;
  creators: readonly string[];
  fps: number;
  language: string;
  rightsTextVersion: string;
  /**
   * The upload declaration's version (ADR 0044 amendment 2026-09-19). Both ticks are required by
   * the form before this module is loaded at all, and the server refuses the upload without them —
   * it stores the wording verbatim against this version.
   */
  uploadDeclarationVersion: string;
  onProgress?: (progress: UploadProgress) => void;
  signal?: AbortSignal;
}

/** Raised when the customer left or pressed cancel; nothing is retried afterwards. */
export class UploadCancelled extends Error {
  constructor() {
    super("cancelled");
    this.name = "UploadCancelled";
  }
}

/**
 * One chunk, as the `Blob` the file gave us rather than a copy of its bytes: a `Blob` is a
 * `BodyInit` the browser streams, a `Uint8Array` is not one TypeScript's DOM types accept.
 */
async function sendChunk(uploadId: string, index: number, chunk: Blob): Promise<unknown> {
  const response = await fetch(`/app/api/uploads/${uploadId}/chunks/${index}`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "X-CSRFToken": csrfToken() ?? "",
      "Content-Type": "application/octet-stream",
    },
    body: chunk,
  });
  if (!response.ok) {
    // Reuse the same error shape every other write produces, so the words are the same words.
    const { ApiError } = await import("@shared/api/errors");
    let body: unknown = null;
    try {
      body = (await response.json()) as unknown;
    } catch {
      // A refusal without a JSON body: the status alone decides the words.
    }
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : `Request failed (${response.status})`;
    throw new ApiError(response.status, detail, body, null);
  }
  return response.json();
}

/**
 * Declare, send and complete one upload. Resolves with the server's answer to `complete`: the
 * queued job, which carries the id the screen then polls (`lib/useUploadStatus.ts`) and the id of
 * the original once there is one. The original itself appears in the overview once it is added.
 */
export async function uploadFile(file: File, options: UploadOptions): Promise<Upload> {
  const started = (await send("/app/api/uploads", "POST", {
    file_size: file.size,
    title: options.title,
    creators: options.creators,
    fps: options.fps,
    rights_confirmed: true,
    rights_text_version: options.rightsTextVersion,
    language: options.language,
    sole_performer_confirmed: true,
    article9_consent: true,
    upload_declaration_version: options.uploadDeclarationVersion,
  })) as UploadStarted;

  const chunkBytes = started.chunk_bytes;
  const digest = new Sha256();
  let sent = 0;
  let index = 0;
  while (sent < file.size) {
    if (options.signal?.aborted) throw new UploadCancelled();
    const slice = file.slice(sent, Math.min(sent + chunkBytes, file.size));
    const bytes = new Uint8Array(await slice.arrayBuffer());
    digest.update(bytes);
    await sendChunk(started.id, index, slice);
    sent += bytes.length;
    index += 1;
    options.onProgress?.({ sent, total: file.size });
  }

  if (options.signal?.aborted) throw new UploadCancelled();
  return (await send(`/app/api/uploads/${started.id}/complete`, "POST", {
    sha256: digest.digest(),
  })) as Upload;
}
