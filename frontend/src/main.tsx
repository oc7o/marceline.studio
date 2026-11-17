import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Both from tiny, side-effect-free modules (ADR 0028 performance budget, issue #82): importing
// `ApiError` from `./api/client` or `queryKeys` from `./api/queries` would pull the whole staff
// API client — `openapi-fetch`'s `createClient()` runs at module load — into this entry chunk,
// which every `/app` visitor downloads too. The staff screens still import the full modules.
import { ApiError } from "./api/errors";
import { queryKeys } from "./api/queryKeys";
import "./index.css";
import { router } from "./router";

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
      // Session expired: the root layout falls back to the login screen. (The customer app keys
      // its own session under a different query key — see src/app/queries.ts — so this no-ops for
      // customer 401s.)
      if (error instanceof ApiError && error.status === 401) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.me });
      }
    },
  }),
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("#root missing");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
