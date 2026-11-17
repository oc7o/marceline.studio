import { useTranslation } from "react-i18next";

import { useLanguage } from "../i18n";
import { LANGUAGES } from "../i18n/language";

/**
 * The explicit language choice. Always visible, signed in or not: someone who cannot read the
 * sign-in form cannot sign in to change the language.
 */
export function LanguageSwitch() {
  const { t } = useTranslation();
  const [current, setLanguage] = useLanguage();
  return (
    <div role="group" aria-label={t("language.group")} className="flex items-center gap-1">
      {LANGUAGES.map((language) => {
        const active = language === current;
        return (
          <button
            key={language}
            type="button"
            lang={language}
            aria-pressed={active}
            onClick={() => setLanguage(language)}
            className={`rounded px-2 py-1 text-xs font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 ${
              active ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"
            }`}
          >
            {t(`language.${language}`)}
          </button>
        );
      })}
    </div>
  );
}
