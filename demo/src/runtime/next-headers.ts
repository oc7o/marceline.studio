/**
 * `next/headers` for a page with no server: the cookie jar is `document.cookie`, and the only
 * request header the app reads for a page is `Accept-Language`.
 */
import { readCookie } from "../../../frontend/customer/lib/csrf";
import { acceptLanguage } from "./language";

export function cookies(): Promise<{ get(name: string): { name: string; value: string } | undefined }> {
  return Promise.resolve({
    get(name) {
      const value = readCookie(name, document.cookie);
      return value === null ? undefined : { name, value };
    },
  });
}

export function headers(): Promise<Headers> {
  return Promise.resolve(new Headers({ "accept-language": acceptLanguage(), host: window.location.host }));
}
