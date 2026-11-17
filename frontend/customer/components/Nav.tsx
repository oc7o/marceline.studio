"use client";
/**
 * The customer's navigation. An island for one reason: marking which item you are on needs the
 * current path, and that is the browser's. The words and the paths come from the server, so no
 * resource file travels with it.
 *
 * `href`s are relative to the app's `basePath` (`/app`), which `next/link` adds itself.
 *
 * **Two shapes, one markup** (issue #117, phone first). Below `sm` it is a bar fixed to the
 * bottom of the screen: five equal cells, each at least 44 px tall, within a thumb's reach of
 * where the hand already is. From `sm` up it is the tab row it always was, under the header.
 * Nothing about the DOM changes between the two — the same list, the same links, the same
 * island, no second copy for a screen reader to read twice and no JavaScript that measures
 * anything. `Shell` reserves the bar's height at the bottom of the page, so the bar covers
 * nothing; the safe-area inset is added *inside* the bar, under the last row of text, which is
 * where an iPhone's home indicator sits.
 *
 * Raised chrome (`surface-200`) like the header, and the "you are here" marker is the one piece
 * of red in the frame: a `brand` rule along the edge the bar is attached to — the top edge when
 * it is at the bottom of the screen, the bottom edge when it is under the header — behind a
 * `brand-ink` label. `aria-current` says the same thing without colour.
 *
 * The labels are the long ones ("Removals log", "Entfernungsprotokoll") from `sm` up and the
 * short ones ("Removals", "Entfernt") in the bar, because a fifth of 360 px is 72 px and a
 * twenty-character German compound in that cell would push the page sideways. Both spans are in
 * the DOM and one of them is `display: none` at any width, so exactly one reaches the
 * accessibility tree.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";

export interface NavItem {
  href: string;
  label: string;
  /** The same place, named short enough for a fifth of a phone screen. */
  short: string;
  /** Overview matches only itself; the others match their subtree. */
  exact?: boolean;
}

export function Nav({ label, items }: { label: string; items: readonly NavItem[] }) {
  const pathname = usePathname();
  return (
    <nav
      aria-label={label}
      className="fixed inset-x-0 bottom-0 z-20 border-t border-hairline-strong bg-surface-200 pb-[env(safe-area-inset-bottom)] sm:static sm:border-t-0 sm:border-b sm:px-8 sm:pb-0"
    >
      <ul className="mx-auto flex w-full max-w-3xl sm:flex-wrap sm:gap-4">
        {items.map((item) => {
          const active = item.exact
            ? pathname === item.href
            : pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <li key={item.href} className="min-w-0 flex-1 sm:flex-none">
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`flex min-h-11 items-center justify-center border-t-2 px-0.5 py-2 text-center text-caption leading-tight break-words transition-colors duration-120 ease-standard sm:-mb-px sm:min-h-0 sm:justify-start sm:border-t-0 sm:border-b-2 sm:px-1 sm:py-3 sm:text-label ${
                  active
                    ? "border-brand text-brand-ink"
                    : "border-transparent text-ink-muted hover:text-ink"
                }`}
              >
                <span className="sm:hidden">{item.short}</span>
                <span className="hidden sm:inline">{item.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
