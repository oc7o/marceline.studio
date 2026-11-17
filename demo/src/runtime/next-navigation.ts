/**
 * `next/navigation`, as much of it as the customer app uses. `redirect` and `notFound` throw a
 * marker the demo's router catches while it is running a page, exactly where Next catches its own.
 */
import { createContext, useContext } from "react";

export interface DemoRouter {
  push(href: string): void;
  replace(href: string): void;
  refresh(): void;
  back(): void;
  forward(): void;
  prefetch(href: string): void;
}

export interface Navigation {
  router: DemoRouter;
  /** Without the `/app` base path, as Next reports it. */
  pathname: string;
  search: string;
}

export const NavigationContext = createContext<Navigation | null>(null);

function useNavigation(): Navigation {
  const navigation = useContext(NavigationContext);
  if (navigation === null) throw new Error("rendered outside the demo router");
  return navigation;
}

export function useRouter(): DemoRouter {
  return useNavigation().router;
}

export function usePathname(): string {
  return useNavigation().pathname;
}

export function useSearchParams(): URLSearchParams {
  return new URLSearchParams(useNavigation().search);
}

export class RedirectSignal extends Error {
  readonly href: string;
  constructor(href: string) {
    super(`redirect to ${href}`);
    this.href = href;
  }
}

export class NotFoundSignal extends Error {
  constructor() {
    super("not found");
  }
}

export function redirect(href: string): never {
  throw new RedirectSignal(href);
}

export function notFound(): never {
  throw new NotFoundSignal();
}
