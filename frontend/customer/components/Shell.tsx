/**
 * The customer app's chrome (ADR 0026's `CustomerApp`, as a server component — ADR 0036).
 *
 * A separate audience, not a widened staff screen: nothing here renders staff navigation, and
 * nothing here can load a copy of the material (issue #53). Three small islands — the language
 * switch, the navigation's "you are here" and the sign-out button — and each gets its words as
 * props, so no resource file is ever a client module.
 *
 * The frame on the design system's tokens: `surface-000` is the ground (the body's, so it reaches
 * past the frame), the header and the navigation are raised chrome on `surface-200`, and the
 * wordmark is the design system's lockup: the fang mark in `brand` beside the name set in the
 * display serif, in `ink`, unletterspaced and undecorated.
 */
import type { ReactNode } from "react";

import { LANGUAGES, type Language } from "@shared/i18n/language";

import type { Words } from "../lib/words";
import { LanguageSwitch } from "./LanguageSwitch";
import { Nav, type NavItem } from "./Nav";
import { SignOutButton } from "./SignOutButton";

function languageLabels(i18n: Words): Record<Language, string> {
  const labels = {} as Record<Language, string>;
  for (const language of LANGUAGES) labels[language] = i18n.t(`language.${language}`);
  return labels;
}

/**
 * The mark: two fangs on the design system's 214 × 80 grid (`assets/Brand/mark-brand.svg`), copied
 * path for path — never redrawn, never below 14px high. Decorative here: the name is set beside it.
 */
function BrandMark() {
  return (
    <svg
      viewBox="0 0 214 80"
      aria-hidden="true"
      className="h-4 w-auto shrink-0 fill-current text-brand"
    >
      <path d="M0,0 L50,80 L100,0 L84,0 L50,47 L16,0 Z" />
      <path d="M114,0 L164,80 L214,0 L198,0 L164,47 L130,0 Z" />
    </svg>
  );
}

/** The customer's places. Paths are relative to `basePath` (`/app`); `next/link` adds it. */
function navItems(i18n: Words): NavItem[] {
  return [
    { href: "/", label: i18n.t("nav.overview"), short: i18n.t("nav.short.overview"), exact: true },
    { href: "/removals", label: i18n.t("nav.removals"), short: i18n.t("nav.short.removals") },
    { href: "/notices", label: i18n.t("nav.notices"), short: i18n.t("nav.short.notices") },
    { href: "/invoices", label: i18n.t("nav.invoices"), short: i18n.t("nav.short.invoices") },
    { href: "/rights", label: i18n.t("nav.rights"), short: i18n.t("nav.short.rights") },
  ];
}

