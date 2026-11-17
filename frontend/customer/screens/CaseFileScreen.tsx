/**
 * The record of one matching copy (`/app/originals/{orig_…}/copies/{page_…}`) — the heart of the
 * customer UI (docs/customer-ui-plan.md §3): what happened and who did it, where the copy sits, why
 * we think it is the customer's, who hosts it, and what can be done.
 *
 * Honesty rules, each enforced by a test in `@shared/app/caseFile`'s own suite:
 * - every date is a date the API sent; a step it reports `unavailable` is rendered unavailable,
 *   with its reason, never as upcoming and never as done;
 * - an option without an endpoint is a disabled button that says why, never a button that
 *   pretends ("Prepare notice draft", not "Send": the customer sends from their own account);
 * - the hosting section names what is not known instead of showing an empty record;
 * - the page address is text plus one explicit new-tab link. No embed, no image, no preview.
 *
 * All server-rendered. The one interactive part is `<details>`, which is HTML, not JavaScript.
 */
import type { ReactNode } from "react";

import {
  type CaseOption,
  ESCALATION_LADDER,
  type Evidence,
  type Host,
  type OriginalKind,
  type ResponseHistory,
  type TimelineEntry,
  caseOptions,
  countryName,
  evidenceOf,
  extentExplanationKey,
  hostsOf,
  leakTimeline,
  whereabouts,
} from "@shared/app/caseFile";

import {
  Card,
  PageAddress,
  Timeline,
  type TimelineItem,
  WhereaboutsBadge,
  formatDate,
  formatDay,
} from "../components/parts";
import type { CaseDetail, CustomerFinding } from "../lib/reads";
import type { Words } from "../lib/words";

type Unknowns = CaseDetail["not_available"];

export function CaseFileScreen({
  i18n,
  detail,
  finding,
}: {
  i18n: Words;
  detail: CaseDetail;
  finding: CustomerFinding;
}) {
  const { t } = i18n;
  const { original } = detail;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <a
          href={`/app/originals/${original.id}`}
          className="tap rounded-sm text-body-s break-words text-brand-ink underline"
        >
          {t("caseFile.back", { title: original.title })}
        </a>
        <h1 className="mt-2 font-display text-display-m break-all text-ink">
          {t("caseFile.heading", { site: finding.site })}
        </h1>
        <p className="mt-1 text-body-s break-words text-ink-muted">
          {t("caseFile.of", { title: original.title })}
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          <WhereaboutsBadge whereabouts={whereabouts(finding)} i18n={i18n} />
        </div>
      </div>

      <WhereSection i18n={i18n} finding={finding} kind={original.kind} />
      <TimelineSection i18n={i18n} entries={leakTimeline(finding, detail.timeline)} />
      <WhySection i18n={i18n} evidence={evidenceOf(finding)} />
      <HostingSection i18n={i18n} finding={finding} />
      <OptionsSection
        i18n={i18n}
        notAvailable={detail.not_available}
        originalId={original.id}
      />
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 py-2 text-body-s sm:flex-row sm:gap-3">
      <dt className="shrink-0 text-label text-ink-muted sm:w-36 sm:pt-0.5">{label}</dt>
      <dd className="min-w-0 break-words text-ink">{children}</dd>
    </div>
  );
}

function WhereSection({
  i18n,
  finding,
  kind,
}: {
  i18n: Words;
  finding: CustomerFinding;
  kind: OriginalKind;
}) {
  const { t, language } = i18n;
  const stamp = (value: string) => formatDate(value, language);
  const where = whereabouts(finding);
  const status =
    where === "online"
      ? t("caseFile.where.online")
      : where === "gone"
        ? finding.removed_at
          ? t("caseFile.where.gone", { date: stamp(finding.removed_at) })
          : t("caseFile.where.goneUndated")
        : t("caseFile.where.unknown");

  return (
    <Card labelledBy="where-heading">
      <h2 id="where-heading" className="font-display text-display-s text-ink">
        {t("caseFile.where.heading")}
      </h2>
      <dl className="mt-2 divide-y divide-hairline">
        <Row label={t("caseFile.where.site")}>{finding.site}</Row>
        <Row label={t("caseFile.where.address")}>
          <PageAddress url={finding.url} i18n={i18n} />
        </Row>
        <Row label={t("finding.extent.label")}>
          <span>{t(`finding.extent.${finding.extent}.label`)}</span>
          <span className="mt-1 block text-caption text-ink-muted">
            {t(extentExplanationKey(finding.extent, kind))}
          </span>
        </Row>
        <Row label={t("caseFile.where.lastChecked")}>
          {finding.last_checked ? stamp(finding.last_checked) : t("caseFile.where.neverChecked")}
        </Row>
        <Row label={t("caseFile.where.status")}>
          <span>{status}</span>
          {finding.reappeared_at ? (
            <span className="mt-1 block text-caution-ink">
              {t("caseFile.where.reappeared", { date: stamp(finding.reappeared_at) })}
            </span>
          ) : null}
        </Row>
      </dl>
    </Card>
  );
}

