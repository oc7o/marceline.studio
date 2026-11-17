"use client";
/**
 * The explicit language choice. Always visible, signed in or not: someone who cannot read the
 * sign-in form cannot sign in to change the language.
 *
 * An island, and a small one: it writes the `marceline.language` cookie the server reads
 * (`lib/requestLanguage.ts`) and asks the server for this page again in the new language. No
 * resource file comes with it — the two labels arrive as props, already in the right words.
 */
import { useRouter } from "next/navigation";
import { useTransition } from "react";

import { LANGUAGES, type Language } from "@shared/i18n/language";

import { rememberLanguage } from "../lib/languageCookie";

export function LanguageSwitch({
  current,
  groupLabel,
  labels,
}: {
  current: Language;
  groupLabel: string;
  labels: Record<Language, string>;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  const choose = (language: Language) => {
    if (language === current) return;
    rememberLanguage(language);
    startTransition(() => {
      router.refresh();
    });
  };

  return (
    <div role="group" aria-label={groupLabel} className="flex items-center gap-1">
      {LANGUAGES.map((language) => {
        const active = language === current;
        return (
          <button
            key={language}
            type="button"
            lang={language}
            aria-pressed={active}
            disabled={pending}
            onClick={() => choose(language)}
            /* `inline-flex items-center` so the 44 px floor a finger-driven browser gets
               (`globals.css`) centres this label instead of leaving it at the top of the box. */
            className={`inline-flex items-center rounded-sm px-2 py-1 text-label transition-colors duration-120 ease-standard disabled:opacity-45 ${
              active ? "bg-ink text-ink-inverse" : "text-ink-muted hover:bg-surface-300"
            }`}
          >
            {labels[language]}
          </button>
        );
      })}
    </div>
  );
}
