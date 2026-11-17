/**
 * `next/link`: an anchor whose `href` gets the base path. The click itself is taken by the one
 * listener in `links.ts`, which treats every in-app anchor alike.
 */
import type { AnchorHTMLAttributes } from "react";

import { withBase } from "./paths";

export default function Link({ href, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  return <a href={withBase(href)} {...rest} />;
}
