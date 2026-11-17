import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { queryKeys } from "../api/queries";
import { Badge, Button, Dialog, Flash, StatusBadge } from "../components/ui";
import { dateTime } from "../lib/format";
import {
  type DraftLanguage,
  LANGUAGE_LABELS,
  draftFor,
  evidenceLabel,
  fullText,
  mediaFetchLabel,
  reportedBody,
  toLocalInput,
} from "../lib/incidentReport";

type Incident = Schemas["IncidentItemOut"];
type ReportDraft = Schemas["ReportDraftOut"];

const LANGUAGES: DraftLanguage[] = ["de", "en"];

async function copyToClipboard(text: string): Promise<void> {
  if (!navigator.clipboard) throw new Error("Clipboard unavailable: select the text and copy it by hand.");
  await navigator.clipboard.writeText(text);
}

/**
 * Safety incidents (issue #66): one case per genuinely new finding, and the hotline report draft a
 * human copies into the hotline's web form. Text only, like the rest of the safety screen: URLs are
 * plain text, never links, and nothing here loads anything external or submits a report.
 */
export function SafetyIncidents() {
  const [open, setOpen] = useState<number | null>(null);
  const [marking, setMarking] = useState<Incident | null>(null);
  const [flash, setFlash] = useState<{ message: string; tone: "error" | "info" } | null>(null);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.safetyIncidents,
    queryFn: () =>
      unwrap(api.GET("/api/v1/review/safety/incidents", { params: { query: { status: "active" } } })),
  });
  const items = query.data?.items ?? [];

  return (
    <section className="flex flex-col gap-3" aria-labelledby="incidents-heading">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="incidents-heading" className="font-display text-display-m">
          Safety incidents
        </h2>
        <span className="text-body-s text-ink-muted">
          {items.length} open or reported{query.data?.truncated ? " (more in the admin)" : ""}
        </span>
      </div>
      <p className="rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-body-s text-caution-ink">
        Judge a case from its URL, category, term and timestamps only — never open the page. The report draft is copied
        by hand into the hotline&apos;s web form (docs/safety.md); nothing is sent from here. Afterwards, mark the case
        reported and enter the hotline&apos;s reference number, or leave it blank if none was issued.
      </p>
      <Flash message={flash?.message ?? null} tone={flash?.tone} onClose={() => setFlash(null)} />
      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-hairline-strong bg-surface-100 p-6 text-center text-ink-muted">
          No open or reported safety incidents.
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((incident) => (
            <li key={incident.id} className="rounded-sm border border-hairline bg-surface-100 p-3">
              <IncidentSummary incident={incident} />
              <div className="mt-2 flex flex-wrap gap-2">
                <Button
                  aria-expanded={open === incident.id}
                  onClick={() => setOpen((current) => (current === incident.id ? null : incident.id))}
                >
                  {open === incident.id ? "Hide report draft" : "Report draft"}
                </Button>
                {incident.status === "open" ? (
                  <Button tone="primary" onClick={() => setMarking(incident)}>
                    Mark reported…
                  </Button>
                ) : null}
              </div>
              {open === incident.id ? (
                <ReportDraftLoader incidentId={incident.id} onFlash={(message, tone) => setFlash({ message, tone })} />
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {marking ? (
        <MarkReportedDialog
          incident={marking}
          onClose={() => setMarking(null)}
          onDone={(updated) => {
            setMarking(null);
            setFlash({ message: `${updated.reference_id} marked reported.`, tone: "info" });
            void queryClient.invalidateQueries({ queryKey: queryKeys.safetyIncidents });
          }}
        />
      ) : null}
    </section>
  );
}

function IncidentSummary({ incident }: { incident: Incident }) {
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-body-s font-semibold">{incident.reference_id}</span>
        <StatusBadge status={incident.status} />
        {incident.categories.map((category) => (
          <Badge key={category} tone="caution">
            {category}
          </Badge>
        ))}
      </div>
      <div className="mt-1 text-caption text-ink-muted">
        {incident.sites.join(", ") || "—"} · {incident.url_count} URL{incident.url_count === 1 ? "" : "s"} · detected{" "}
        {dateTime(incident.detected_at)} · last seen {dateTime(incident.last_seen_at)}
      </div>
      {incident.terms.length ? (
        <div className="mt-1 flex flex-wrap gap-1 text-caption">
          {incident.terms.map((term) => (
            <span key={`${term.category}-${term.term}`} className="rounded-sm bg-critical-quiet px-1.5 py-0.5 text-critical-ink">
              “{term.term}” · {term.category}
            </span>
          ))}
        </div>
      ) : null}
      {incident.reported_at ? (
        <div className="mt-1 text-caption text-ink-muted">
          Reported {dateTime(incident.reported_at)} via {incident.reported_via || "—"} ·{" "}
          {incident.reference ? `reference ${incident.reference}` : "no reference number issued"}
        </div>
      ) : null}
    </div>
  );
}

function ReportDraftLoader({
  incidentId,
  onFlash,
}: {
  incidentId: number;
  onFlash: (message: string, tone: "error" | "info") => void;
}) {
  const query = useQuery({
    queryKey: queryKeys.safetyReportDraft(incidentId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/review/safety/incidents/{incident_id}/report-draft", {
          params: { path: { incident_id: incidentId } },
        }),
      ),
    gcTime: 0,
  });
  if (query.isPending) return <p className="mt-2 text-body-s text-ink-subtle">Loading draft…</p>;
  if (query.isError) return <p className="mt-2 text-body-s text-critical-ink">Could not load: {errorMessage(query.error)}</p>;
  return (
    <ReportDraftView
      draft={query.data}
      onCopy={(text) =>
        copyToClipboard(text).then(
          () => onFlash("Copied to the clipboard.", "info"),
          (error: unknown) => onFlash(errorMessage(error), "error"),
        )
      }
    />
  );
}

