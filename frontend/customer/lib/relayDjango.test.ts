/**
 * The BFF relay, end to end, against a **real Django on WSGI** (ADR 0036).
 *
 * Why this file exists. On 2026-09-18 nobody could sign in to the customer app. `forwardedHeaders`
 * rebuilds the outgoing header set from scratch — deliberately, so nothing of ours leaks to Django
 * — and `Content-Length` was not on the allowlist. undici then sent the relayed body with chunked
 * transfer-encoding, and Django on WSGI reads a request body as
 * `LimitedStream(environ["wsgi.input"], int(environ.get("CONTENT_LENGTH") or 0))`: no length, no
 * body. **Every** write through the BFF arrived empty and came back `422 field required` — sign-in,
 * the sign-out session flush, notice drafts, add original, the fingerprint upload. It shipped green
 * because `bff.test.ts` tests pure functions and everything else mocked `fetch`, and a mocked
 * upstream accepts an empty body happily. `make customer-smoke` was the near miss: it is real, but
 * its upstream is a stub that never looked at what arrived, and it is not in `make check`.
 *
 * So the upstream here is Django itself (`scripts/django-upstream.py`, no database, no apps), on
 * `wsgiref`, on an ephemeral port on 127.0.0.1. Nothing about the relay is mocked: the route
 * handlers are the real ones, the `fetch` is undici's, and every assertion is about what Django
 * actually received or actually answered. `next/headers` and `server-only` are replaced because
 * they are Next's request context, which a test process does not have.
 *
 * The check that matters: delete the `Content-Length` line from `lib/bff.ts` and the first two
 * tests below fail with `body_bytes: 0` and `transfer_encoding: "chunked"`.
 *
 * No live network: the only socket is a loopback one this test starts and kills itself.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

// Next's request context, which a test process does not have. Nothing of the relay itself is
// replaced — `publicOrigin()` never asks for headers because CUSTOMER_PUBLIC_ORIGIN is set.
vi.mock("server-only", () => ({}));
vi.mock("next/headers", () => ({ headers: () => Promise.resolve(new Headers()) }));

const APP_ROOT = path.resolve(import.meta.dirname, "..");
const REPO_ROOT = path.resolve(APP_ROOT, "..", "..");
const UPSTREAM_SCRIPT = path.join(APP_ROOT, "scripts", "django-upstream.py");

const PUBLIC_ORIGIN = "https://customer.test";
/** The same token `scripts/django-upstream.py` puts in the cookie it hands out. */
const CSRF_TOKEN = "relaytestcsrftoken0123456789abcd";

/**
 * The interpreter that has Django: the backend's own virtualenv, which `make check` has already
 * created by the time it gets here (its first target runs `uv run ruff`). Never skipped when it is
 * missing — a test nobody runs is the state this file exists to leave. `MARCELINE_PYTHON` points
 * it elsewhere (a worktree has no venv of its own); a `python3` without Django fails loudly, with
 * the child's own ImportError in the message.
 */
function pythonWithDjango(): string {
  const candidates = [
    process.env.MARCELINE_PYTHON,
    path.join(REPO_ROOT, "backend", ".venv", "bin", "python"),
  ].filter((candidate): candidate is string => Boolean(candidate));
  return candidates.find((candidate) => existsSync(candidate)) ?? "python3";
}

interface Upstream {
  baseUrl: string;
  stop: () => void;
}

/** Start one Django upstream with the given `CSRF_TRUSTED_ORIGINS`, on a port it picks itself. */
async function startUpstream(trustedOrigins: string): Promise<Upstream> {
  const child = spawn(pythonWithDjango(), [UPSTREAM_SCRIPT], {
    env: { ...process.env, RELAY_CSRF_TRUSTED_ORIGINS: trustedOrigins },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let complaints = "";
  child.stderr?.setEncoding("utf8");
  child.stderr?.on("data", (piece: string) => (complaints += piece));
  const port = await new Promise<number>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`the Django upstream did not start in 30 s: ${complaints}`)),
      30_000,
    );
    let line = "";
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (piece: string) => {
      line += piece;
      const end = line.indexOf("\n");
      if (end < 0) return;
      clearTimeout(timer);
      resolve((JSON.parse(line.slice(0, end)) as { port: number }).port);
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`the Django upstream exited with ${code}: ${complaints}`));
    });
  });
  return { baseUrl: `http://127.0.0.1:${port}`, stop: () => void child.kill("SIGKILL") };
}