export function Shell({
  i18n,
  username,
  canExportData = false,
  canRequestData = false,
  askToVerifyEmail = false,
  children,
}: {
  i18n: Words;
  /** The signed-in customer's own name, or null on the sign-in page. */
  username: string | null;
  /** `me.can_export_data` (ADR 0045 amendment 2026-09-18): false on the sign-in page and on the
   * public host, where the export route is dev-only and would 404. */
  canExportData?: boolean;
  /** `me.can_request_data` (ADR 0056, issue #133): whether `/app/data` is worth linking to. Unlike
   * the download above this one **can** be true on the public host — it is the only way a creator
   * who never sees development can ask for a copy or a deletion at all. */
  canRequestData?: boolean;
  /**
   * `true` when this account's address is not confirmed yet **and** a confirmation can be asked for
   * (`me.email_verified === false && me.can_verify_email`, ADR 0054 §3). It renders one quiet line
   * and a link; it **blocks nothing**, and the link goes to a page rather than a button, so the
   * shell stays free of client JavaScript on every screen.
   */
  askToVerifyEmail?: boolean;
  children: ReactNode;
}) {
  const signedIn = username !== null;
  return (
    /*
      Signed in, the navigation is a bar fixed to the bottom of the screen below `sm` (`Nav`), so
      the page reserves its height here — 2.75 rem of bar plus whatever the device puts under it
      (an iPhone's home indicator). Without this the footer, and the last action of a long
      screen, sit underneath it.
    */
    <div
      className={`flex min-h-full flex-col ${
        signedIn ? "pb-[calc(2.75rem_+_env(safe-area-inset-bottom))] sm:pb-0" : ""
      }`}
    >
      {/*
        The first thing in the tab order, and invisible until it has focus: the header carries a
        language switch and a sign-out button, and without this a keyboard reader walks all of
        them plus the whole navigation before every single screen.
      */}
      <a
        href="#main"
        className="sr-only rounded-sm border border-brand bg-surface-100 px-3 py-2 text-button text-brand-ink focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-30"
      >
        {i18n.t("shell.skipToContent")}
      </a>
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-hairline bg-surface-200 px-4 py-2.5 sm:px-8 sm:py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 font-display text-display-s tracking-normal text-ink">
            <BrandMark />
            <span>
              {i18n.t("shell.brand")} <span className="text-brand-ink">Studio</span>
            </span>
          </div>
          {/*
            Below `sm` the frame is cut to what a phone needs to act: the tagline and the name of
            the account signed in are true, and neither is worth a row of a 640 px screen when
            the screen underneath it is the answer the customer came for. Both come back at `sm`.
          */}
          <p className="hidden text-caption text-ink-subtle sm:block">{i18n.t("shell.tagline")}</p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2 sm:gap-3">
          <LanguageSwitch
            current={i18n.language}
            groupLabel={i18n.t("language.group")}
            labels={languageLabels(i18n)}
          />
          {username === null ? null : (
            <>
              <span className="hidden text-body-s text-ink-muted sm:inline">
                {i18n.t("shell.signedInAs", { username })}
              </span>
              {canExportData ? (
                <a
                  href="/app/api/export"
                  download
                  className="rounded-sm text-body-s text-brand-ink underline"
                >
                  {i18n.t("shell.downloadData")}
                </a>
              ) : null}
              {/* The screen where a copy or a deletion is asked for (ADR 0056). A plain link, not a
                  sixth navigation place: it belongs with sign-out and the download, and the phone's
                  bottom bar has five slots of 72 px. Its label lives in the `dataRequests` slice
                  rather than in `core.ts`, although this renders on every page: only this app has
                  the screen, it renders on the server from the whole resource, and `core.ts` is what
                  every SPA `/app` visit pays for (budget 4.5 KB, nearly spent). */}
              {canRequestData ? (
                <a href="/app/data" className="rounded-sm text-body-s text-brand-ink underline">
                  {i18n.t("dataRequests.navLink")}
                </a>
              ) : null}
              <SignOutButton
                label={i18n.t("shell.signOut")}
                busyLabel={i18n.t("shell.signingOut")}
              />
            </>
          )}
        </div>
      </header>
      {username === null ? null : <Nav label={i18n.t("nav.label")} items={navItems(i18n)} />}
      {/*
        The address is not confirmed yet (ADR 0054 §3). A quiet line, not a warning: staff issued
        the invite *to* this address, nothing is refused over it, and the only thing it changes is
        whether we can rely on reaching the customer later. No island — a link, and the page it
        leads to holds the button.
      */}
      {askToVerifyEmail ? (
        <div className="border-b border-hairline bg-surface-200 px-4 py-2 text-body-s text-ink-muted sm:px-8">
          {i18n.t("shell.verifyEmail")}{" "}
          <a href="/app/verify-email" className="tap rounded-sm text-brand-ink underline">
            {i18n.t("shell.verifyEmailAction")}
          </a>
        </div>
      ) : null}
      {/*
        `tabIndex={-1}` so the skip link above actually moves focus here rather than only the
        scroll position — a landmark is not focusable by itself.
      */}
      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-3xl flex-1 p-4 sm:p-8">
        {children}
      </main>
      {/*
        The footer carries the one way to reach a human, on every page including sign-in and an
        invite: there is no signup and no password reset, eight strings in this app say "write to
        us" without ever naming an address, and the screen that asks for an export or a deletion
        (ADR 0056) is behind the session gate and switched off by default — somebody who cannot sign
        in still has to be able to reach a person. A `mailto:` makes no request of its own, so it
        costs the page nothing.
      */}
      <footer className="border-t border-hairline px-4 py-3 text-caption text-ink-subtle sm:px-8">
        {i18n.t("shell.noImagery")}{" "}
        <span className="block sm:inline">
          {i18n.t("shell.contact")}{" "}
          <a
            href={`mailto:${i18n.t("shell.contactEmail")}`}
            className="tap rounded-sm break-all text-brand-ink underline"
          >
            {i18n.t("shell.contactEmail")}
          </a>
        </span>
      </footer>
    </div>
  );
}
