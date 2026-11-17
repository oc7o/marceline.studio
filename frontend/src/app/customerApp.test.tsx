// @vitest-environment jsdom
/**
 * The customer auth path end to end, against a stubbed API: every response ADR 0023 documents, the
 * CSRF dance, logout, the route gate and the language switch.
 *
 * Expected copy is read from the resource files rather than retyped, so a reworded message is not
 * a failing test — a *missing* or *wrong* message is.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLanguageForTest } from "../i18n";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import { LANGUAGE_KEY } from "../i18n/language";
import { routeTree } from "../router";
import { APP_ROUTE_ID, APP_SIGN_IN_ROUTE_ID } from "./routeIds";

interface Call {
  path: string;
  method: string;
  csrf: string | null;
}

interface Reply {
  status: number;
  body: unknown;
  headers?: Record<string, string>;
}

const ORGANISATION = {
  id: "org_" + "1".repeat(32),
  name: "Novak Studio",
  segment: "professional",
  role: "owner",
  created_at: "2026-09-16T10:00:00Z",
};
const ACCOUNT = {
  username: "test-customer@marceline.studio",
  organisation: ORGANISATION,
  monitored_sources: "10_plus",
  can_export_data: true,
};
const CSRF_TOKEN = "csrf-token-1";
const BASE_URL = "http://localhost";

let calls: Call[] = [];

/**
 * Both globals have to be in place *before* this file's imports run, which is what `vi.hoisted`
 * gives us:
 *
 * - openapi-fetch captures `globalThis.fetch` when the client module is first imported, so a
 *   `vi.stubGlobal("fetch", …)` in a test body is never seen by it;
 * - jsdom ships no fetch, so these tests run against Node's, whose `Request` refuses a relative
 *   URL. The SPA uses relative URLs deliberately (same origin only, ADR 0014) and a real browser
 *   resolves them against the document — so do that here instead of bending the client.
 */
const { fetchMock } = vi.hoisted(() => {
  const NativeRequest = globalThis.Request;
  class RelativeRequest extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  }
  globalThis.Request = RelativeRequest;
  const fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return { fetchMock };
});

function describeCall(input: RequestInfo | URL, init?: RequestInit): Call {
  const request = new Request(input, init);
  return {
    path: new URL(request.url, BASE_URL).pathname,
    method: request.method.toUpperCase(),
    csrf: request.headers.get("X-CSRFToken"),
  };
}

/** Serve the customer API from a small state machine: signing in flips the session. */
function serveApi(options: { signedIn?: boolean; login?: Reply; account?: typeof ACCOUNT } = {}) {
  let signedIn = options.signedIn ?? false;
  const account = options.account ?? ACCOUNT;
  const reply = (call: Call): Reply => {
    if (call.path === "/api/v1/app/auth/session" && call.method === "GET") {
      return {
        status: 200,
        body: signedIn ? { authenticated: true, username: account.username } : { authenticated: false },
      };
    }
    if (call.path === "/api/v1/app/auth/login" && call.method === "POST") {
      const answer = options.login ?? { status: 200, body: account };
      if (answer.status === 200) signedIn = true;
      return answer;
    }
    if (call.path === "/api/v1/app/auth/logout" && call.method === "POST") {
      signedIn = false;
      return { status: 200, body: { authenticated: false } };
    }
    if (call.path === "/api/v1/app/me" && call.method === "GET") {
      return signedIn
        ? { status: 200, body: account }
        : { status: 401, body: { detail: "Unauthorized" } };
    }
    // The overview's data; its screens are tested in customerScreens.test.tsx.
    if ((call.path === "/api/v1/app/originals" || call.path === "/api/v1/app/findings") && call.method === "GET") {
      return signedIn ? { status: 200, body: [] } : { status: 401, body: { detail: "Unauthorized" } };
    }
    // The staff API, for the test that the staff SPA still mounts.
    if (call.path === "/api/v1/auth/me" && call.method === "GET") {
      return { status: 200, body: { authenticated: false, username: null, is_staff: false } };
    }
    return { status: 404, body: { detail: `unexpected ${call.method} ${call.path}` } };
  };

  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call = describeCall(input, init);
    calls.push(call);
    const answer = reply(call);
    return new Response(JSON.stringify(answer.body), {
      status: answer.status,
      headers: { "content-type": "application/json", ...answer.headers },
    });
  });
}

function renderAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

async function signIn() {
  fireEvent.change(await screen.findByLabelText(en.signIn.username), {
    target: { value: ACCOUNT.username },
  });
  fireEvent.change(screen.getByLabelText(en.signIn.password), { target: { value: "hunter2" } });
  fireEvent.click(screen.getByRole("button", { name: en.signIn.submit }));
}

const loginCalls = () =>
  calls.filter((call) => call.path === "/api/v1/app/auth/login" && call.method === "POST");

beforeEach(async () => {
  calls = [];
  fetchMock.mockReset();
  window.localStorage.clear();
  document.cookie = `csrftoken=${CSRF_TOKEN}`;
  await setLanguageForTest("en");
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
});

describe("the /app route gate", () => {
  it("sends an unauthenticated visitor to sign-in", async () => {
    serveApi({ signedIn: false });
    const router = renderAt("/app");

    expect(await screen.findByRole("heading", { name: en.signIn.heading })).toBeTruthy();
    await waitFor(() => expect(router.state.location.pathname).toBe("/app/sign-in"));
  });

  it("shows a customer no staff navigation", async () => {
    serveApi({ signedIn: false });
    renderAt("/app");

    await screen.findByRole("heading", { name: en.signIn.heading });
    expect(screen.queryByRole("navigation", { name: "Queues" })).toBeNull();
    expect(screen.queryByText("Reference leaks")).toBeNull();
    expect(screen.queryByText("Safety review")).toBeNull();
    // The staff layout never even ran: no staff session was asked for.
    expect(calls.some((call) => call.path === "/api/v1/auth/me")).toBe(false);
  });

  it("keeps the route ids the shell gates on", () => {
    serveApi();
    const router = renderAt("/app/sign-in");
    // The gate in CustomerApp compares against these ids; a renamed route must fail here rather
    // than quietly leave a screen ungated.
    expect(Object.keys(router.routesById)).toContain(APP_SIGN_IN_ROUTE_ID);
    expect(Object.keys(router.routesById)).toContain(APP_ROUTE_ID);
  });

  it("leaves the staff SPA on its own routes", async () => {
    serveApi();
    renderAt("/");

    // The staff login gate, not the customer shell.
    expect(await screen.findByRole("heading", { name: "Marceline review" })).toBeTruthy();
    expect(screen.queryByText(en.shell.tagline)).toBeNull();
  });
});

