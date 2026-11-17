/**
 * The demo's entry point. Order matters: the mock API replaces `fetch` first, and only then is
 * the app imported — dynamically, because a static import would be evaluated before this module's
 * body and openapi-fetch would capture the real `fetch`.
 */
import { installLinkHandling } from "./mock/downloads";
import { installMockApi } from "./mock/fetch";

installMockApi();
installLinkHandling();

// The demo is the customer app only. The staff moderation screens are still in the route tree it
// imports, but nothing here answers their API, so every other address leads to /app.
if (!/^\/app(\/|$)/.test(window.location.pathname)) {
  window.history.replaceState(null, "", "/app");
}

void import("./main");
