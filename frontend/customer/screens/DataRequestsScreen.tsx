/**
 * Asking for a copy of your data, or for its deletion (`/app/data`, ADR 0056, issue #133).
 *
 * A synchronous **server** component: the standing of each request, its date, the statutory
 * deadline and the address are HTML, because none of them needs a browser. One island sits inside
 * it — `DataRequestForms` — for the three writes (ask for a copy, ask for a deletion, take either
 * back).
 *
 * What the words may not do here: say that anything has been exported or deleted, promise a moment,
 * or suggest that pressing a button does either. Both requests are answered by a person, and the
 * screen's job is to make the asking real and the record visible — which is precisely what an
 * invited creator on the public host did not have (`can_export_data` is false there).
 */
import { DataRequestForms } from "../components/DataRequestForms";
import { formatDate } from "../components/parts";
import { Banner } from "../components/ui";
import type { DataRequest, DataRequests } from "../lib/reads";
import { type Resource, slice } from "../lib/translate";
import type { Words } from "../lib/words";

/** Everything settled, newest first — the record of what was asked before. The island has the
 * mirror of this for the open ones, because it is the part that has a button. */
export function settledOf(status: DataRequests): DataRequest[] {
  return status.requests.filter((row) => !row.open);
}

export function DataRequestsScreen({
  i18n,
  words,
  status,
}: {
  i18n: Words;
  /** The island's own words: the resource slice, never the whole resource (ADR 0036). */
  words: Resource;
  status: DataRequests;
}) {
  const { t, language } = i18n;
  const settled = settledOf(status);
  return (
    <section aria-labelledby="data-heading" className="flex flex-col gap-4">
      <div>
        <h1 id="data-heading" className="font-display text-display-m text-ink">
          {t("dataRequests.heading")}
        </h1>
        <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("dataRequests.lead")}</p>
      </div>

      {/* The deadline and the address are standing context, not a status: a note. Both come from
          the server (`contact_email`), so no string in this app spells the address itself. */}
      <Banner role="note">
        {t("dataRequests.deadline")}{" "}
        {t("dataRequests.contact", { email: status.contact_email })}
      </Banner>

      <DataRequestForms language={language} words={words} status={status} />

      {settled.length > 0 ? (
        <div className="flex flex-col gap-2">
          <h2 className="text-label text-ink-muted">{t("dataRequests.history.heading")}</h2>
          <ul className="flex flex-col gap-2">
            {settled.map((row) => (
              <li key={`${row.kind}-${row.requested_at}`}>
                <article
                  data-kind={row.kind}
                  data-status={row.status}
                  className="rounded-none border border-hairline bg-surface-100 p-3"
                >
                  <div className="text-body-l font-medium text-ink">
                    {t(`dataRequests.${row.kind}.heading`)}
                  </div>
                  <p className="mt-1 text-body-s text-ink">
                    {t(`dataRequests.status.${row.status}`)}
                  </p>
                  <p className="mt-1 text-caption text-ink-subtle">
                    {t("dataRequests.history.requestedOn", {
                      date: formatDate(row.requested_at, language),
                    })}
                    {row.settled_at
                      ? ` · ${t("dataRequests.history.settledOn", {
                          date: formatDate(row.settled_at, language),
                        })}`
                      : ""}
                  </p>
                </article>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

/** The slice this screen's island needs, in one place so the page cannot pass the wrong one. */
export function dataRequestWords(resource: Resource): Resource {
  return slice(resource, "dataRequests", "common");
}
