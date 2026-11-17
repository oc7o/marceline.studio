/**
 * The staff frame: the login gate, the queue navigation with its open counts, and the header.
 *
 * On the design system's tokens (`src/index.css`). Two of its rules shape this file: the chrome is
 * raised (`surface-200`) above the working ground (`surface-000`) and separated by hairlines rather
 * than by shadow, and **the active nav marker is one of the few things allowed to spend the brand
 * red** — everything else in here is neutral.
 */
import { Link, Outlet } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";

import { errorMessage } from "../api/client";
import { useCounts, useLogin, useLogout, useMe } from "../api/queries";
import { ImageModeToggle } from "./ImageModeToggle";
import { Button } from "./ui";

export function RootLayout() {
  const me = useMe();
  if (me.isPending) return <p className="p-8 text-ink-muted">Loading…</p>;
  if (me.isError) {
    return <p className="p-8 text-critical-ink">Cannot reach the API: {errorMessage(me.error)}</p>;
  }
  if (!me.data.authenticated || !me.data.is_staff) return <LoginScreen />;
  return (
    <Shell username={me.data.username ?? ""}>
      <Outlet />
    </Shell>
  );
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

function Shell({ username, children }: { username: string; children: React.ReactNode }) {
  const counts = useCounts();
  const logout = useLogout();
  const nav = [
    { to: "/", label: "Overview" },
    { to: "/references", label: "Reference leaks", count: counts.data?.references },
    { to: "/clusters", label: "Clusters", count: counts.data?.clusters },
    {
      to: "/safety",
      label: "Safety review",
      count: counts.data ? counts.data.safety + counts.data.safety_release : undefined,
    },
    { to: "/creators", label: "Creators to review", count: counts.data?.creators },
    { to: "/claims", label: "Refused claims", count: counts.data?.claims },
    { to: "/common-frames", label: "Common frames", count: counts.data?.common_frames },
    { to: "/originals", label: "Originals" },
    { to: "/candidates", label: "Source candidates" },
  ] as const;
  return (
    <div className="flex h-full min-h-0">
      <nav
        aria-label="Queues"
        className="flex w-52 shrink-0 flex-col border-r border-hairline bg-surface-200"
      >
        {/* The lockup: the fang mark beside the name in the display face, never below display-s. */}
        <div className="flex items-center gap-2 px-4 py-4 font-display text-display-s text-ink">
          <BrandMark />
          <span>
            Marceline <span className="text-brand-ink">Studio</span>
          </span>
        </div>
        <ul className="flex flex-col gap-1 px-2">
          {nav.map((item) => (
            <li key={item.to}>
              <Link
                to={item.to}
                activeOptions={{ exact: true, includeSearch: false }}
                className="group flex items-center justify-between rounded-sm px-3 py-2 text-body-s text-ink-muted transition-colors duration-120 ease-standard hover:bg-surface-300 hover:text-ink"
                activeProps={{
                  className: "bg-brand text-on-brand hover:bg-brand-hover hover:text-on-brand",
                  "aria-current": "page",
                }}
              >
                <span>{item.label}</span>
                {"count" in item ? (
                  <span
                    className="rounded-full bg-surface-300 px-2 text-overline text-ink group-aria-[current=page]:bg-on-brand/20 group-aria-[current=page]:text-on-brand"
                    aria-label={`${item.count ?? 0} open`}
                  >
                    {item.count ?? "…"}
                  </span>
                ) : null}
              </Link>
            </li>
          ))}
        </ul>
        <p className="mt-auto px-4 py-4 text-caption text-ink-subtle">
          Internal moderation. Press <kbd className="font-mono text-mono-s">?</kbd> for shortcuts.
        </p>
      </nav>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-hairline bg-surface-200 px-5 py-2.5">
          <ImageModeToggle />
          <div className="flex items-center gap-3 text-body-s text-ink-muted">
            <span>{username}</span>
            <Button onClick={() => logout.mutate()} disabled={logout.isPending}>
              Log out
            </Button>
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-auto bg-surface-000 p-5">{children}</main>
      </div>
    </div>
  );
}

function LoginScreen() {
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate({ username, password });
  };
  return (
    <div className="flex h-full items-center justify-center bg-surface-000 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border border-hairline bg-surface-100 p-6 shadow-md"
      >
        <h1 className="mb-4 font-display text-display-s">Marceline review</h1>
        <label className="mb-3 block text-body-s">
          <span className="mb-1 block text-ink-muted">Username</span>
          <input
            className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-ink"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
            autoFocus
          />
        </label>
        <label className="mb-4 block text-body-s">
          <span className="mb-1 block text-ink-muted">Password</span>
          <input
            className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-ink"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {login.isError ? (
          <p role="alert" className="mb-3 text-body-s text-critical-ink">
            {errorMessage(login.error)}
          </p>
        ) : null}
        <Button type="submit" tone="primary" disabled={login.isPending} className="w-full justify-center">
          Log in
        </Button>
        <p className="mt-3 text-caption text-ink-subtle">Staff accounts only (Django admin users).</p>
      </form>
    </div>
  );
}
