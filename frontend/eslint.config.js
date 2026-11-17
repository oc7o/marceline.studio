import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  // src/wasm holds the generated Emscripten build of Meta's PDQ hasher (frontend/wasm/pdq).
  globalIgnores(["dist", "node_modules", "src/api/schema.d.ts", "src/wasm/pdq-wasm.js"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, tseslint.configs.strict, reactHooks.configs.flat.recommended],
    languageOptions: { globals: globals.browser },
    rules: {
      // Images only ever come from our passthrough endpoint; see src/components/MediaGrid.tsx.
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='src'] > Literal[value=/^(https?:)?\\/\\//]",
          message: "No external src: images go through /api/v1/media/{id}/image only.",
        },
      ],
    },
  },
  {
    // Customer UI privacy rules, in code rather than in comments (issue #53;
    // docs/customer-ui-plan.md §4). The customer UI shows no copy of the pirated material in any
    // form, and it must be *incapable* of pointing at page media: the elements and helpers that
    // could load one are not available here at all. src/app/privacy.test.ts asserts the same thing
    // against the source text, so removing a rule from this file does not quietly remove the ban.
    files: ["src/app/**/*.{ts,tsx}"],
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
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["**/MediaGrid", "**/ImageModeToggle"],
              message:
                "Media components belong to the staff SPA; the customer UI shows no media (#53).",
            },
          ],
        },
      ],
    },
  },
]);