type Handler = (request: Request) => Promise<Response>;
type ParamHandler<T> = (request: Request, context: { params: Promise<T> }) => Promise<Response>;

interface Handlers {
  login: Handler;
  logout: Handler;
  session: Handler;
  notices: Handler;
  fingerprints: Handler;
  proofs: Handler;
  claimant: Handler;
  deletePartner: ParamHandler<{ partnerId: string }>;
  invoicePdf: ParamHandler<{ invoiceId: string }>;
}

/**
 * The real route handlers, bound to one upstream.
 *
 * `lib/django.ts` reads Django's address once, at import; `vi.resetModules()` is how a second
 * upstream gets its own copy of the module graph.
 */
async function handlersFor(upstream: Upstream): Promise<Handlers> {
  process.env.CUSTOMER_DJANGO_BASE_URL = upstream.baseUrl;
  process.env.CUSTOMER_PUBLIC_ORIGIN = PUBLIC_ORIGIN;
  vi.resetModules();
  return {
    login: (await import("../app/api/login/route")).POST,
    logout: (await import("../app/api/logout/route")).POST,
    session: (await import("../app/api/session/route")).GET,
    notices: (await import("../app/api/notices/route")).POST,
    fingerprints: (await import("../app/api/fingerprints/route")).POST,
    proofs: (await import("../app/api/rights/proofs/route")).POST,
    claimant: (await import("../app/api/rights/claimant/route")).PUT,
    deletePartner: (await import("../app/api/rights/partners/[partnerId]/route")).DELETE,
    invoicePdf: (await import("../app/api/invoices/[invoiceId]/pdf/route")).GET,
  };
}

/**
 * A request as the browser sends it to `/app/api/…`: same-origin, with the cookie and the token.
 *
 * `Content-Length` is set here because the `Request` constructor does not set it (in the Fetch
 * standard the length belongs to the network layer, not to the header list) while Next's route
 * handlers do carry it: Next builds the `Request` from the socket, where it is a header the
 * browser really sent. That is the header the relay has to pass on, and the header whose absence
 * cost every write on 2026-09-18 — so the test states it the way the handler receives it, and the
 * proof that it travelled is what Django echoes back, not what is set here.
 */
function browserRequest(
  url: string,
  init: {
    method: string;
    body?: BodyInit | null;
    contentType?: string;
    contentLength?: string;
    token?: string | null;
  } = {
    method: "GET",
  },
): Request {
  const headers = new Headers({
    "Sec-Fetch-Site": "same-origin",
    Origin: PUBLIC_ORIGIN,
    Cookie: `sessionid=browser-session; csrftoken=${CSRF_TOKEN}`,
    "Accept-Language": "de-DE,de;q=0.9",
    "X-Forwarded-For": "203.0.113.7",
    "X-Forwarded-Proto": "https",
    // The headers the relay must not pass on, sent on every request so their absence is measured.
    "User-Agent": "Mozilla/5.0 (a real browser)",
    Referer: `${PUBLIC_ORIGIN}/app/sign-in`,
    Authorization: "Bearer not-for-django",
  });
  const token = init.token === undefined ? CSRF_TOKEN : init.token;
  if (token !== null) headers.set("X-CSRFToken", token);
  if (init.contentType) headers.set("Content-Type", init.contentType);
  const measured = typeof init.body === "string" ? String(Buffer.byteLength(init.body)) : null;
  const length = init.contentLength ?? measured;
  if (length !== null) headers.set("Content-Length", length);
  return new Request(`${PUBLIC_ORIGIN}${url}`, {
    method: init.method,
    headers,
    body: init.body ?? null,
    duplex: "half",
  } as RequestInit & { duplex: "half" });
}

