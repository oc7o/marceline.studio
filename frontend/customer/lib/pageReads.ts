/**
 * How a page deals with a read that failed (ADR 0036).
 *
 * Three answers, and no fourth:
 *
 * - **signed out** is a redirect to sign-in;
 * - **"there is no such thing"** is the not-found page (an unknown id and another organisation's
 *   id are the same answer — ADR 0028 c's 404 rule);
 * - **anything else** — Django down, a 500, a timeout — is `null`, which the screen renders as
 *   "could not be loaded" for that section, or the page passes on so the gate's own
 *   "we could not reach the server" stands.
 *
 * A failed read must never look like "nothing was found": that is a statement about the customer's
 * work, and only Django gets to make it. Nothing here throws on a failure, because a throw in a
 * page is a bare 500 for the whole route.
 */
import "server-only";
import { notFound, redirect } from "next/navigation";

import { isNotFound, isSignedOut } from "./django";

/** The read may fail: `null` means the screen says "could not be loaded". */
export async function optional<T>(read: () => Promise<T>): Promise<T | null> {
  try {
    return await read();
  } catch (error) {
    if (isSignedOut(error)) redirect("/sign-in");
    return null;
  }
}

/**
 * The read addresses one thing by its id: a 404 renders the not-found page and stops the render;
 * any other failure is `null`, exactly as `optional`.
 */
export async function found<T>(read: () => Promise<T>): Promise<T | null> {
  try {
    return await read();
  } catch (error) {
    if (isSignedOut(error)) redirect("/sign-in");
    if (isNotFound(error)) notFound();
    return null;
  }
}