/** One step of the record, in the customer's words, as the shared `Timeline` wants it. */
function timelineItem(entry: TimelineEntry, i18n: Words): TimelineItem {
  const { t, language } = i18n;
  return {
    step: entry.step,
    status: entry.status,
    title: t(`caseFile.timeline.steps.${entry.step}`),
    meta: (
      <>
        <span className="font-semibold">{t(`caseFile.timeline.status.${entry.status}`)}</span>
        {entry.status === "done" && entry.at ? <> · {formatDate(entry.at, language)}</> : null}
        {entry.status === "done" && entry.on ? <> · {formatDay(entry.on, language)}</> : null}
        {" · "}
        {t(`caseFile.actors.${entry.actor}`)}
      </>
    ),
    // Both, when both apply: a step can be unavailable *and* carry a copy that came back.
    note:
      entry.reason === null && entry.reappearedAt === null ? undefined : (
        <>
          {entry.status === "unavailable" && entry.reason
            ? t(`caseFile.reasons.${entry.reason}`)
            : null}
          {entry.reappearedAt ? (
            <span className="block text-caution-ink">
              {t("caseFile.timeline.reappeared", {
                date: formatDate(entry.reappearedAt, language),
              })}
            </span>
          ) : null}
        </>
      ),
  };
}

function TimelineSection({ i18n, entries }: { i18n: Words; entries: TimelineEntry[] }) {
  const { t } = i18n;
  return (
    <Card labelledBy="timeline-heading">
      <h2 id="timeline-heading" className="font-display text-display-s text-ink">
        {t("caseFile.timeline.heading")}
      </h2>
      <p className="mt-1 mb-3 text-body-s text-ink-muted">{t("caseFile.timeline.lead")}</p>
      <Timeline
        labelledBy="timeline-heading"
        items={entries.map((entry) => timelineItem(entry, i18n))}
      />
    </Card>
  );
}

function evidenceText(line: Evidence, i18n: Words): string {
  switch (line.kind) {
    case "fingerprint_match":
      return i18n.t("caseFile.why.fingerprintMatch");
    case "no_fingerprint_match":
      return i18n.t("caseFile.why.noMatch");
    case "confirmed":
      return i18n.t("caseFile.why.confirmed", { date: formatDay(line.on, i18n.language) });
  }
}

