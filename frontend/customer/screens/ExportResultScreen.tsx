/**
 * `/app/export` (issue #131): the message a customer sees when "Download my data" did not result
 * in a file.
 *
 * `Shell`'s link always points at `/app/api/export`; a 200 there is a download and never reaches
 * this screen, so every `problem` rendered here is a real refusal Django sent — the export is
 * throttled to one per rolling hour, and the same 404 an account without an organisation gets
 * everywhere else. Before this the browser saved the refusal's JSON body as a file and the page
 * said nothing.
 */
import { type ExportProblem, exportMessageKey, isExportProblem } from "@shared/app/exportDownload";

import { LinkButton } from "../components/ui";
import type { Words } from "../lib/words";

/** The redirect's `?problem=` value, narrowed; anything unrecognised reads as the generic error. */
export function exportProblemFromParam(value: string | string[] | undefined): ExportProblem {
  const raw = Array.isArray(value) ? value[0] : value;
  return raw !== undefined && isExportProblem(raw) ? raw : "error";
}

export function ExportResultScreen({ i18n, problem }: { i18n: Words; problem: ExportProblem }) {
  const { t } = i18n;
  return (
    <section aria-labelledby="export-heading" className="flex flex-col gap-3">
      <h1 id="export-heading" className="font-display text-display-m text-ink">
        {t("shell.export.heading")}
      </h1>
      <p className="max-w-[68ch] text-body text-ink-muted">{t(exportMessageKey(problem))}</p>
      <LinkButton href="/app">{t("nav.overview")}</LinkButton>
    </section>
  );
}