/** What the upstream echoed back about the request it received. */
interface Echo {
  path: string;
  method: string;
  content_length: string | null;
  transfer_encoding: string | null;
  content_type: string | null;
  body_bytes: number;
  body_sha256: string;
  body_text: string | null;
  headers: Record<string, string>;
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

let trusted: Upstream;
let untrusted: Upstream;
let bff: Handlers;

beforeAll(async () => {
  [trusted, untrusted] = await Promise.all([
    startUpstream(PUBLIC_ORIGIN),
    // The same Django with `CSRF_TRUSTED_ORIGINS` empty: the container that never got
    // DJANGO_CSRF_TRUSTED_ORIGINS passed in, which is the other half of the 2026-09-18 failure.
    startUpstream(""),
  ]);
  bff = await handlersFor(trusted);
}, 60_000);

afterAll(() => {
  trusted?.stop();
  untrusted?.stop();
});

describe("a relayed write reaches Django with its body", () => {
  it("sends the same bytes, with a length and no chunked encoding", async () => {
    const payload = JSON.stringify({ username: "someone@example.test", password: "a password" });
    const response = await bff.login(
      browserRequest("/app/api/login", {
        method: "POST",
        body: payload,
        contentType: "application/json",
      }),
    );

    expect(response.status).toBe(200);
    const echo = (await response.json()) as Echo;
    // The whole defect, stated as three numbers.
    expect(echo.body_bytes).toBe(Buffer.byteLength(payload));
    expect(echo.body_sha256).toBe(sha256(payload));
    expect(echo.content_length).toBe(String(Buffer.byteLength(payload)));
    expect(echo.transfer_encoding).toBeNull();
    // Django parsed the same document the browser sent.
    expect(echo.body_text).toBe(payload);
    expect(echo.content_type).toBe("application/json");
    expect(echo.path).toBe("/api/v1/app/auth/login");
  });

  it("never reads the body it is relaying", () => {
    // A guard against the shape of fix that would make the test above pass by buffering: the
    // relay must hand `request.body` to `fetch` as a stream, never read it first. The 16 MiB
    // fingerprint upload is the reason (issue #29).
    const source = readFileSync(path.join(APP_ROOT, "lib", "django.ts"), "utf8");
    for (const consumer of [
      /request\.text\(/,
      /request\.arrayBuffer\(/,
      /request\.json\(/,
      /request\.formData\(/,
      /request\.blob\(/,
      /request\.bytes\(/,
    ]) {
      expect(source).not.toMatch(consumer);
    }
    expect(source).toMatch(/body:[\s\S]{0,120}request\.body/);
  });
});

describe("Django's own checks judge what the relay forwarded", () => {
  it("accepts the forwarded Origin, cookie and CSRF token", async () => {
    const response = await bff.notices(
      browserRequest("/app/api/notices", {
        method: "POST",
        body: JSON.stringify({ site: "example" }),
        contentType: "application/json",
      }),
    );
    // `CsrfViewMiddleware` ran, with CSRF_TRUSTED_ORIGINS naming the public origin.
    expect(response.status).toBe(200);
    const echo = (await response.json()) as Echo;
    expect(echo.headers.Origin).toBe(PUBLIC_ORIGIN);
    expect(echo.headers.Cookie).toBe(`sessionid=browser-session; csrftoken=${CSRF_TOKEN}`);
    expect(echo.headers["X-Csrftoken"]).toBe(CSRF_TOKEN);
    // The login throttle (ADR 0023) reads the rightmost entry: this hop adds none.
    expect(echo.headers["X-Forwarded-For"]).toBe("203.0.113.7");
    expect(echo.headers["X-Forwarded-Proto"]).toBe("https");
    expect(echo.headers["Accept-Language"]).toBe("de-DE,de;q=0.9");
  });

  it("refuses the write when the browser sent no CSRF token, exactly as Django refuses it", async () => {
    const response = await bff.notices(
      browserRequest("/app/api/notices", {
        method: "POST",
        body: JSON.stringify({ site: "example" }),
        contentType: "application/json",
        token: null,
      }),
    );
    expect(response.status).toBe(403);
  });

  it("refuses the write when Django was never told to trust our origin", async () => {
    // Defect 1 of 2026-09-18, as Django experiences it: `DJANGO_CSRF_TRUSTED_ORIGINS` existed in
    // settings.py and in .env.example but was never passed into the web container, so it was empty
    // and every POST from the customer app failed the CSRF check. `infra/compose.yaml` passes it
    // now, and `backend/marceline/tests/test_compose_env.py` fails if it stops.
    const elsewhere = await handlersFor(untrusted);
    const response = await elsewhere.notices(
      browserRequest("/app/api/notices", {
        method: "POST",
        body: JSON.stringify({ site: "example" }),
        contentType: "application/json",
      }),
    );
    expect(response.status).toBe(403);
    bff = await handlersFor(trusted);
  });

  it("forwards nothing else the browser sent", async () => {
    const response = await bff.notices(
      browserRequest("/app/api/notices", {
        method: "POST",
        body: "{}",
        contentType: "application/json",
      }),
    );
    const echo = (await response.json()) as Echo;
    expect(echo.headers.Referer).toBeUndefined();
    expect(echo.headers.Authorization).toBeUndefined();
    // undici puts its own `User-Agent: node` on the hop; the browser's never reaches Django.
    expect(echo.headers["User-Agent"]).not.toContain("Mozilla");
  });
});

describe("Django's answer comes back", () => {
  it("passes every Set-Cookie through, separately", async () => {
    const response = await bff.login(
      browserRequest("/app/api/login", {
        method: "POST",
        body: JSON.stringify({ username: "someone@example.test", password: "x" }),
        contentType: "application/json",
      }),
    );
    const cookies = response.headers.getSetCookie();
    expect(cookies.some((cookie) => cookie.startsWith("sessionid=relay-test-session"))).toBe(true);
    expect(cookies.some((cookie) => cookie.startsWith("csrftoken="))).toBe(true);
    expect(cookies).toHaveLength(2);
  });

  it("passes the csrftoken cookie of the session GET through", async () => {
    const response = await bff.session(browserRequest("/app/api/session"));
    expect(response.status).toBe(200);
    expect(response.headers.getSetCookie().some((c) => c.startsWith("csrftoken="))).toBe(true);
  });

  it("passes the status and Retry-After of a lockout through (ADR 0023)", async () => {
    const response = await bff.login(
      browserRequest("/app/api/login", {
        method: "POST",
        body: JSON.stringify({ username: "locked@example.test", password: "x" }),
        contentType: "application/json",
      }),
    );
    expect(response.status).toBe(429);
    expect(response.headers.get("Retry-After")).toBe("900");
  });

  it("lets no answer of one account be stored, and no type be guessed", async () => {
    // The relay rebuilds the answer's headers from an allowlist, so Django's own
    // `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff` on the invoice PDF
    // never reached the browser, and no relayed answer carried a store directive at all. Stated
    // here rather than forwarded: every one of these is about one signed-in account.
    const invoiceId = `inv_${"a".repeat(32)}`;
    const answers = [
      await bff.session(browserRequest("/app/api/session")),
      await bff.invoicePdf(
        browserRequest(`/app/api/invoices/${invoiceId}/pdf`, { method: "GET" }),
        { params: Promise.resolve({ invoiceId }) },
      ),
      await bff.login(
        browserRequest("/app/api/login", {
          method: "POST",
          body: JSON.stringify({ username: "someone@example.test", password: "a password" }),
          contentType: "application/json",
        }),
      ),
    ];
    for (const answer of answers) {
      expect(answer.headers.get("Cache-Control")).toBe("private, no-store");
      expect(answer.headers.get("X-Content-Type-Options")).toBe("nosniff");
    }
  });

  it("keeps the private side private, and adds no CORS header", async () => {
    const response = await bff.session(browserRequest("/app/api/session"));
    expect(response.headers.get("X-Upstream-Note")).toBeNull();
    expect(response.headers.get("Server")).toBeNull();
    for (const [name] of response.headers) {
      expect(name.toLowerCase().startsWith("access-control-")).toBe(false);
    }
  });

  it("brings a binary answer back byte for byte, with its filename", async () => {
    const invoiceId = `inv_${"a".repeat(32)}`;
    const response = await bff.invoicePdf(
      browserRequest(`/app/api/invoices/${invoiceId}/pdf`, { method: "GET" }),
      { params: Promise.resolve({ invoiceId }) },
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Type")).toBe("application/pdf");
    expect(response.headers.get("Content-Disposition")).toBe(
      'attachment; filename="invoice-2026-0001.pdf"',
    );
    const bytes = new Uint8Array(await response.arrayBuffer());
    const expected = Buffer.concat([
      Buffer.from("%PDF-1.4\n"),
      Buffer.concat(Array.from({ length: 4 }, () => Buffer.from(Array.from({ length: 256 }, (_, i) => i)))),
    ]);
    expect(Buffer.from(bytes).equals(expected)).toBe(true);
  });
});

describe("every shape of write the app sends", () => {
  it("relays a write with no body at all (the sign-out flush)", async () => {
    const response = await bff.logout(browserRequest("/app/api/logout", { method: "POST" }));
    expect(response.status).toBe(200);
    const echo = (await response.json()) as Echo;
    expect(echo.body_bytes).toBe(0);
    expect(echo.path).toBe("/api/v1/app/auth/logout");
    expect(response.headers.getSetCookie().some((c) => c.startsWith("sessionid="))).toBe(true);
  });

  it("relays a PUT with its body (the claimant details, ADR 0034)", async () => {
    const payload = JSON.stringify({ claimant_type: "organisation", name: "Studio" });
    const response = await bff.claimant(
      browserRequest("/app/api/rights/claimant", {
        method: "PUT",
        body: payload,
        contentType: "application/json",
      }),
    );
    const echo = (await response.json()) as Echo;
    expect(echo.method).toBe("PUT");
    expect(echo.body_sha256).toBe(sha256(payload));
    expect(echo.content_length).toBe(String(Buffer.byteLength(payload)));
  });

  it("relays a DELETE, which carries no body", async () => {
    const response = await bff.deletePartner(
      browserRequest("/app/api/rights/partners/partner-1", { method: "DELETE" }),
      { params: Promise.resolve({ partnerId: "partner-1" }) },
    );
    expect(response.status).toBe(200);
    const echo = (await response.json()) as Echo;
    expect(echo.method).toBe("DELETE");
    expect(echo.body_bytes).toBe(0);
  });

  it("relays multipart so Django's own parser reads the fields and the file", async () => {
    // The boundary lives in Content-Type and the length in Content-Length; without either one
    // Django's multipart parser fails rather than quietly seeing nothing.
    const boundary = "----marceline-relay-test-boundary";
    const file = "a rights proof, not a picture of anything";
    const body =
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="kind"\r\n\r\nagency_mandate\r\n` +
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="document"; filename="mandate.txt"\r\n` +
      `Content-Type: text/plain\r\n\r\n${file}\r\n` +
      `--${boundary}--\r\n`;

    const response = await bff.proofs(
      browserRequest("/app/api/rights/proofs", {
        method: "POST",
        body,
        contentType: `multipart/form-data; boundary=${boundary}`,
      }),
    );
    expect(response.status).toBe(200);
    const parsed = (await response.json()) as {
      content_type: string;
      content_length: string;
      fields: Record<string, string>;
      files: Record<string, { name: string; size: number }>;
    };
    expect(parsed.content_type).toBe(`multipart/form-data; boundary=${boundary}`);
    expect(parsed.content_length).toBe(String(Buffer.byteLength(body)));
    expect(parsed.fields).toEqual({ kind: "agency_mandate" });
    expect(parsed.files.document).toEqual({ name: "mandate.txt", size: Buffer.byteLength(file) });
  });
});

describe("the fingerprint upload, the one large body (issue #29)", () => {
  it("streams four megabytes through unchanged", async () => {
    // A hashes-only document of the shape `client/fingerprint.py` and the browser produce: never
    // built as one string here, and never buffered by the relay — the chunks are generated, hashed
    // and handed to the stream one at a time, which is the only way a 16 MiB upload stays cheap.
    const opening = '{"kind":"video","pipeline":"ffmpeg-rgb512-pdq-v1","hashes":["';
    const closing = '"]}';
    const middle = "0123456789abcdef".repeat(4096); // 64 KiB
    const repeats = 64; // 4 MiB of hashes
    const pieces = function* (): Generator<Uint8Array> {
      const encoder = new TextEncoder();
      yield encoder.encode(opening);
      for (let i = 0; i < repeats; i += 1) yield encoder.encode(middle);
      yield encoder.encode(closing);
    };

    const digest = createHash("sha256");
    let total = 0;
    for (const piece of pieces()) {
      digest.update(piece);
      total += piece.byteLength;
    }
    expect(total).toBeGreaterThan(4 * 1024 * 1024);

    const source = pieces();
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        const next = source.next();
        if (next.done) controller.close();
        else controller.enqueue(next.value);
      },
    });
    const response = await bff.fingerprints(
      browserRequest("/app/api/fingerprints", {
        method: "POST",
        body: stream,
        contentType: "application/json",
        contentLength: String(total),
      }),
    );
    expect(response.status).toBe(200);
    const echo = (await response.json()) as Echo;
    expect(echo.content_length).toBe(String(total));
    expect(echo.transfer_encoding).toBeNull();
    expect(echo.body_bytes).toBe(total);
    expect(echo.body_sha256).toBe(digest.digest("hex"));
  }, 30_000);
});
