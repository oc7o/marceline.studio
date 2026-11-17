export interface MockRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  /** Parsed JSON, `FormData` for a multipart upload, or `undefined`. */
  body: unknown;
}

export interface MockReply {
  status: number;
  body: unknown;
}

export function ok(body: unknown): MockReply {
  return { status: 200, body };
}

export function refuse(status: number, detail: string): MockReply {
  return { status, body: { detail } };
}
