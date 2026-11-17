/**
 * This app's own source files, read from disk — test support, imported by tests only.
 *
 * Three tests scan the source text rather than the module graph (`privacy.test.ts`,
 * `lazyFingerprinting.test.ts`, `routes.test.ts`), because what they forbid is a *construct*, not
 * a value: a media element, a static import of the fingerprinting code, a link out of `/app`.
 *
 * They used `import.meta.glob` (Vite's, via `vite/client` types). Next 16 declares an
 * `import.meta.glob` of its own with a different signature, so the two collided in `tsc`; reading
 * the files with `node:fs` needs no bundler API at all and is what `routes.test.ts` always did.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

/** The customer app's root (`frontend/customer/`). */
export const APP_ROOT = path.resolve(import.meta.dirname, "..");

/** Every file under `dir`, recursively, as absolute paths. */
export function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

/** The directories that hold code this app ships or renders. */
const CODE_DIRS = ["app", "components", "screens", "lib"] as const;

/**
 * Every `.ts`/`.tsx` file of ours, tests excluded — a test that names a forbidden construct in
 * order to forbid it must not fail on its own text. Paths are relative to the app root, so a
 * failure names the file the way a person would.
 */
export function sourceFiles(): { file: string; source: string }[] {
  return CODE_DIRS.flatMap((dir) => walk(path.join(APP_ROOT, dir)))
    .filter((file) => /\.tsx?$/.test(file) && !file.includes(".test."))
    .sort()
    .map((file) => ({
      file: path.relative(APP_ROOT, file),
      source: readFileSync(file, "utf8"),
    }));
}
