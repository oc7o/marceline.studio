/**
 * What Next's App Router does for the customer app, for a page with no server.
 *
 * The app's pages and its session-gate layout are async server components: functions that read
 * and return markup. Here they are called in the browser — the same functions, with the same
 * `params` and `searchParams` — and what they return is rendered by React as it is. `redirect()`
 * and `notFound()` are caught where Next catches them, `router.refresh()` runs the page again and
 * lets React reconcile (so an island keeps its state, as it does in production), and the address
 * bar carries `/app/…` exactly as the real app's does.
 */
import {
  Component,
  Fragment,
  type ReactNode,
  createElement,
  useMemo,
  useSyncExternalStore,
} from "react";

import SignedInLayout from "../../../frontend/customer/app/(signed-in)/layout";
import ErrorScreen from "../../../frontend/customer/app/error";
import NotFoundPage from "../../../frontend/customer/app/not-found";
import { requestI18n } from "../../../frontend/customer/lib/i18n";
import type { Language } from "../../../frontend/customer/lib/requestLanguage";
import { isBffPath } from "./bff";
import { installLinkHandling, save } from "./links";
import { currentLanguage } from "./language";
import { type DemoRouter, NavigationContext, NotFoundSignal, RedirectSignal } from "./next-navigation";
import { type RoutePattern, matchRoute, routeSegments, withBase, withoutBase } from "./paths";

interface PageProps {
  params: Promise<Record<string, string>>;
  searchParams: Promise<Record<string, string>>;
}
interface PageModule {
  default: (props: PageProps) => ReactNode | Promise<ReactNode>;
}
interface Route {
  load: () => Promise<PageModule>;
  /** Inside `app/(signed-in)`: the session gate runs first and its shell wraps the page. */
  gated: boolean;
}

const routes: RoutePattern<Route>[] = Object.entries(
  import.meta.glob<PageModule>("../../../frontend/customer/app/**/page.tsx"),
).map(([file, load]) => ({
  segments: routeSegments(file, "/customer/app/", "/page.tsx"),
  value: { load, gated: file.includes("/(signed-in)/") },
}));

async function renderPage(pathname: string, search: string): Promise<ReactNode> {
  try {
    const match = matchRoute(routes, pathname);
    if (!match) throw new NotFoundSignal();
    const { load, gated } = match.value;
    // The gate answers before the page is run, as a layout does: signed out is a redirect from
    // here, whatever the page underneath would have made of it.
    if (gated) await SignedInLayout({ children: null });
    const page = (await load()).default;
    const content = await page({
      params: Promise.resolve(match.params),
      searchParams: Promise.resolve(Object.fromEntries(new URLSearchParams(search))),
    });
    // A new address is a new page (its islands start over); the same address asked for again is
    // the same page with new data.
    const keyed = createElement(Fragment, { key: pathname }, content);
    return gated ? await SignedInLayout({ children: keyed }) : keyed;
  } catch (error) {
    if (error instanceof NotFoundSignal) return await NotFoundPage();
    throw error;
  }
}

interface View {
  element: ReactNode;
  pathname: string;
  search: string;
  language: Language;
  /** Changes with every committed render, so the error boundary lets go of an old failure. */
  serial: number;
}

let view: View | null = null;
const listeners = new Set<() => void>();
/** Where the app is going: what `refresh()` asks for again, even while a navigation is under way. */
let target = new URL(window.location.href);
let latest = 0;

type Mode = "push" | "replace" | "refresh" | "pop";

async function show(mode: Mode, href?: string): Promise<void> {
  const ticket = ++latest;
  let url = href === undefined ? new URL(target) : new URL(href, window.location.origin);
  if (mode !== "refresh") target = url;
  let history: Mode = mode;
  let element: ReactNode = null;
  for (let hop = 0; ; hop += 1) {
    const pathname = withoutBase(url.pathname) ?? "/";
    try {
      element = await renderPage(pathname, url.search);
      break;
    } catch (error) {
      if (error instanceof RedirectSignal && hop < 5) {
        url = new URL(withBase(error.href), window.location.origin);
        target = url;
        if (history !== "push") history = "replace";
        continue;
      }
      console.warn("[demo] page failed", pathname, error);
      element = <ErrorScreen error={error instanceof Error ? error : new Error(String(error))} reset={refresh} />;
      break;
    }
  }
  if (ticket !== latest) return;

  const address = url.pathname.replace(/(.)\/+$/, "$1") + url.search + url.hash;
  const here = window.location.pathname + window.location.search + window.location.hash;
  if (address !== here) {
    if (history === "push") window.history.pushState(null, "", address);
    else if (history !== "pop") window.history.replaceState(null, "", address);
  }
  const { t, language } = await requestI18n();
  document.documentElement.lang = language;
  document.title = t("shell.documentTitle");
  view = {
    element,
    pathname: withoutBase(url.pathname) ?? "/",
    search: url.search,
    language,
    serial: (view?.serial ?? 0) + 1,
  };
  for (const listener of listeners) listener();
  if (mode === "push") {
    const anchor = url.hash ? document.getElementById(url.hash.slice(1)) : null;
    if (anchor) anchor.scrollIntoView();
    else window.scrollTo(0, 0);
  }
}

function refresh(): void {
  void show("refresh");
}

const router: DemoRouter = {
  push: (href) => void show("push", withBase(href)),
  replace: (href) => void show("replace", withBase(href)),
  refresh,
  back: () => window.history.back(),
  forward: () => window.history.forward(),
  prefetch: () => {},
};

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A screen that throws while rendering gets the app's own error screen, not a blank page. */
class RenderBoundary extends Component<{ serial: number; children: ReactNode }, { serial: number; error: Error | null }> {
  override state: { serial: number; error: Error | null } = { serial: this.props.serial, error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  /** The next committed page is a fresh start. */
  static getDerivedStateFromProps(props: { serial: number }, state: { serial: number }) {
    return props.serial === state.serial ? null : { serial: props.serial, error: null };
  }

  override render(): ReactNode {
    if (this.state.error) return <ErrorScreen error={this.state.error} reset={refresh} />;
    return this.props.children;
  }
}

/** Called once, before the first render: the address bar, the anchors and the first page. */
export function startRouter(): void {
  window.addEventListener("popstate", () => void show("pop", window.location.href));
  const navigate = (href: string) => void show("push", href);
  installLinkHandling(navigate);
  const { pathname, search } = window.location;
  if (isBffPath(pathname)) {
    // A download link opened as a document (new tab, copied address): a static host has no file
    // there, so the overview opens and the handler's answer is saved from it.
    void show("replace", withBase("/")).then(() => save(new URL(pathname + search, window.location.origin), navigate));
    return;
  }
  void show("replace", window.location.href);
}

/** The language the page on screen was rendered in; follows the app's own switch. */
export function usePageLanguage(): Language {
  return useSyncExternalStore(subscribe, () => view?.language ?? currentLanguage());
}

export function AppRouter() {
  const current = useSyncExternalStore(subscribe, () => view);
  const navigation = useMemo(
    () => (current ? { router, pathname: current.pathname, search: current.search } : null),
    [current],
  );
  if (!current || !navigation) return null;
  return (
    <NavigationContext.Provider value={navigation}>
      <RenderBoundary serial={current.serial}>{current.element}</RenderBoundary>
    </NavigationContext.Provider>
  );
}
