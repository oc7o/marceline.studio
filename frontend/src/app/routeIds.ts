/**
 * The two route ids the customer shell has to reason about, in a module of their own so the shell
 * and the router do not import each other.
 *
 * They are TanStack's own ids (parent id + path), and `customerApp.test.tsx` asserts that both
 * still exist in the built route tree — so renaming a route breaks a test rather than silently
 * un-gating a screen.
 */
export const APP_ROUTE_ID = "/app";
export const APP_SIGN_IN_ROUTE_ID = "/app/sign-in";
