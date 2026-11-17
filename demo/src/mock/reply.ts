export interface MockRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  /** Parsed JSON, `FormData` for a multipart upload, or `undefined`. */
  body: unknown;
}

/** A download built in the browser: an invoice PDF, a notice export, the data export. */
export interface MockFile {
  name: string;
  type: string;
  content: string;
}

export interface MockReply {
  status: number;
  body: unknown;
  file?: MockFile;
}

export function ok(body: unknown): MockReply {
  return { status: 200, body };
}

export function refuse(status: number, detail: string): MockReply {
  return { status, body: { detail } };
}

export function download(file: MockFile): MockReply {
  return { status: 200, body: null, file };
}
