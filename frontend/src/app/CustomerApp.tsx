/**
 * The customer app shell (`/app`, ADR 0026).
 *
 * A separate audience, not a widened staff screen: the staff SPA is a queue for someone who
 * decides, this is an answer for someone whose work is being stolen. Nothing here renders staff
 * navigation, and nothing here can load a copy of the material (issue #53) — the elements that
 * could are banned by eslint and asserted absent by `privacy.test.ts`.
 */
import { Link, Navigate, Outlet, useRouterState } from "@tanstack/react-router";
import { type ReactNode, useEffect } from "react";
import { I18nextProvider, useTranslation } from "react-i18next";

import { Button } from "../components/ui";
import { i18n } from "../i18n";
import { LanguageSwitch } from "./LanguageSwitch";
import { useCustomerAccount, useCustomerLogout, useCustomerSession } from "./queries";
import { APP_SIGN_IN_ROUTE_ID } from "./routeIds";

/**
 * Route component for `/app`: the only place the customer i18n instance is mounted, and the one
 * place the session gate is applied.
 *
 * The gate wraps the outlet for **every** `/app` route except sign-in itself, so a screen added
 * under `/app` is behind it by construction rather than by remembering to check.
 */
export function CustomerApp() {
  const onSignIn = useRouterState({
    select: (state) => state.matches.some((match) => match.routeId === APP_SIGN_IN_ROUTE_ID),
  });
  return (
    <I18nextProvider i18n={i18n}>
      <CustomerShell>
        {onSignIn ? (
          <Outlet />
        ) : (
          <RequireCustomerSession>
            <Outlet />
          </RequireCustomerSession>
        )}
      </CustomerShell>
    </I18nextProvider>
  );
}

function CustomerShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const session = useCustomerSession();
  const logout = useCustomerLogout();
  const signedIn = session.data?.authenticated === true;
  // Only asked once signed in: this shell also wraps the sign-in page, and `/app/me` needs a
  // session. `can_export_data` is false on the public host (ADR 0045 amendment 2026-09-18).
  const account = useCustomerAccount({ enabled: signedIn });

  useEffect(() => {
    document.title = t("shell.documentTitle");
  }, [t]);

  return (
    /*
      This rollback copy of the customer app (`CUSTOMER_APP=spa`, ADR 0028) keeps its own light
      ground explicitly. Since 2026-09-18 the staff SPA's stylesheet carries the design system's
      tokens in both themes, so `body` follows the reader's system setting; these screens are on
      Tailwind's own palette and are on their way out with `src/app/**` (ADR 0036), so they pin the
      ground they were drawn for instead of being converted twice.
    */
    <div className="flex min-h-full flex-col bg-slate-50 text-slate-900">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-slate-200 bg-white px-5 py-3">
        <div>
          <div className="text-lg font-bold tracking-tight">{t("shell.brand")}</div>
          <p className="text-xs text-slate-500">{t("shell.tagline")}</p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-3">
          <LanguageSwitch />
          {signedIn ? (
            <>
              <span className="text-sm text-slate-600">
                {t("shell.signedInAs", { username: session.data?.username ?? "" })}
              </span>
              {account.data?.can_export_data ? (
                <a href="/api/v1/app/export" download className="text-sm text-sky-800 underline">
                  {t("shell.downloadData")}
                </a>
              ) : null}
              <Button onClick={() => logout.mutate()} disabled={logout.isPending}>
                {logout.isPending ? t("shell.signingOut") : t("shell.signOut")}
              </Button>
            </>
          ) : null}
        </div>
      </header>
      {signedIn ? <CustomerNav /> : null}
      <main className="mx-auto w-full max-w-3xl flex-1 p-4 sm:p-5">{children}</main>
      {/* The one way to reach a human, on every page — see `customer/components/Shell.tsx`. */}
      <footer className="border-t border-slate-200 px-5 py-3 text-xs text-slate-500">
        {t("shell.noImagery")}{" "}
        <span className="block sm:inline">
          {t("shell.contact")}{" "}
          <a href={`mailto:${t("shell.contactEmail")}`} className="break-all text-sky-800 underline">
            {t("shell.contactEmail")}
          </a>
        </span>
      </footer>
    </div>
  );
}

/** The customer's places: their originals (with the case files below them), the log, invoices. */
function CustomerNav() {
  const { t } = useTranslation();
  const linkClass = "rounded px-2 py-1 text-sm font-medium text-slate-700 hover:bg-slate-100";
  const activeProps = { className: "bg-slate-900 text-white hover:bg-slate-900" };
  return (
    <nav aria-label={t("nav.label")} className="border-b border-slate-200 bg-white px-4 py-2 sm:px-5">
      <ul className="mx-auto flex w-full max-w-3xl flex-wrap gap-1">
        <li>
          <Link to="/app" activeOptions={{ exact: true }} className={linkClass} activeProps={activeProps}>
            {t("nav.overview")}
          </Link>
        </li>
        <li>
          <Link to="/app/removals" className={linkClass} activeProps={activeProps}>
            {t("nav.removals")}
          </Link>
        </li>
        <li>
          <Link to="/app/notices" className={linkClass} activeProps={activeProps}>
            {t("nav.notices")}
          </Link>
        </li>
        <li>
          <Link to="/app/invoices" className={linkClass} activeProps={activeProps}>
            {t("nav.invoices")}
          </Link>
        </li>
        <li>
          <Link to="/app/rights" className={linkClass} activeProps={activeProps}>
            {t("nav.rights")}
          </Link>
        </li>
      </ul>
    </nav>
  );
}

/** Nothing but the sign-in screen renders without a customer session. */
function RequireCustomerSession({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const session = useCustomerSession();
  if (session.isPending) return <p className="text-slate-500">{t("common.loading")}</p>;
  if (session.isError) {
    return (
      <p role="alert" className="text-rose-800">
        {t("common.unreachable")}
      </p>
    );
  }
  if (!session.data.authenticated) return <Navigate to="/app/sign-in" replace />;
  return children;
}
