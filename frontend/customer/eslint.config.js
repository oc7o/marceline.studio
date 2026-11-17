/**
 * Lint rules for the Next.js customer app (ADR 0036).
 *
 * The privacy block is the same one `frontend/eslint.config.js` applies to the Vite SPA's
 * `src/app/**` (issue #53): the customer UI shows no copy of the pirated material and must be
 * *incapable* of pointing at one. `lib/privacy.test.ts` asserts the same thing against the source
 * text, so removing a rule here does not quietly remove the ban.
 */
import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  globalIgnores([".next", "node_modules", "next-env.d.ts"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, tseslint.configs.strict, reactHooks.configs.flat.recommended],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "JSXOpeningElement[name.name=/^(img|image|picture|source|video|audio|iframe|embed|object|canvas)$/]",
          message:
            "No media elements in the customer UI: it must be incapable of loading a pirated copy (#53).",
        },
        {
          selector: "JSXAttribute[name.name=/^(src|srcSet|poster)$/]",
          message: "No media sources in the customer UI: it renders no images at all (#53).",
        },
        {
          selector: "Identifier[name='mediaImageUrl']",
          message: "mediaImageUrl builds a media URL; the customer UI shows no media (#53).",
        },
        {
          selector: "Property[key.name='backgroundImage']",
          message: "No background images in the customer UI (#53).",
        },
        {
          selector: "JSXAttribute[name.name='href'] > Literal[value=/^(https?:)?\\/\\//]",
          message: "Same-origin only: no external links, CDNs or fonts in the customer UI.",
        },
      ],
    },
  },
  {
    // The scripts and config files are Node, not a React tree.
    files: ["scripts/**/*.mjs", "*.config.{js,ts,mjs}"],
    languageOptions: { globals: globals.node },
  },
]);
