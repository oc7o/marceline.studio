import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApiError } from "../../frontend/src/api/errors";
import { queryKeys } from "../../frontend/src/api/queryKeys";
import { router } from "../../frontend/src/router";
import { DemoChrome } from "./DemoChrome";
import "./demo.css";

// The same client `frontend/src/main.tsx` builds; that file cannot be imported because it mounts
// the app itself, without the demo banner.
const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
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
    <DemoChrome>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </DemoChrome>
  </StrictMode>,
);