function WhySection({ i18n, evidence }: { i18n: Words; evidence: Evidence[] }) {
  return (
    <Card labelledBy="why-heading">
      <h2 id="why-heading" className="font-display text-display-s text-ink">
        {i18n.t("caseFile.why.heading")}
      </h2>
      <ul className="mt-2 flex flex-col gap-1 text-body-s text-ink">
        {evidence.map((line) => (
          <li key={line.kind} className="flex items-start gap-2">
            <span
              aria-hidden="true"
              className={`mt-2 h-1.5 w-1.5 shrink-0 rounded-full ${
                line.kind === "confirmed" ? "bg-border-control" : "bg-accent"
              }`}
            />
            <span>{evidenceText(line, i18n)}</span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-caption text-ink-subtle">{i18n.t("caseFile.why.noImagery")}</p>
    </Card>
  );
}

/**
 * Hosting (ADR 0027): who serves the page and its previews. What matters to a customer comes first
 * — provider, country and the suggested notice channel, the network in front, the abuse contact
 * with where it came from, the registrar, and the provider's rounded response history. Addresses
 * and the AS number sit in a collapsed "technical details". A missing fact reads "not known yet";
 * a host nobody has looked up says exactly that and shows no empty rows.
 *
 * Everything is text. The RDAP source record is shown as an address, not a link: the customer UI
 * links out only to the matched page itself, and only from "where it is".
 */
export function HostingSection({
  i18n,
  finding,
}: {
  i18n: Words;
  finding: Pick<CustomerFinding, "hosting">;
}) {
  const { t } = i18n;
  const hosts = hostsOf(finding);
  const [page, ...content] = hosts;
  return (
    <Card labelledBy="hosting-heading">
      <h2 id="hosting-heading" className="font-display text-display-s text-ink">
        {t("caseFile.hosting.heading")}
      </h2>
      <p className="mt-1 text-body-s text-ink-muted">{t("caseFile.hosting.lead")}</p>
      {page ? (
        <HostBlock
          i18n={i18n}
          host={page.host}
          title={t("caseFile.hosting.pageHost", { hostname: page.host.hostname })}
        />
      ) : null}
      {content.length > 0 ? (
        <div className="mt-4">
          <h3 className="text-label text-ink">{t("caseFile.hosting.contentHosts")}</h3>
          {content.map(({ host }) => (
            <HostBlock
              key={host.hostname}
              i18n={i18n}
              host={host}
              title={t("caseFile.hosting.contentHost", { hostname: host.hostname })}
            />
          ))}
        </div>
      ) : null}
    </Card>
  );
}

function NotKnown({ i18n }: { i18n: Words }) {
  return <span className="text-ink-subtle">{i18n.t("caseFile.hosting.notKnown")}</span>;
}

function HostBlock({ i18n, host, title }: { i18n: Words; host: Host; title: string }) {
  const { t, language } = i18n;
  const stamp = (value: string) => formatDate(value, language);
  const lookupNote =
    host.lookup === "complete" ? null : t(`caseFile.hosting.lookup.${host.lookup}`);

  return (
    <section
      aria-label={host.hostname}
      data-host={host.hostname}
      className="mt-3 rounded-md border border-hairline bg-surface-inset p-3"
    >
      <h4 className="text-label break-all text-ink">{title}</h4>
      {lookupNote ? <p className="mt-1 text-caption text-ink-muted">{lookupNote}</p> : null}
      {host.stale && host.looked_up_at ? (
        <p className="mt-1 text-caption text-caution-ink">
          {t("caseFile.hosting.stale", { date: stamp(host.looked_up_at) })}
        </p>
      ) : null}
      {host.lookup === "not_looked_up" ? null : (
        <>
          <dl className="mt-2 divide-y divide-hairline">
            <Row label={t("caseFile.hosting.facts.provider")}>
              {host.provider ?? <NotKnown i18n={i18n} />}
            </Row>
            <Row label={t("caseFile.hosting.facts.location")}>
              {host.country ? (
                <span className="block">{countryName(host.country, language)}</span>
              ) : null}
              <span className="block text-ink-muted">
                {t(`caseFile.hosting.jurisdiction.${host.jurisdiction}`)}
              </span>
            </Row>
            <Row label={t("caseFile.hosting.facts.cdn")}>
              {host.cdn
                ? t("caseFile.hosting.cdn", { cdn: host.cdn })
                : t("caseFile.hosting.noCdn")}
            </Row>
            <Row label={t("caseFile.hosting.facts.abuse_contact")}>
              {host.abuse_contact ? (
                <>
                  <span className="block break-all">{host.abuse_contact.email}</span>
                  <span className="block text-caption text-ink-muted">
                    {t(`caseFile.hosting.abuseSource.${host.abuse_contact.source}`)}
                  </span>
                  <span className="block font-mono text-mono-s break-all text-ink-subtle">
                    {t("caseFile.hosting.record", { url: host.abuse_contact.source_url })}
                  </span>
                </>
              ) : (
                <NotKnown i18n={i18n} />
              )}
            </Row>
            <Row label={t("caseFile.hosting.facts.registrar")}>
              {host.registrar ? (
                <>
                  <span className="block">{host.registrar.name}</span>
                  {host.registrar.abuse_email ? (
                    <span className="block text-caption break-all text-ink-muted">
                      {t("caseFile.hosting.registrarAbuse", {
                        email: host.registrar.abuse_email,
                      })}
                    </span>
                  ) : null}
                  <span className="block font-mono text-mono-s break-all text-ink-subtle">
                    {t("caseFile.hosting.record", { url: host.registrar.source_url })}
                  </span>
                </>
              ) : (
                <NotKnown i18n={i18n} />
              )}
            </Row>
            <Row label={t("caseFile.hosting.facts.response_history")}>
              <ResponseHistoryLine i18n={i18n} history={host.response_history} />
            </Row>
          </dl>
          <details className="mt-2 text-caption text-ink-muted">
            <summary className="w-fit cursor-pointer rounded-sm">
              {t("caseFile.hosting.technical.summary")}
            </summary>
            <dl className="mt-1">
              <Row label={t("caseFile.hosting.technical.addresses")}>
                <span className="font-mono break-all">
                  {host.addresses.length > 0
                    ? host.addresses.join(", ")
                    : t("caseFile.hosting.technical.none")}
                </span>
              </Row>
              <Row label={t("caseFile.hosting.technical.network")}>
                {host.asn !== null || host.as_name ? (
                  <span className="break-all">
                    {[host.asn !== null ? `AS${host.asn}` : null, host.as_name]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                ) : (
                  <NotKnown i18n={i18n} />
                )}
              </Row>
              {host.looked_up_at ? (
                <Row label={t("caseFile.hosting.technical.lookedUp")}>
                  {stamp(host.looked_up_at)}
                </Row>
              ) : null}
            </dl>
          </details>
        </>
      )}
    </section>
  );
}

/**
 * How past notices to this provider went, as the API rounded it: "about 75 % removed · usually 3+
 * days", or "no response history yet". Past tense, no band, no colour that reads as a verdict.
 */
export function ResponseHistoryLine({
  i18n,
  history,
}: {
  i18n: Words;
  history: ResponseHistory | null;
}) {
  const { t } = i18n;
  if (history === null) {
    return (
      <span data-history="none" className="text-ink-subtle">
        {t("caseFile.hosting.history.none")}
      </span>
    );
  }
  const time = history.usual_removal_time;
  return (
    <span data-history="shown" className="flex flex-col gap-0.5">
      <span>
        {t("caseFile.hosting.history.removed", { percent: history.removed_percent })}
        {time ? <> · {t(`caseFile.hosting.history.usually.${time}`)}</> : null}
      </span>
      <span className="text-caption text-ink-subtle">{t("caseFile.hosting.history.note")}</span>
    </span>
  );
}

function OptionsSection({
  i18n,
  notAvailable,
  originalId,
}: {
  i18n: Words;
  notAvailable: Unknowns;
  originalId: string;
}) {
  const { t } = i18n;
  return (
    <Card labelledBy="options-heading">
      <h2 id="options-heading" className="font-display text-display-s text-ink">
        {t("caseFile.options.heading")}
      </h2>
      <p className="mt-1 text-body-s text-ink-muted">{t("caseFile.options.lead")}</p>
      <ul className="mt-3 flex flex-col gap-3">
        {caseOptions(notAvailable).map((option) => (
          <li
            key={option.option}
            data-option={option.option}
            className="rounded-md border border-hairline p-3"
          >
            {option.option === "prepare_notice" ? (
              <PrepareNoticeOption i18n={i18n} originalId={originalId} />
            ) : (
              <OptionBody i18n={i18n} option={option} />
            )}
            {option.option === "prepare_notice" ? (
              <p className="mt-1 text-caption text-ink-subtle">{t("caseFile.options.delivery")}</p>
            ) : null}
            {option.option === "escalation" ? <EscalationLadder i18n={i18n} /> : null}
            {option.option === "reupload_watch" && option.reason ? (
              <p className="mt-1 text-caption text-ink-subtle">{t("caseFile.options.coverage")}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/**
 * The one option that is offered today: a link into the notice-draft screen for this record.
 *
 * The screen has existed here since ADR 0036 ported it, but nothing linked to it — the option was
 * rendered as the generic disabled affordance, so on the app production serves the whole feature
 * was unreachable. The SPA has had the link since ADR 0035 (`src/app/CaseFileScreen.tsx`).
 */
function PrepareNoticeOption({ i18n, originalId }: { i18n: Words; originalId: string }) {
  const { t } = i18n;
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <p className="text-body-s text-ink-muted">{t("caseFile.options.prepare_notice.body")}</p>
      </div>
      <a
        href={`/app/originals/${originalId}/notices`}
        className="inline-flex shrink-0 items-center gap-2 rounded-sm border border-brand bg-brand px-3 py-1.5 text-button text-on-brand transition-colors duration-120 ease-standard hover:bg-brand-hover"
      >
        {t("caseFile.options.prepare_notice.label")}
      </a>
    </div>
  );
}

/** A visible, disabled affordance that says why it cannot be used yet. */
function OptionBody({ i18n, option }: { i18n: Words; option: CaseOption }) {
  const { t } = i18n;
  const whyId = `option-${option.option}-why`;
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <p className="text-body-s text-ink">{t(`caseFile.options.${option.option}.body`)}</p>
        <p id={whyId} className="mt-1 text-caption font-medium text-ink-subtle">
          {option.reason
            ? t(`caseFile.reasons.${option.reason}`)
            : t("caseFile.options.notOffered")}
        </p>
      </div>
      <button
        type="button"
        disabled
        aria-describedby={whyId}
        className="shrink-0 cursor-not-allowed rounded-sm border border-border-control bg-surface-inset px-3 py-1.5 text-button text-ink-subtle opacity-45"
      >
        {t(`caseFile.options.${option.option}.label`)}
      </button>
    </div>
  );
}

function EscalationLadder({ i18n }: { i18n: Words }) {
  const { t } = i18n;
  return (
    <ol
      aria-label={t("caseFile.options.ladder.label")}
      className="mt-2 flex flex-wrap items-center gap-1 text-caption"
    >
      {ESCALATION_LADDER.map((rung, index) => (
        <li key={rung} className="flex items-center gap-1">
          {index > 0 ? <span aria-hidden="true">→</span> : null}
          <span className="rounded-sm border border-dashed border-hairline-strong px-1.5 py-0.5 text-ink-muted">
            {t(`caseFile.options.ladder.${rung}`)} · {t("caseFile.options.ladder.notStarted")}
          </span>
        </li>
      ))}
    </ol>
  );
}
