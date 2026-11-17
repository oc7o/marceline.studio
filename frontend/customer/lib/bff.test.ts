import { describe, expect, it } from "vitest";

import {
  assertCustomerPath,
  detailOf,
  forwardedHeaders,
  parseRetryAfter,
  writeAllowed,
} from "./bff";

const identity = { publicOrigin: "https://dash.marceline.studio" };

describe("assertCustomerPath", () => {
  it("allows the customer API", () => {
    expect(assertCustomerPath("/api/v1/app/originals")).toBe("/api/v1/app/originals");
  });

  it("refuses anything else Django serves", () => {
    for (const path of [
      "/admin/",
      "/api/v1/review/overview",
      "/api/v1/auth/me",
      "/api/v1/media/1/image",
      "/api/v1/health",
    ]) {
      expect(() => assertCustomerPath(path)).toThrow(/customer API path/);
    }
  });

  it("refuses an absolute URL, a traversal and a smuggled host", () => {
    expect(() => assertCustomerPath("https://evil.example/api/v1/app/me")).toThrow();
    expect(() => assertCustomerPath("/api/v1/app/../review/overview")).toThrow(/suspicious/);
    expect(() => assertCustomerPath("/api/v1/app//evil.example/")).toThrow(/suspicious/);
    expect(() => assertCustomerPath("/api/v1/app/x y")).toThrow(/suspicious/);
  });
});

describe("forwardedHeaders", () => {
  it("sends the public origin, not the internal address", () => {
    const out = forwardedHeaders(new Headers({ host: "customer:3000" }), identity);
    expect(out.get("Origin")).toBe("https://dash.marceline.studio");
  });

  it("sets no Host header, because `fetch` would drop it anyway", () => {
    // Measured 2026-09-17 (`infra/prod/check-customer.sh`): `Host` is a forbidden header name in
    // the Fetch standard and Node drops it silently, so Django sees the internal service name and
    // `DJANGO_ALLOWED_HOSTS` has to name it. Setting it here would only look like it worked.
    expect(forwardedHeaders(new Headers({ host: "customer:3000" }), identity).get("Host")).toBeNull();
  });

  it("passes the browser's cookie and CSRF token through", () => {
    const out = forwardedHeaders(
      new Headers({ cookie: "sessionid=abc; csrftoken=xyz", "x-csrftoken": "xyz" }),
      identity,
    );
    expect(out.get("Cookie")).toBe("sessionid=abc; csrftoken=xyz");
    expect(out.get("X-CSRFToken")).toBe("xyz");
  });

  it("passes X-Forwarded-For through unchanged, so the login throttle still sees the client", () => {
    // Caddy replaced whatever the client sent; adding a hop here would make
    // LOGIN_TRUSTED_PROXIES=1 resolve this server instead of the customer.
    const out = forwardedHeaders(new Headers({ "x-forwarded-for": "203.0.113.7" }), identity);
    expect(out.get("X-Forwarded-For")).toBe("203.0.113.7");
  });

  it("claims https by default, so Django keeps treating the request as secure", () => {
    expect(forwardedHeaders(new Headers(), identity).get("X-Forwarded-Proto")).toBe("https");
    expect(
      forwardedHeaders(new Headers({ "x-forwarded-proto": "http" }), identity).get(
        "X-Forwarded-Proto",
      ),
    ).toBe("http");
  });

  it("forwards nothing else the browser sent", () => {
    const out = forwardedHeaders(
      new Headers({
        "user-agent": "Mozilla/5.0",
        referer: "https://elsewhere.example/",
        authorization: "Bearer nope",
        "x-real-ip": "198.51.100.1",
      }),
      identity,
    );
    expect(out.get("User-Agent")).toBeNull();
    expect(out.get("Referer")).toBeNull();
    expect(out.get("Authorization")).toBeNull();
    expect(out.get("X-Real-IP")).toBeNull();
  });

  it("carries the content type only when one was given", () => {
    expect(forwardedHeaders(new Headers(), identity).get("Content-Type")).toBeNull();
    expect(
      forwardedHeaders(new Headers(), { ...identity, contentType: "application/json" }).get(
        "Content-Type",
      ),
    ).toBe("application/json");
  });
});

describe("writeAllowed", () => {
  it("allows a same-origin write from our own page", () => {
    expect(
      writeAllowed(
        new Headers({ "sec-fetch-site": "same-origin", origin: identity.publicOrigin }),
        identity.publicOrigin,
      ),
    ).toBe(true);
  });

  it("refuses a cross-site write", () => {
    expect(writeAllowed(new Headers({ "sec-fetch-site": "cross-site" }), identity.publicOrigin)).toBe(
      false,
    );
    expect(
      writeAllowed(new Headers({ "sec-fetch-site": "same-site" }), identity.publicOrigin),
    ).toBe(false);
  });

  it("refuses a foreign Origin whatever Sec-Fetch-Site says", () => {
    expect(
      writeAllowed(
        new Headers({ "sec-fetch-site": "same-origin", origin: "https://evil.example" }),
        identity.publicOrigin,
      ),
    ).toBe(false);
  });

  it("lets a request with neither header through to Django's CSRF check", () => {
    // Not a browser: a script with a session can always call the API as itself (ADR 0028 b).
    // The CSRF token is what stops it, and that check is Django's, unchanged.
    expect(writeAllowed(new Headers(), identity.publicOrigin)).toBe(true);
    expect(writeAllowed(new Headers({ "sec-fetch-site": "none" }), identity.publicOrigin)).toBe(true);
  });
});

describe("parseRetryAfter", () => {
  it("reads seconds", () => {
    expect(parseRetryAfter("900")).toBe(900);
  });

  it("reads an HTTP date", () => {
    const now = Date.parse("2026-09-17T10:00:00Z");
    expect(parseRetryAfter("Thu, 17 Sep 2026 10:01:00 GMT", now)).toBe(60);
  });

  it("is null without a header and never negative", () => {
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter("nonsense")).toBeNull();
    const now = Date.parse("2026-09-17T10:00:00Z");
    expect(parseRetryAfter("Thu, 17 Sep 2026 09:59:00 GMT", now)).toBe(0);
  });
});

describe("detailOf", () => {
  it("takes the code Django sent", () => {
    expect(detailOf({ detail: "claim_needs_review" }, 409)).toBe("claim_needs_review");
  });

  it("falls back to the status", () => {
    expect(detailOf(null, 502)).toBe("Request failed (502)");
    expect(detailOf({ detail: { msg: "x" } }, 422)).toBe("Request failed (422)");
  });
});
