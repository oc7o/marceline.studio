/**
 * What to tell someone whose sign-in failed — every response ADR 0023 documents, and nothing more
 * than it documents. Pure, so each branch is pinned by a test rather than by clicking.
 */
import { ApiError } from "../api/errors";

export interface SignInMessage {
  /** Translation key. */
  key: string;
  /** Interpolation values that key needs. */
  values?: Record<string, number>;
}

const SECONDS_PER_MINUTE = 60;

/** A 429 states the wait. It is shown, never acted on: no timer, no automatic second attempt. */
export function rateLimitedMessage(retryAfterSeconds: number | null): SignInMessage {
  if (retryAfterSeconds === null || retryAfterSeconds <= 0) {
    return { key: "signIn.errors.rateLimited" };
  }
  if (retryAfterSeconds < SECONDS_PER_MINUTE) {
    return { key: "signIn.errors.rateLimitedSeconds", values: { count: retryAfterSeconds } };
  }
  return {
    key: "signIn.errors.rateLimitedMinutes",
    values: { count: Math.ceil(retryAfterSeconds / SECONDS_PER_MINUTE) },
  };
}

/** A 403 from the login endpoint means one of two unrelated things. */
function isCsrfFailure(error: ApiError): boolean {
  return /csrf/i.test(error.message);
}

export function signInMessage(error: unknown): SignInMessage {
  if (!(error instanceof ApiError)) return { key: "common.unreachable" };
  switch (error.status) {
    case 401:
      // One answer for an unknown account, a wrong password and a disabled account. The backend is
      // deliberately indistinguishable across the three (ADR 0023); saying which one it was here
      // would undo that and turn the form into an account-enumeration oracle.
      return { key: "signIn.errors.invalid" };
    case 403:
      // A stale CSRF token, or a correct password on an account with no organisation. Only the
      // second is about the account, and it is safe to say: it follows a correct password.
      return isCsrfFailure(error)
        ? { key: "signIn.errors.formExpired" }
        : { key: "signIn.errors.noOrganisation" };
    case 429:
      return rateLimitedMessage(error.retryAfterSeconds);
    default:
      return { key: "signIn.errors.unexpected", values: { status: error.status } };
  }
}