/** The draft as plain text with copy buttons. Presentational: no fetching, no links, no images. */
export function ReportDraftView({ draft, onCopy }: { draft: ReportDraft; onCopy: (text: string) => unknown }) {
  const [language, setLanguage] = useState<DraftLanguage>("de");
  const ids = useId();
  const text = draftFor(draft.drafts, language);
  return (
    <div className="mt-3 flex flex-col gap-3 rounded-none border border-hairline bg-surface-inset p-3 text-body-s">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">Report draft</span>
        <span className="text-caption text-ink-muted">
          template {draft.template_version} · channel: {draft.channel}
        </span>
        <div role="group" aria-label="Draft language" className="ml-auto flex gap-1">
          {LANGUAGES.map((code) => (
            <Button key={code} aria-pressed={language === code} tone={language === code ? "primary" : "neutral"} onClick={() => setLanguage(code)}>
              {LANGUAGE_LABELS[code]}
            </Button>
          ))}
        </div>
      </div>
      {draft.supplement ? (
        <p className="rounded-sm border border-caution-ink bg-caution-quiet px-2 py-1 text-caution-ink">
          Already reported {dateTime(draft.reported_at)}
          {draft.reference ? ` (reference ${draft.reference})` : " (no reference number issued)"}. This draft is a
          supplement — file it only for URLs marked new since the report.
        </p>
      ) : null}
      <p className="text-ink-muted">{mediaFetchLabel(draft.media_fetch)}</p>

      <div>
        <div className="mb-1 font-medium">URLs for the form (plain text, do not open)</div>
        <ul className="flex flex-col gap-1">
          {draft.urls.map((item) => (
            <li key={item.url} className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-caption break-all">{item.url}</span>
              <span className="text-caption text-ink-muted">
                {item.site} · {evidenceLabel(item.evidence)}
                {item.new_since_report ? " · new since report" : ""}
              </span>
              <Button className="ml-auto" aria-label={`Copy URL ${item.url}`} onClick={() => onCopy(item.url)}>
                Copy
              </Button>
            </li>
          ))}
        </ul>
      </div>

      {text ? (
        <>
          <div>
            <div className="mb-1 flex items-center justify-between">
              <label htmlFor={`${ids}-subject`} className="font-medium">
                Subject
              </label>
              <Button onClick={() => onCopy(text.subject)}>Copy subject</Button>
            </div>
            <input
              id={`${ids}-subject`}
              readOnly
              value={text.subject}
              className="w-full rounded-none border border-border-control bg-surface-100 px-2 py-1 font-mono text-mono-s"
            />
          </div>
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <label htmlFor={`${ids}-body`} className="font-medium">
                Text
              </label>
              <span className="flex gap-2">
                <Button onClick={() => onCopy(text.body)}>Copy text</Button>
                <Button onClick={() => onCopy(fullText(text))}>Copy subject + text</Button>
              </span>
            </div>
            <textarea
              id={`${ids}-body`}
              readOnly
              value={text.body}
              lang={language}
              rows={18}
              className="w-full rounded-none border border-border-control bg-surface-100 px-2 py-1 font-mono text-mono-s"
            />
          </div>
        </>
      ) : null}
    </div>
  );
}

