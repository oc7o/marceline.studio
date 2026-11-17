/**
 * The case file of one copy (`/app/originals/$originalId/copies/$pageId`) — the heart of the
 * customer UI (docs/customer-ui-plan.md §3): what happened and who did it, where the copy sits,
 * why we think it is the customer's, who hosts it, and what can be done.
 *
 * Honesty rules, each enforced by a test:
 * - every date is a date the API sent; a step it reports `unavailable` is rendered unavailable,
 *   with its reason, never as upcoming and never as done;
 * - an option without an endpoint is a disabled button that says why, never a button that
 *   pretends ("Prepare notice", not "Send": Marceline transmits nothing, the customer sends);
 * - the hosting section names what is not known instead of showing an empty record;
 * - the page address is text plus one explicit new-tab link. No embed, no image, no preview.
 */
import { Link, useParams } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  type CaseOption,
  ESCALATION_LADDER,
  type Evidence,
  type Host,
  type OriginalKind,
  type ResponseHistory,
  type TimelineEntry,
  caseOptions,
  evidenceOf,
  extentExplanationKey,
  countryName,
  hostsOf,
  leakTimeline,
  parsePublicId,
  whereabouts,
} from "./caseFile";
import {
  Card,
  LoadError,
  Loading,
  NotFound,
  PageAddress,
  WhereaboutsBadge,
  isNotFound,
  useFormatDate,
  useFormatDay,
} from "./parts";
import { useLanguage } from "../i18n";
import { type CaseDetail, type CaseUnknown, type CustomerFinding, useCustomerCase } from "./queries";

export function CaseFileScreen() {
  const params = useParams({ from: "/app/originals/$originalId/copies/$pageId" });
  const originalId = parsePublicId("orig", params.originalId);
  const pageId = parsePublicId("page", params.pageId);
  if (originalId === null || pageId === null) return <NotFound what="caseFile" />;
  return <CaseFileLoader originalId={originalId} pageId={pageId} />;
}

function CaseFileLoader({ originalId, pageId }: { originalId: string; pageId: string }) {
  const detail = useCustomerCase(originalId);
  if (detail.isPending) return <Loading />;
  if (detail.isError) {
    return isNotFound(detail.error) ? <NotFound what="caseFile" /> : <LoadError onRetry={() => void detail.refetch()} />;
  }
  // A page that is not among this original's findings — including one hidden by the safety gate,
  // which the API never sends — is simply not found. There is no other code path to it.
  const finding = detail.data.findings.find((row) => row.page_id === pageId);
  if (!finding) return <NotFound what="caseFile" />;
  return <CaseFileView detail={detail.data} finding={finding} />;
}

