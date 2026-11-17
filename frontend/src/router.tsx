/**
 * Two audiences, one SPA, two layouts.
 *
 * - **Staff** moderation screens keep their paths, their ids and their staff-only gate
 *   (`RootLayout`). Their route definitions are untouched by the customer UI.
 * - **Customers** live under `/app` with their own shell, session and language (ADR 0026). A
 *   customer never renders staff navigation — `RootShell` picks the layout from the matched route,
 *   so for an `/app` URL the staff layout (and its `GET /auth/me`) never runs at all.
 *
 * Deliberately **no pathless layout routes** (`createRoute({ id })`): TanStack types `Link`'s `to`
 * against route *ids*, and a pathless parent puts its id into every descendant's id — which would
 * turn `to="/references"` into a type error for the staff screens and `to="/app/originals"` into
 * one for the customer screens still to come.
 */
import {
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  useRouterState,
} from "@tanstack/react-router";
import { Suspense, lazy } from "react";

import { APP_ROUTE_ID } from "./app/routeIds";
import { type Band, type DecidedByFilter, parseBands, parseDecidedBy } from "./lib/confidence";

/**
 * Every screen (both audiences) is a lazy route component (issue #82, ADR 0028 performance
 * budget): the entry chunk carries only the router shell, React and TanStack Router/Query, so
 * visiting `/app` never downloads the staff moderation screens and visiting a staff URL never
 * downloads the customer app or its i18n resources. `RootLayout` (the staff-only chrome, its own
 * `GET /auth/me`) is lazy too, so it is never fetched for a customer route.
 */

export const STATUS_FILTERS = ["proposed", "confirmed", "rejected", "conflict", "all"] as const;
export type StatusFilter = (typeof STATUS_FILTERS)[number];

export const CLUSTER_SHOW = ["linked", "all"] as const;
export type ClusterShow = (typeof CLUSTER_SHOW)[number];

export interface ReferenceSearch {
  reference?: number;
  status?: StatusFilter;
  site?: number;
  hideRemoved?: boolean;
  /** Confidence band chips; absent = high + review (low hidden). */
  band?: Band[];
  /** Also show low-band matches. */
  includeLow?: boolean;
  decidedBy?: DecidedByFilter;
}

export interface ClusterSearch {
  /** Default "all": every open cluster, linked ones first; "linked" is opt-in (issue #2). */
  show?: ClusterShow;
  reference?: number;
  hideRemoved?: boolean;
}

function positiveInt(value: unknown): number | undefined {
  const number = typeof value === "string" ? Number(value) : value;
  return typeof number === "number" && Number.isInteger(number) && number > 0 ? number : undefined;
}

function flag(value: unknown): true | undefined {
  return value === true || value === "true" || value === "1" ? true : undefined;
}

export function parseReferenceSearch(search: Record<string, unknown>): ReferenceSearch {
  const status = STATUS_FILTERS.find((value) => value === search.status);
  return {
    reference: positiveInt(search.reference),
    status,
    site: positiveInt(search.site),
    hideRemoved: flag(search.hideRemoved),
    band: parseBands(search.band),
    includeLow: flag(search.includeLow),
    decidedBy: parseDecidedBy(search.decidedBy),
  };
}

export function parseClusterSearch(search: Record<string, unknown>): ClusterSearch {
  return {
    show: CLUSTER_SHOW.find((value) => value === search.show),
    reference: positiveInt(search.reference),
    hideRemoved: flag(search.hideRemoved),
  };
}

const RootLayout = lazy(() =>
  import("./components/Layout").then((module) => ({ default: module.RootLayout })),
);

/**
 * Which shell this URL gets. The staff layout demands a staff session and renders the queue
 * navigation, so it must not run for a customer: the decision is made from the matched route id,
 * not from a string comparison on the location. `RootLayout` is imported lazily, so a customer
 * visiting `/app` never fetches it (and never fires the `GET /auth/me` it makes on mount).
 */
function RootShell() {
  const isCustomerRoute = useRouterState({
    select: (state) => state.matches.some((match) => match.routeId === APP_ROUTE_ID),
  });
  if (isCustomerRoute) return <Outlet />;
  return (
    <Suspense fallback={null}>
      <RootLayout />
    </Suspense>
  );
}

const rootRoute = createRootRoute({ component: RootShell });

// ---------------------------------------------------------------------------
// Staff moderation SPA: unchanged paths, unchanged staff-only login gate. Every screen is a lazy
// route component so none of it reaches the customer's `/app` bundle.
// ---------------------------------------------------------------------------
const referencesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/references",
  validateSearch: parseReferenceSearch,
  component: lazyRouteComponent(() => import("./screens/ReferencesScreen"), "ReferencesScreen"),
});

const clustersRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/clusters",
  validateSearch: parseClusterSearch,
  component: lazyRouteComponent(() => import("./screens/ClustersScreen"), "ClustersScreen"),
});

const safetyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/safety",
  component: lazyRouteComponent(() => import("./screens/SafetyScreen"), "SafetyScreen"),
});

// Start page.
const overviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: lazyRouteComponent(() => import("./screens/OverviewScreen"), "OverviewScreen"),
});

const originalsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/originals",
  component: lazyRouteComponent(() => import("./screens/OriginalsScreen"), "OriginalsScreen"),
});

const originalRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/originals/$referenceId",
  component: lazyRouteComponent(() => import("./screens/OriginalsScreen"), "OriginalDetailScreen"),
});

const candidatesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/candidates",
  component: lazyRouteComponent(() => import("./screens/CandidatesScreen"), "CandidatesScreen"),
});

const creatorsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/creators",
  component: lazyRouteComponent(() => import("./screens/CreatorsScreen"), "CreatorsScreen"),
});

const claimsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/claims",
  component: lazyRouteComponent(() => import("./screens/ClaimsScreen"), "ClaimsScreen"),
});

const commonFramesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/common-frames",
  component: lazyRouteComponent(
    () => import("./screens/CommonFramesScreen"),
    "CommonFramesScreen",
  ),
});

// ---------------------------------------------------------------------------
// Customer app (ADR 0026), mounted apart from the staff routes. Every screen (including the shell
// itself) is a lazy route component so a staff URL never fetches the customer app or its i18n
// resources (issue #82, ADR 0028 performance budget).
// ---------------------------------------------------------------------------
const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/app",
  // Loads (and initialises i18next with) only the detected language's **core** words before any
  // /app screen renders — a dynamic import here, not a static one, so the language files stay out
  // of every other route's bundle too (ADR 0028 performance budget, issue #82). A screen whose
  // words are a slice of their own loads it in its own loader below (issue #99).
  loader: () => import("./i18n").then((module) => module.ensureI18nReady()),
  component: lazyRouteComponent(() => import("./app/CustomerApp"), "CustomerApp"),
});

const appSignInRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/sign-in",
  component: lazyRouteComponent(() => import("./app/SignInScreen"), "SignInScreen"),
});

const appHomeRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/",
  component: lazyRouteComponent(() => import("./app/HomeScreen"), "CustomerHomeScreen"),
});

const appOriginalRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/originals/$originalId",
  component: lazyRouteComponent(() => import("./app/OriginalScreen"), "OriginalScreen"),
});

// Lazy on purpose (issue #29): the form is only for customers adding an original, and the
// fingerprinting code behind it (Worker, PDQ WASM) loads only once they press start. The static
// segment "new" wins over `$originalId`, so it never reads as an original id.
const appAddOriginalRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/originals/new",
  loader: () => import("./i18n").then((module) => module.loadScreenWords("AddOriginalScreen")),
  component: lazyRouteComponent(() => import("./app/AddOriginalScreen"), "AddOriginalScreen"),
});

const appCaseFileRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/originals/$originalId/copies/$pageId",
  loader: () => import("./i18n").then((module) => module.loadScreenWords("CaseFileScreen")),
  component: lazyRouteComponent(() => import("./app/CaseFileScreen"), "CaseFileScreen"),
});

// "Prepare notice draft" (issue #33, ADR 0035): a sibling of the copies route, one screen per
// record covering every notice draft for it.
const appNoticesRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/originals/$originalId/notices",
  loader: () => import("./i18n").then((module) => module.loadScreenWords("NoticeDraftScreen")),
  component: lazyRouteComponent(() => import("./app/NoticeDraftScreen"), "NoticeDraftScreen"),
});

const appRemovalsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/removals",
  component: lazyRouteComponent(() => import("./app/RemovalsScreen"), "RemovalsScreen"),
});

// Account-wide notice drafts, across every record: the per-record screen above
// (`appNoticesRoute`) only ever showed one record's drafts, so an account with several records had
// no way to see what it had sent without opening each one in turn.
const appNoticesListRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/notices",
  loader: () => import("./i18n").then((module) => module.loadScreenWords("NoticesScreen")),
  component: lazyRouteComponent(() => import("./app/NoticesScreen"), "NoticesScreen"),
});

const appInvoicesRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/invoices",
  component: lazyRouteComponent(() => import("./app/InvoicesScreen"), "InvoicesScreen"),
});

const appRightsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/rights",
  loader: () => import("./i18n").then((module) => module.loadScreenWords("RightsScreen")),
  component: lazyRouteComponent(() => import("./app/RightsScreen"), "RightsScreen"),
});

export const routeTree = rootRoute.addChildren([
  referencesRoute,
  clustersRoute,
  safetyRoute,
  overviewRoute,
  originalsRoute,
  originalRoute,
  candidatesRoute,
  creatorsRoute,
  claimsRoute,
  commonFramesRoute,
  appRoute.addChildren([
    appSignInRoute,
    // The customer data screens, all children of `appRoute`: `CustomerApp` puts everything except
    // the sign-in route behind the session gate, so a new screen is protected without a check of
    // its own. Flat siblings, not nested, so no screen has to render an outlet for another.
    appHomeRoute,
    appAddOriginalRoute,
    appOriginalRoute,
    appCaseFileRoute,
    appNoticesRoute,
    appRemovalsRoute,
    appNoticesListRoute,
    appInvoicesRoute,
    appRightsRoute,
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