describe("signing in", () => {
  it("fetches the CSRF cookie first, echoes it, and shows the organisation", async () => {
    serveApi({ signedIn: false });
    renderAt("/app/sign-in");
    await signIn();

    expect(await screen.findByText(ORGANISATION.name)).toBeTruthy();
    expect(screen.getByText(en.home.segments.professional)).toBeTruthy();
    expect(screen.getByText(en.home.roles.owner)).toBeTruthy();
    expect(screen.getByText(en.shell.signedInAs.replace("{{username}}", ACCOUNT.username))).toBeTruthy();
    // ADR 0045 amendment: the download link is a plain same-origin `<a download>`, shown once
    // `me` says the account may export (never on the public host).
    const download = (await screen.findByText(en.shell.downloadData)) as HTMLAnchorElement;
    expect(download.getAttribute("href")).toBe("/api/v1/app/export");
    expect(download.hasAttribute("download")).toBe(true);

    // ADR 0023: the session GET comes first (it is what sets `csrftoken`), then the POST carries
    // the token as `X-CSRFToken`.
    const loginIndex = calls.findIndex((call) => call.path === "/api/v1/app/auth/login");
    expect(loginIndex).toBeGreaterThan(-1);
    expect(
      calls
        .slice(0, loginIndex)
        .some((call) => call.path === "/api/v1/app/auth/session" && call.method === "GET"),
    ).toBe(true);
    expect(calls[loginIndex]?.csrf).toBe(CSRF_TOKEN);
  });

  it("shows no download link where the account may not export (the public host)", async () => {
    serveApi({ signedIn: true, account: { ...ACCOUNT, can_export_data: false } });
    renderAt("/app");

    await screen.findByText(ORGANISATION.name);
    expect(screen.queryByText(en.shell.downloadData)).toBeNull();
  });

  it("says the same thing for every credential failure (401)", async () => {
    serveApi({ login: { status: 401, body: { detail: "Invalid username or password" } } });
    renderAt("/app/sign-in");
    await signIn();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(en.signIn.errors.invalid);
    // Says nothing about whether this account exists, and does not echo what was submitted.
    // (signInError.test.ts pins the stronger property: 401s are byte-identical to each other.)
    expect(alert.textContent).not.toMatch(/does not exist|unknown account|disabled|deactivated/i);
    expect(alert.textContent).not.toContain(ACCOUNT.username);
  });

  it("explains a correct password on an account with no organisation (403)", async () => {
    serveApi({
      login: { status: 403, body: { detail: "This account has no customer organisation." } },
    });
    renderAt("/app/sign-in");
    await signIn();

    expect((await screen.findByRole("alert")).textContent).toBe(en.signIn.errors.noOrganisation);
  });

  it("does not blame the account when a 403 was really a stale CSRF token", async () => {
    serveApi({ login: { status: 403, body: { detail: "CSRF check failed" } } });
    renderAt("/app/sign-in");
    await signIn();

    expect((await screen.findByRole("alert")).textContent).toBe(en.signIn.errors.formExpired);
  });

  it("shows the wait for a 429 and does not retry by itself", async () => {
    serveApi({
      login: {
        status: 429,
        body: { detail: "Too many login attempts. Try again later." },
        headers: { "Retry-After": "900" },
      },
    });
    renderAt("/app/sign-in");
    await signIn();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(
      en.signIn.errors.rateLimitedMinutes_other.replace("{{count}}", "15"),
    );

    // One attempt, and it stays one: a 429 must never become a retry loop.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(loginCalls()).toHaveLength(1);
  });

  it("reports an unreachable server without blaming the credentials", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (describeCall(input, init).path.endsWith("/auth/login")) {
        throw new TypeError("Failed to fetch");
      }
      return new Response(JSON.stringify({ authenticated: false }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    renderAt("/app/sign-in");
    await signIn();

    expect((await screen.findByRole("alert")).textContent).toBe(en.common.unreachable);
  });
});

describe("signing out", () => {
  it("ends the session and returns to sign-in", async () => {
    serveApi({ signedIn: true });
    const router = renderAt("/app");

    expect(await screen.findByText(ORGANISATION.name)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: en.shell.signOut }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/app/sign-in"));
    expect(await screen.findByRole("heading", { name: en.signIn.heading })).toBeTruthy();
    // A signed-out customer must not still be looking at their own organisation.
    expect(screen.queryByText(ORGANISATION.name)).toBeNull();
    const logout = calls.find((call) => call.path === "/api/v1/app/auth/logout");
    expect(logout?.csrf).toBe(CSRF_TOKEN);
  });
});

describe("the language switch", () => {
  it("starts in the browser's language, switches, and remembers the choice", async () => {
    serveApi({ signedIn: false });
    renderAt("/app/sign-in");

    // jsdom asks for English.
    expect(await screen.findByRole("heading", { name: en.signIn.heading })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: en.language.de }));

    expect(await screen.findByRole("heading", { name: de.signIn.heading })).toBeTruthy();
    expect(screen.getByText(de.signIn.accountsNote)).toBeTruthy();
    expect(window.localStorage.getItem(LANGUAGE_KEY)).toBe("de");
    expect(document.documentElement.lang).toBe("de");
    expect(screen.getByRole("button", { name: de.language.de }).getAttribute("aria-pressed")).toBe(
      "true",
    );
  });

  it("translates the error messages too", async () => {
    serveApi({ login: { status: 401, body: { detail: "Invalid username or password" } } });
    renderAt("/app/sign-in");

    fireEvent.click(await screen.findByRole("button", { name: en.language.de }));
    fireEvent.change(await screen.findByLabelText(de.signIn.username), {
      target: { value: ACCOUNT.username },
    });
    fireEvent.change(screen.getByLabelText(de.signIn.password), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: de.signIn.submit }));

    expect((await screen.findByRole("alert")).textContent).toBe(de.signIn.errors.invalid);
  });
});