function CaseFileView({ detail, finding }: { detail: CaseDetail; finding: CustomerFinding }) {
  const { t } = useTranslation();
  const { original } = detail;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <Link
          to="/app/originals/$originalId"
          params={{ originalId: original.id }}
          className="text-sm break-words text-sky-800 underline"
        >
          {t("caseFile.back", { title: original.title })}
        </Link>
        <h1 className="mt-2 text-xl font-bold break-all">{t("caseFile.heading", { site: finding.site })}</h1>
        <p className="mt-1 text-sm break-words text-slate-600">{t("caseFile.of", { title: original.title })}</p>
        <div className="mt-2 flex flex-wrap gap-2">
          <WhereaboutsBadge whereabouts={whereabouts(finding)} />
        </div>
      </div>

      <WhereSection finding={finding} kind={original.kind} />
      <TimelineSection entries={leakTimeline(finding, detail.timeline)} />
      <WhySection evidence={evidenceOf(finding)} />
      <HostingSection finding={finding} />
      <OptionsSection notAvailable={detail.not_available} originalId={original.id} />
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 py-1.5 text-sm sm:flex-row sm:gap-3">
      <dt className="shrink-0 text-slate-500 sm:w-36">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function WhereSection({ finding, kind }: { finding: CustomerFinding; kind: OriginalKind }) {
  const { t } = useTranslation();
  const formatDate = useFormatDate();
  const where = whereabouts(finding);
  const status =
    where === "online"
      ? t("caseFile.where.online")
      : where === "gone"
        ? finding.removed_at
          ? t("caseFile.where.gone", { date: formatDate(finding.removed_at) })
          : t("caseFile.where.goneUndated")
        : t("caseFile.where.unknown");

  return (
    <Card labelledBy="where-heading">
      <h2 id="where-heading" className="font-semibold">
        {t("caseFile.where.heading")}
      </h2>
      <dl className="mt-2 divide-y divide-slate-100">
        <Row label={t("caseFile.where.site")}>{finding.site}</Row>
        <Row label={t("caseFile.where.address")}>
          <PageAddress url={finding.url} />
        </Row>
        <Row label={t("finding.extent.label")}>
          <span>{t(`finding.extent.${finding.extent}.label`)}</span>
          <span className="mt-1 block text-slate-600">{t(extentExplanationKey(finding.extent, kind))}</span>
        </Row>
        <Row label={t("caseFile.where.lastChecked")}>
          {finding.last_checked ? formatDate(finding.last_checked) : t("caseFile.where.neverChecked")}
        </Row>
        <Row label={t("caseFile.where.status")}>
          <span>{status}</span>
          {finding.reappeared_at ? (
            <span className="mt-1 block text-amber-900">
              {t("caseFile.where.reappeared", { date: formatDate(finding.reappeared_at) })}
            </span>
          ) : null}
        </Row>
      </dl>
    </Card>
  );
}

const STATUS_MARKER: Record<TimelineEntry["status"], string> = {
  done: "border-emerald-600 bg-emerald-600",
  pending: "border-slate-400 bg-white",
  unavailable: "border-dashed border-slate-300 bg-slate-100",
};

function TimelineSection({ entries }: { entries: TimelineEntry[] }) {
  const { t } = useTranslation();
  const formatDate = useFormatDate();
  const formatDay = useFormatDay();
  return (
    <Card labelledBy="timeline-heading">
      <h2 id="timeline-heading" className="font-semibold">
        {t("caseFile.timeline.heading")}
      </h2>
      <p className="text-sm text-slate-600">{t("caseFile.timeline.lead")}</p>
      <ol aria-labelledby="timeline-heading" className="mt-3 flex flex-col">
        {entries.map((entry) => (
          <li key={entry.step} data-step={entry.step} data-status={entry.status} className="flex gap-3 pb-3 last:pb-0">
            <span aria-hidden="true" className={`mt-1 h-3 w-3 shrink-0 rounded-full border-2 ${STATUS_MARKER[entry.status]}`} />
            <div className="min-w-0 text-sm">
              <div className={entry.status === "done" ? "font-medium" : "text-slate-600"}>
                {t(`caseFile.timeline.steps.${entry.step}`)}
              </div>
              <div className="text-xs text-slate-600">
                <span className="font-semibold">{t(`caseFile.timeline.status.${entry.status}`)}</span>
                {entry.status === "done" && entry.at ? <> · {formatDate(entry.at)}</> : null}
                {entry.status === "done" && entry.on ? <> · {formatDay(entry.on)}</> : null}
                {" · "}
                {t(`caseFile.actors.${entry.actor}`)}
              </div>
              {entry.status === "unavailable" && entry.reason ? (
                <div className="text-xs text-slate-500">{t(`caseFile.reasons.${entry.reason}`)}</div>
              ) : null}
              {entry.reappearedAt ? (
                <div className="text-xs text-amber-900">
                  {t("caseFile.timeline.reappeared", { date: formatDate(entry.reappearedAt) })}
                </div>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function EvidenceLine({ line }: { line: Evidence }) {
  const { t } = useTranslation();
  const formatDay = useFormatDay();
  switch (line.kind) {
    case "fingerprint_match":
      return <>{t("caseFile.why.fingerprintMatch")}</>;
    case "no_fingerprint_match":
      return <>{t("caseFile.why.noMatch")}</>;
    case "confirmed":
      return <>{t("caseFile.why.confirmed", { date: formatDay(line.on) })}</>;
  }
}

function WhySection({ evidence }: { evidence: Evidence[] }) {
  const { t } = useTranslation();
  return (
    <Card labelledBy="why-heading">
      <h2 id="why-heading" className="font-semibold">
        {t("caseFile.why.heading")}
      </h2>
      <ul className="mt-2 list-disc pl-5 text-sm text-slate-700">
        {evidence.map((line) => (
          <li key={line.kind}>
            <EvidenceLine line={line} />
          </li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-slate-500">{t("caseFile.why.noImagery")}</p>
    </Card>
  );
}

/**
 * Hosting (ADR 0027): who serves the page and its previews. What matters to a customer comes first
 * — provider, country and the suggested notice channel, the network in front, the abuse contact
 * with where it came from, the registrar, and the provider's rounded response history. Addresses and the
 * AS number sit in a collapsed "technical details". A missing fact reads "not known yet"; a host
 * nobody has looked up says exactly that and shows no empty rows.
 *
 * Everything is text. The RDAP source record is shown as an address, not a link: the customer UI
 * links out only to the pirate page itself, and only from "where it is".
 */
export function HostingSection({ finding }: { finding: Pick<CustomerFinding, "hosting"> }) {
  const { t } = useTranslation();
  const hosts = hostsOf(finding);
  const [page, ...content] = hosts;
  return (
    <Card labelledBy="hosting-heading">
      <h2 id="hosting-heading" className="font-semibold">
        {t("caseFile.hosting.heading")}
      </h2>
      <p className="text-sm text-slate-600">{t("caseFile.hosting.lead")}</p>
      {page ? (
        <HostBlock host={page.host} title={t("caseFile.hosting.pageHost", { hostname: page.host.hostname })} />
      ) : null}
      {content.length > 0 ? (
        <div className="mt-4">
          <h3 className="text-sm font-semibold text-slate-700">{t("caseFile.hosting.contentHosts")}</h3>
          {content.map(({ host }) => (
            <HostBlock
              key={host.hostname}
              host={host}
              title={t("caseFile.hosting.contentHost", { hostname: host.hostname })}
            />
          ))}
        </div>
      ) : null}
    </Card>
  );
}

function NotKnown() {
  const { t } = useTranslation();
  return <span className="text-slate-500">{t("caseFile.hosting.notKnown")}</span>;
}

function HostBlock({ host, title }: { host: Host; title: string }) {
  const { t } = useTranslation();
  const [language] = useLanguage();
  const formatDate = useFormatDate();
  const lookupNote = host.lookup === "complete" ? null : t(`caseFile.hosting.lookup.${host.lookup}`);

  return (
    <section aria-label={host.hostname} data-host={host.hostname} className="mt-3 rounded border border-slate-200 p-3">
      <h4 className="text-sm font-semibold break-all">{title}</h4>
      {lookupNote ? <p className="mt-1 text-xs text-slate-600">{lookupNote}</p> : null}
      {host.stale && host.looked_up_at ? (
        <p className="mt-1 text-xs text-amber-900">
          {t("caseFile.hosting.stale", { date: formatDate(host.looked_up_at) })}
        </p>
      ) : null}
      {host.lookup === "not_looked_up" ? null : (
        <>
          <dl className="mt-2 divide-y divide-slate-100">
            <Row label={t("caseFile.hosting.facts.provider")}>{host.provider ?? <NotKnown />}</Row>
            <Row label={t("caseFile.hosting.facts.location")}>
              {host.country ? <span className="block">{countryName(host.country, language)}</span> : null}
              <span className="block text-slate-600">{t(`caseFile.hosting.jurisdiction.${host.jurisdiction}`)}</span>
            </Row>
            <Row label={t("caseFile.hosting.facts.cdn")}>
              {host.cdn ? t("caseFile.hosting.cdn", { cdn: host.cdn }) : t("caseFile.hosting.noCdn")}
            </Row>
            <Row label={t("caseFile.hosting.facts.abuse_contact")}>
              {host.abuse_contact ? (
                <>
                  <span className="block break-all">{host.abuse_contact.email}</span>
                  <span className="block text-xs text-slate-600">
                    {t(`caseFile.hosting.abuseSource.${host.abuse_contact.source}`)}
                  </span>
                  <span className="block font-mono text-xs break-all text-slate-500">
                    {t("caseFile.hosting.record", { url: host.abuse_contact.source_url })}
                  </span>
                </>
              ) : (
                <NotKnown />
              )}
            </Row>
            <Row label={t("caseFile.hosting.facts.registrar")}>
              {host.registrar ? (
                <>
                  <span className="block">{host.registrar.name}</span>
                  {host.registrar.abuse_email ? (
                    <span className="block text-xs break-all text-slate-600">
                      {t("caseFile.hosting.registrarAbuse", { email: host.registrar.abuse_email })}
                    </span>
                  ) : null}
                  <span className="block font-mono text-xs break-all text-slate-500">
                    {t("caseFile.hosting.record", { url: host.registrar.source_url })}
                  </span>
                </>
              ) : (
                <NotKnown />
              )}
            </Row>
            <Row label={t("caseFile.hosting.facts.response_history")}>
              <ResponseHistoryLine history={host.response_history} />
            </Row>
          </dl>
          <details className="mt-2 text-xs text-slate-600">
            <summary className="cursor-pointer">{t("caseFile.hosting.technical.summary")}</summary>
            <dl className="mt-1">
              <Row label={t("caseFile.hosting.technical.addresses")}>
                <span className="font-mono break-all">
                  {host.addresses.length > 0 ? host.addresses.join(", ") : t("caseFile.hosting.technical.none")}
                </span>
              </Row>
              <Row label={t("caseFile.hosting.technical.network")}>
                {host.asn !== null || host.as_name ? (
                  <span className="break-all">
                    {[host.asn !== null ? `AS${host.asn}` : null, host.as_name].filter(Boolean).join(" · ")}
                  </span>
                ) : (
                  <NotKnown />
                )}
              </Row>
              {host.looked_up_at ? (
                <Row label={t("caseFile.hosting.technical.lookedUp")}>{formatDate(host.looked_up_at)}</Row>
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
export function ResponseHistoryLine({ history }: { history: ResponseHistory | null }) {
  const { t } = useTranslation();
  if (history === null) {
    return <span data-history="none" className="text-slate-500">{t("caseFile.hosting.history.none")}</span>;
  }
  const time = history.usual_removal_time;
  return (
    <span data-history="shown" className="flex flex-col gap-0.5">
      <span>
        {t("caseFile.hosting.history.removed", { percent: history.removed_percent })}
        {time ? <> · {t(`caseFile.hosting.history.usually.${time}`)}</> : null}
      </span>
      <span className="text-xs text-slate-500">{t("caseFile.hosting.history.note")}</span>
    </span>
  );
}

function OptionsSection({
  notAvailable,
  originalId,
}: {
  notAvailable: readonly CaseUnknown[];
  originalId: string;
}) {
  const { t } = useTranslation();
  return (
    <Card labelledBy="options-heading">
      <h2 id="options-heading" className="font-semibold">
        {t("caseFile.options.heading")}
      </h2>
      <p className="text-sm text-slate-600">{t("caseFile.options.lead")}</p>
      <ul className="mt-3 flex flex-col gap-3">
        {caseOptions(notAvailable).map((option) => (
          <li key={option.option} data-option={option.option} className="rounded border border-slate-200 p-3">
            {/* Prepare notice draft is a real, offered capability now (issue #33): a link to the
                notice screen, not the generic disabled-with-a-reason affordance the other
                options still use while they remain unbuilt or blocked. */}
            {option.option === "prepare_notice" ? (
              <PrepareNoticeOption originalId={originalId} />
            ) : (
              <OptionBody option={option} />
            )}
            {option.option === "escalation" ? <EscalationLadder /> : null}
            {option.option === "reupload_watch" && option.reason ? (
              <p className="mt-1 text-xs text-slate-500">{t("caseFile.options.coverage")}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** The one option that is offered today: a link into the notice-draft screen for this record. */
function PrepareNoticeOption({ originalId }: { originalId: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <p className="text-sm text-slate-700">{t("caseFile.options.prepare_notice.body")}</p>
        <p className="mt-1 text-xs text-slate-500">{t("caseFile.options.delivery")}</p>
      </div>
      <Link
        to="/app/originals/$originalId/notices"
        params={{ originalId }}
        className="shrink-0 rounded border border-slate-900 bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800"
      >
        {t("caseFile.options.prepare_notice.label")}
      </Link>
    </div>
  );
}

/** A visible, disabled affordance that says why it cannot be used yet. */
function OptionBody({ option }: { option: CaseOption }) {
  const { t } = useTranslation();
  const whyId = `option-${option.option}-why`;
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <p className="text-sm text-slate-700">{t(`caseFile.options.${option.option}.body`)}</p>
        <p id={whyId} className="mt-1 text-xs font-medium text-slate-500">
          {option.reason ? t(`caseFile.reasons.${option.reason}`) : t("caseFile.options.notOffered")}
        </p>
      </div>
      <button
        type="button"
        disabled
        aria-describedby={whyId}
        className="shrink-0 cursor-not-allowed rounded border border-slate-300 bg-slate-50 px-3 py-1.5 text-sm font-medium text-slate-500"
      >
        {t(`caseFile.options.${option.option}.label`)}
      </button>
    </div>
  );
}

function EscalationLadder() {
  const { t } = useTranslation();
  return (
    <ol aria-label={t("caseFile.options.ladder.label")} className="mt-2 flex flex-wrap items-center gap-1 text-xs">
      {ESCALATION_LADDER.map((rung, index) => (
        <li key={rung} className="flex items-center gap-1">
          {index > 0 ? <span aria-hidden="true">→</span> : null}
          <span className="rounded border border-dashed border-slate-300 px-1.5 py-0.5 text-slate-600">
            {t(`caseFile.options.ladder.${rung}`)} · {t("caseFile.options.ladder.notStarted")}
          </span>
        </li>
      ))}
    </ol>
  );
}
