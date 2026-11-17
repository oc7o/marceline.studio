/**
 * Raises Testing Library's default async-query timeout for this whole project (issue #122).
 *
 * The customer routes are lazy (`lazyRouteComponent`, `src/app/CLAUDE.md` "Performance budget"):
 * before a screen's first heading appears, the router's own loader, that screen's i18n slice and
 * the screen's own chunk (`import("./app/…Screen")`) all have to resolve, on top of a mocked fetch
 * settling through React Query. Normally that chain is a handful of milliseconds, but
 * `make remote-check` runs on a shared host that this ticket also made queue checks one at a time
 * precisely because a single check already saturates every core (`pytest -n auto`) — even so, a
 * screen's chunk can still be transformed for the first time in a worker right as a neighbouring
 * check phase or another vitest worker briefly claims the CPU. That has timed out
 * `findByRole`/`findByText` on `frontend/src/app/noticeDraftScreen.test.tsx` and
 * `noticesScreen.test.tsx` around the default 1000 ms — a slow render, not a broken screen.
 *
 * One place, not a scattered `{}, { timeout: 5000 }` on individual queries (that pattern used to
 * live only in `addOriginalScreen.test.tsx`, which hit the same thing first).
 */
import { configure } from "@testing-library/dom";

configure({ asyncUtilTimeout: 5000 });
