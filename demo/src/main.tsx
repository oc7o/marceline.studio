/**
 * The demo's entry point: the app's route handlers answer `fetch` from memory, every address
 * outside `/app` leads into it, and the real pages are mounted under the demo banner.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { DemoChrome } from "./DemoChrome";
import { installBff } from "./runtime/bff";
import { BASE_PATH, withoutBase } from "./runtime/paths";
import { AppRouter, startRouter } from "./runtime/router";
import "./demo.css";

function mount(): void {
  installBff();
  startRouter();

  const root = document.getElementById("root");
  if (!root) throw new Error("#root missing");

  createRoot(root).render(
    <StrictMode>
      <DemoChrome>
        <AppRouter />
      </DemoChrome>
    </StrictMode>,
  );
}

if (withoutBase(window.location.pathname) === null) {
  // A document request, not a history entry: the app's language cookie is scoped to `/app`, and a
  // document loaded from `/` can neither read nor write it, whatever its address bar says later.
  // The slash is the form a static host serves a directory under, which saves it a redirect.
  window.location.replace(`${BASE_PATH}/`);
} else {
  mount();
}