function MarkReportedDialog({
  incident,
  onClose,
  onDone,
}: {
  incident: Incident;
  onClose: () => void;
  onDone: (updated: Incident) => void;
}) {
  const [reference, setReference] = useState("");
  const [via, setVia] = useState("");
  const [reportCategory, setReportCategory] = useState("");
  const [reportedAt, setReportedAt] = useState(() => toLocalInput(new Date()));
  const [language, setLanguage] = useState<DraftLanguage>("de");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = () => {
    const body = reportedBody({ reference, via, reportCategory, reportedAt, language });
    if (!body) {
      setProblem("Enter a valid report date.");
      return;
    }
    setBusy(true);
    unwrap(
      api.POST("/api/v1/review/safety/incidents/{incident_id}/reported", {
        params: { path: { incident_id: incident.id } },
        body,
      }),
    ).then(onDone, (error: unknown) => {
      setBusy(false);
      setProblem(errorMessage(error));
    });
  };

  const field = "w-full rounded-sm border border-hairline-strong px-3 py-1.5";
  return (
    <Dialog title={`Mark ${incident.reference_id} reported`} onClose={onClose}>
      <div className="flex flex-col gap-3 text-body-s">
        <label className="block">
          <span className="mb-1 block text-ink-muted">Hotline reference number (blank = none was issued)</span>
          <input className={field} value={reference} onChange={(event) => setReference(event.target.value)} />
        </label>
        <label className="block">
          <span className="mb-1 block text-ink-muted">Reported via (blank = the channel named in the draft)</span>
          <input className={field} value={via} onChange={(event) => setVia(event.target.value)} />
        </label>
        <label className="block">
          <span className="mb-1 block text-ink-muted">Category chosen in the form (verbatim, optional)</span>
          <input className={field} value={reportCategory} onChange={(event) => setReportCategory(event.target.value)} />
        </label>
        <label className="block">
          <span className="mb-1 block text-ink-muted">Reported at (your local time)</span>
          <input
            type="datetime-local"
            className={field}
            value={reportedAt}
            onChange={(event) => setReportedAt(event.target.value)}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-ink-muted">Draft language used</span>
          <select
            className={field}
            value={language}
            onChange={(event) => setLanguage(event.target.value === "en" ? "en" : "de")}
          >
            {LANGUAGES.map((code) => (
              <option key={code} value={code}>
                {LANGUAGE_LABELS[code]}
              </option>
            ))}
          </select>
        </label>
      </div>
      {problem ? (
        <p role="alert" className="mt-3 text-body-s text-critical-ink">
          {problem}
        </p>
      ) : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="primary" onClick={submit} disabled={busy}>
          Mark reported
        </Button>
      </div>
    </Dialog>
  );
}
