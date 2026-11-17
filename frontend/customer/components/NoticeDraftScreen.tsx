"use client";
/**
 * "Prepare notice draft" (`/app/originals/{orig_…}/notices`, issue #33, ADR 0035; ported to the
 * Next.js app, ADR 0036).
 *
 * Marceline detects and documents content matches and prepares a draft from facts it holds plus
 * the customer's own confirmations. It does not decide whether a use infringes, does not choose a
 * recipient for the customer, and never sends anything to a real recipient — the customer reviews,
 * confirms and sends this draft themselves, from their own mailbox or provider account.
 *
 * A client component from top to bottom: selecting pages, picking a recipient, ticking
 * confirmations and copying the text are all interaction. The page above it reads the candidates
 * and the drafts on the server and hands them over, together with the `notices` and `common`
 * slices of the request's words — so the resource files still never ship as JavaScript.
 *
 * Every write goes to a named BFF route under `/app/api/notices/…` and is followed by
 * `router.refresh()`: the server's own read is the only authority on what a draft now looks like.
 */
import { type FormEvent, useState } from "react";

import type {
  Notice,
  NoticeCandidateFinding,
  NoticeCandidates,
  NoticeProvider,
} from "@shared/app/apiTypes";
import {
  CORRECTION_REASONS,
  EXPORT_FORMATS,
  type ChannelKind,
  type ConfirmationKind,
  type CorrectionReason,
  type ExportFormat,
  type NoticeLanguage,
  type NoticeVariant,
  WITHDRAWAL_REASONS,
  type WithdrawalReason,
  blockerLine,
  canExport,
  declarationLanguage,
  errorKey,
  isOpen,
  isPendingWrite,
  isSelectable,
  isUsableChannel,
  mailtoLink,
  onlyNeedsExceptionTick,
  variantForChannelKind,
} from "@shared/app/notices";

import { NOTICE_TONES } from "../lib/tones";
import { type Resource, type Translate, translator } from "../lib/translate";
import { type WriteState, useWrite } from "../lib/write";
import { BADGE, TitleRow, formatDate } from "./parts";

const INPUT =
  "mt-1 block w-full rounded-sm border border-border-control bg-surface-100 px-2 py-1.5 text-body-s text-ink";
const LABEL = "block text-label text-ink";
const SECTION = "rounded-md border border-hairline bg-surface-100 p-4";
const BUTTON =
  "inline-flex items-center gap-2 rounded-sm border px-3 py-1.5 text-button transition-colors duration-120 ease-standard disabled:cursor-not-allowed disabled:opacity-45";
const NEUTRAL = `${BUTTON} border-border-control bg-surface-100 text-ink hover:bg-surface-300`;
const PRIMARY = `${BUTTON} border-brand bg-brand text-on-brand hover:bg-brand-hover`;
// Withdrawing is the red act on this screen, but an outline rather than a fill: the filled red
// belongs to the one action a view is for, and here that is approving a draft.
const REJECT = `${BUTTON} border-brand bg-surface-100 text-brand-ink hover:bg-brand-quiet`;

interface Ctx {
  t: Translate;
  language: string;
  originalId: string;
}

export function NoticeDraftScreen({
  originalId,
  language,
  words,
  candidates,
  notices,
  organisationName,
}: {
  originalId: string;
  language: string;
  words: Resource;
  candidates: NoticeCandidates;
  /** This record's drafts, newest first, as the server read them. */
  notices: Notice[];
  organisationName: string;
}) {
  const t = translator(words, language);
  const ctx: Ctx = { t, language, originalId };
  return (
    <section aria-labelledby="notices-heading" className="flex flex-col gap-4">
      <div>
        <a
          href={`/app/originals/${originalId}`}
          className="rounded-sm text-body-s text-brand-ink underline"
        >
          {t("original.back")}
        </a>
        <h1 id="notices-heading" className="mt-2 font-display text-display-m break-words text-ink">
          {t("notices.heading", { title: candidates.title })}
        </h1>
        <p className="mt-2 max-w-[68ch] text-body text-ink-muted">{t("notices.lead")}</p>
        <p className="mt-1 max-w-[68ch] text-caption text-ink-subtle">{t("notices.disclaimer")}</p>
      </div>
      <CreateSection ctx={ctx} candidates={candidates} organisationName={organisationName} />
      {notices.length > 0 ? (
        <div className="flex flex-col gap-4">
          <h2 className="font-display text-display-s text-ink">{t("notices.existing.heading")}</h2>
          {notices.map((notice) => (
            <NoticeCard key={notice.id} ctx={ctx} notice={notice} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function WriteError({ t, write }: { t: Translate; write: WriteState }) {
  if (!write.error) return null;
  return (
    <p role="alert" className="text-body-s text-critical-ink">
      {t(errorKey(write.error))}
    </p>
  );
}

/**
 * Production accepted this step but has not applied it yet (202, ADR 0046): the public host holds
 * no findings, no gate and no draft, so it takes the write down and development applies it. The
 * line is factual — it never says the step happened, because it may still be refused.
 */
function WritePending({ t, write }: { t: Translate; write: WriteState }) {
  if (!isPendingWrite(write.data)) return null;
  return (
    <p role="status" className="text-body-s text-ink-muted">
      {t("notices.pending")}
    </p>
  );
}

function CreateSection({
  ctx,
  candidates,
  organisationName,
}: {
  ctx: Ctx;
  candidates: NoticeCandidates;
  organisationName: string;
}) {
  const { t, originalId } = ctx;
  const create = useWrite();

  const [selectedHost, setSelectedHost] = useState<string | null>(null);
  const [selectedPages, setSelectedPages] = useState<Set<string>>(new Set());
  const [exceptionTicked, setExceptionTicked] = useState<Set<string>>(new Set());
  const [channelKind, setChannelKind] = useState<ChannelKind | "">("");
  const [language, setLanguage] = useState<NoticeLanguage>(declarationLanguage(ctx.language));
  const [claimantName, setClaimantName] = useState(organisationName);
  const [claimantEmail, setClaimantEmail] = useState("");
  const [claimantAddress, setClaimantAddress] = useState("");
  const [claimantPhone, setClaimantPhone] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");

  const selectable = candidates.findings.filter((finding) =>
    isSelectable(finding, exceptionTicked),
  );
  const hosts = [...new Set(selectable.map((finding) => finding.site))];
  const hostFindings = selectedHost
    ? selectable.filter((finding) => finding.site === selectedHost)
    : [];
  const providers: NoticeProvider[] = selectedHost ? (hostFindings[0]?.providers ?? []) : [];
  const usableChannels = providers.flatMap((role) =>
    role.channels.filter(isUsableChannel).map((channel) => ({ role, channel })),
  );

  function toggle(finding: NoticeCandidateFinding) {
    if (!isSelectable(finding, exceptionTicked)) return;
    if (
      selectedHost !== null &&
      selectedHost !== finding.site &&
      !selectedPages.has(finding.page_id)
    ) {
      return;
    }
    const next = new Set(selectedPages);
    if (next.has(finding.page_id)) {
      next.delete(finding.page_id);
      setSelectedPages(next);
      if (next.size === 0) setSelectedHost(null);
    } else {
      next.add(finding.page_id);
      setSelectedPages(next);
      setSelectedHost(finding.site);
    }
  }

  function toggleException(pageId: string) {
    const next = new Set(exceptionTicked);
    if (next.has(pageId)) next.delete(pageId);
    else next.add(pageId);
    setExceptionTicked(next);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const picked = usableChannels.find((item) => item.channel.kind === channelKind);
    if (!picked || selectedPages.size === 0 || !claimantEmail.trim() || !claimantName.trim()) return;
    void create
      .run("/app/api/notices", "POST", {
        original_id: originalId,
        page_ids: [...selectedPages],
        exception_confirmed_page_ids: [...selectedPages].filter((pageId) =>
          exceptionTicked.has(pageId),
        ),
        recipient: {
          hostname: picked.role.hostname,
          provider_name: picked.role.provider_name,
          channel_kind: picked.channel.kind,
          email: picked.channel.email ?? "",
          url: picked.channel.url ?? "",
          // The channel id is all the server needs: it reads that channel's forwarding and
          // transparency practice from the directory itself, so the client cannot state it.
          complaint_channel_id: picked.channel.channel_id,
        },
        variant: variantForChannelKind(picked.channel.kind) as NoticeVariant,
        language,
        claimant: {
          name: claimantName.trim(),
          email: claimantEmail.trim(),
          address: claimantAddress.trim(),
          phone: claimantPhone.trim(),
        },
        legitimate_source_url: sourceUrl.trim(),
      })
      .then((ok) => {
        if (ok) {
          setSelectedPages(new Set());
          setSelectedHost(null);
          setChannelKind("");
        }
      });
  }

  return (
    <section aria-labelledby="notices-create" className={SECTION}>
      <h2 id="notices-create" className="font-display text-display-s text-ink">
        {t("notices.create.heading")}
      </h2>
      {candidates.findings.length === 0 ? (
        <p className="mt-2 text-body-s text-ink-muted">{t("notices.create.none")}</p>
      ) : (
        <>
          <p className="mt-1 text-body-s text-ink-muted">{t("notices.create.lead")}</p>
          <ul className="mt-3 flex flex-col gap-2">
            {candidates.findings.map((finding) => {
              const needsExceptionTick = onlyNeedsExceptionTick(finding);
              const canSelect = isSelectable(finding, exceptionTicked);
              return (
                <li
                  key={finding.page_id}
                  className="rounded-none border border-hairline bg-surface-inset p-2 text-body-s"
                >
                  <label className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={selectedPages.has(finding.page_id)}
                      disabled={
                        !canSelect ||
                        (selectedHost !== null &&
                          selectedHost !== finding.site &&
                          !selectedPages.has(finding.page_id))
                      }
                      onChange={() => toggle(finding)}
                    />
                    <span className="min-w-0">
                      <span className="block font-mono text-mono break-words text-ink">
                        {finding.site}
                      </span>
                      <span className="block font-mono text-mono-s break-all text-ink-subtle">
                        {finding.url}
                      </span>
                      {needsExceptionTick ? (
                        <label className="mt-1 flex items-start gap-2 text-caption text-ink">
                          <input
                            type="checkbox"
                            className="mt-0.5"
                            checked={exceptionTicked.has(finding.page_id)}
                            onChange={() => toggleException(finding.page_id)}
                          />
                          <span>{t("notices.create.exceptionTick")}</span>
                        </label>
                      ) : null}
                      {!canSelect && !needsExceptionTick ? (
                        <span className="mt-1 block text-caption text-caution-ink">
                          {t("notices.create.blocked")}{" "}
                          {blockerLine(finding.blockers, (code) => t(`notices.blockers.${code}`))}
                        </span>
                      ) : null}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>

          {hosts.length > 1 && selectedHost === null ? (
            <p className="mt-2 text-caption text-ink-subtle">{t("notices.create.hostHint")}</p>
          ) : null}

          {selectedHost !== null ? (
            <form className="mt-4 flex flex-col gap-3" onSubmit={submit}>
              <div>
                <span className={LABEL}>{t("notices.create.recipient")}</span>
                <div className="mt-1 flex flex-col gap-1">
                  {usableChannels.length === 0 ? (
                    <p className="text-caption text-caution-ink">
                      {t("notices.blockers.no_supported_channel")}
                    </p>
                  ) : (
                    usableChannels.map(({ role, channel }) => (
                      <label
                        key={`${role.slot}-${channel.kind}`}
                        className="flex items-center gap-2 text-body-s text-ink"
                      >
                        <input
                          type="radio"
                          name="channel"
                          checked={channelKind === channel.kind}
                          onChange={() => setChannelKind(channel.kind)}
                        />
                        <span>
                          {role.provider_name || role.hostname} —{" "}
                          {t(`notices.channelKinds.${channel.kind}`)}
                          {role.suggested_channel_kind === channel.kind
                            ? ` (${t("notices.create.suggested")})`
                            : ""}
                        </span>
                      </label>
                    ))
                  )}
                </div>
              </div>

              <label className={LABEL}>
                {t("notices.create.language")}
                <select
                  className={INPUT}
                  value={language}
                  onChange={(event) => setLanguage(event.target.value as NoticeLanguage)}
                >
                  <option value="en">{t("notices.languages.en")}</option>
                  <option value="de">{t("notices.languages.de")}</option>
                </select>
              </label>

              <label className={LABEL}>
                {t("notices.create.claimantName")}
                <input
                  className={INPUT}
                  value={claimantName}
                  onChange={(event) => setClaimantName(event.target.value)}
                  required
                />
              </label>
              <label className={LABEL}>
                {t("notices.create.claimantEmail")}
                <input
                  type="email"
                  className={INPUT}
                  value={claimantEmail}
                  onChange={(event) => setClaimantEmail(event.target.value)}
                  required
                />
              </label>
              <label className={LABEL}>
                {t("notices.create.claimantAddress")}
                <input
                  className={INPUT}
                  value={claimantAddress}
                  onChange={(event) => setClaimantAddress(event.target.value)}
                />
              </label>
              <label className={LABEL}>
                {t("notices.create.claimantPhone")}
                <input
                  className={INPUT}
                  value={claimantPhone}
                  onChange={(event) => setClaimantPhone(event.target.value)}
                />
              </label>
              <label className={LABEL}>
                {t("notices.create.sourceUrl")}
                <input
                  className={INPUT}
                  value={sourceUrl}
                  onChange={(event) => setSourceUrl(event.target.value)}
                />
              </label>

              <button
                type="submit"
                className={PRIMARY}
                disabled={
                  !channelKind || create.pending || !claimantEmail.trim() || !claimantName.trim()
                }
              >
                {t("notices.create.submit")}
              </button>
              <WriteError t={t} write={create} />
              <WritePending t={t} write={create} />
            </form>
          ) : null}
        </>
      )}
    </section>
  );
}

function NoticeCard({ ctx, notice }: { ctx: Ctx; notice: Notice }) {
  const { t, language } = ctx;
  return (
    <article aria-label={notice.work_title} className={SECTION} data-notice-status={notice.status}>
      <TitleRow
        title={
          <div>
            <div className="text-body-l font-medium text-ink">
              {notice.recipient_provider_name || notice.recipient_hostname}
            </div>
            <div className="text-caption text-ink-subtle">
              {t(`notices.variants.${notice.variant}`)} · {formatDate(notice.created_at, language)}
              {notice.is_sandbox ? ` · ${t("notices.sandboxLabel")}` : ""}
            </div>
          </div>
        }
        badge={
          <span data-status={notice.status} className={`${BADGE} ${NOTICE_TONES[notice.status]}`}>
            {t(`notices.statuses.${notice.status}`)}
          </span>
        }
      />

      {notice.status === "draft" ? <ApproveForm ctx={ctx} notice={notice} /> : null}
      {notice.status === "customer_approved" ? <ApprovedView ctx={ctx} notice={notice} /> : null}
      {notice.status === "marked_sent_by_customer" ? (
        <ResponseForm ctx={ctx} notice={notice} />
      ) : null}
      {notice.status === "provider_responded" && notice.outcome ? (
        <p className="mt-2 text-body-s text-ink">{t(`notices.outcomes.${notice.outcome}`)}</p>
      ) : null}
      {notice.removal_observed_at ? (
        <p className="mt-2 text-body-s text-ink-muted">
          {t("notices.removalObserved", { date: formatDate(notice.removal_observed_at, language) })}
        </p>
      ) : null}
      {notice.status === "withdrawn" && notice.withdrawal_reason ? (
        <p className="mt-2 text-body-s text-ink">
          {t(`notices.withdrawalReasons.${notice.withdrawal_reason}`)}
        </p>
      ) : null}
      {notice.status === "corrected" && notice.corrected_into ? (
        <p className="mt-2 text-body-s text-ink">{t("notices.correctedInto")}</p>
      ) : null}

      {isOpen(notice.status) ? <WithdrawAndCorrect ctx={ctx} notice={notice} /> : null}
    </article>
  );
}

function ApproveForm({ ctx, notice }: { ctx: Ctx; notice: Notice }) {
  const { t } = ctx;
  const approve = useWrite();
  const [checked, setChecked] = useState<Set<ConfirmationKind>>(new Set());
  const allChecked = notice.required_confirmations.every((item) => checked.has(item.kind));

  function toggle(kind: ConfirmationKind) {
    const next = new Set(checked);
    if (next.has(kind)) next.delete(kind);
    else next.add(kind);
    setChecked(next);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!allChecked) return;
    void approve.run(
      `/app/api/notices/${notice.id}/approve`,
      "POST",
      notice.required_confirmations.map((item) => ({
        kind: item.kind,
        version: item.version,
        language: notice.language,
        confirmed: true,
      })),
    );
  }

  return (
    <form className="mt-3 flex flex-col gap-3" onSubmit={submit}>
      <div>
        <h3 className="text-label text-ink">{t("notices.preview.heading")}</h3>
        <pre className="mt-1 max-h-64 overflow-auto rounded-none border border-hairline bg-surface-inset p-3 font-mono text-mono-s whitespace-pre-wrap text-ink">
          {notice.preview_text}
        </pre>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="text-label text-ink">
          {t("notices.confirmations.heading")}
        </legend>
        {notice.required_confirmations.map((item) => (
          <label key={item.kind} className="flex items-start gap-2 text-body-s text-ink">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={checked.has(item.kind)}
              onChange={() => toggle(item.kind)}
            />
            <span>{notice.language === "de" ? item.de : item.en}</span>
          </label>
        ))}
      </fieldset>
      <button type="submit" className={PRIMARY} disabled={!allChecked || approve.pending}>
        {t("notices.confirmations.approve")}
      </button>
      <WriteError t={t} write={approve} />
      <WritePending t={t} write={approve} />
    </form>
  );
}

/**
 * Where this app points a download link: its own BFF, never Django directly (ADR 0036) —
 * `app/api/notices/[noticeId]/export/[format]/route.ts` relays it, exactly as the invoice PDF is
 * relayed, so the browser never needs an address on the private side.
 */
function exportPath(id: string, format: ExportFormat): string {
  return `/app/api/notices/${encodeURIComponent(id)}/export/${format}`;
}

const EXPORT_LABELS: Record<ExportFormat, string> = {
  txt: "notices.approved.downloadText",
  eml: "notices.approved.downloadEml",
  annex: "notices.approved.downloadAnnex",
};

function DownloadLinks({ ctx, notice }: { ctx: Ctx; notice: Notice }) {
  const { t } = ctx;
  return (
    <>
      {EXPORT_FORMATS.map((format) => (
        <a
          key={format}
          href={exportPath(notice.id, format)}
          download
          className="rounded-sm text-body-s text-brand-ink underline"
        >
          {t(EXPORT_LABELS[format])}
        </a>
      ))}
    </>
  );
}


function ApprovedView({ ctx, notice }: { ctx: Ctx; notice: Notice }) {
  const { t } = ctx;
  const markSent = useWrite();
  const sandboxSend = useWrite();
  const [copied, setCopied] = useState(false);
  const [sandboxAddress, setSandboxAddress] = useState("");
  const link = mailtoLink(notice);

  async function copy() {
    try {
      await navigator.clipboard.writeText(notice.approved_text ?? "");
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="mt-3 flex flex-col gap-3">
      <pre className="max-h-64 overflow-auto rounded-none border border-hairline bg-surface-inset p-3 font-mono text-mono-s whitespace-pre-wrap text-ink">
        {notice.approved_text}
      </pre>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={NEUTRAL} onClick={() => void copy()}>
          {copied ? t("notices.approved.copied") : t("notices.approved.copy")}
        </button>
        {link ? (
          <a href={link} className="rounded-sm text-body-s text-brand-ink underline">
            {t("notices.approved.mailto")}
          </a>
        ) : null}
        {canExport(notice) ? <DownloadLinks ctx={ctx} notice={notice} /> : null}
        {!notice.is_sandbox ? (
          <button
            type="button"
            className={PRIMARY}
            disabled={markSent.pending}
            onClick={() => void markSent.run(`/app/api/notices/${notice.id}/mark-sent`, "POST")}
          >
            {t("notices.approved.markSent")}
          </button>
        ) : null}
      </div>
      {canExport(notice) ? (
        <p className="text-body-s text-ink-muted">{t("notices.approved.downloadHint")}</p>
      ) : null}
      <WriteError t={t} write={markSent} />
      <WritePending t={t} write={markSent} />

      {notice.is_sandbox ? (
        <div className="rounded-md border border-dashed border-caution-ink/40 bg-caution-quiet p-3 text-body-s">
          <p className="text-caution-ink">{t("notices.sandbox.explain")}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              type="email"
              className={`${INPUT} w-auto`}
              placeholder={t("notices.sandbox.addressPlaceholder")}
              value={sandboxAddress}
              onChange={(event) => setSandboxAddress(event.target.value)}
            />
            <button
              type="button"
              className={NEUTRAL}
              disabled={!sandboxAddress.trim() || sandboxSend.pending}
              onClick={() =>
                void sandboxSend.run(`/app/api/notices/sandbox-send/${notice.id}`, "POST", {
                  recipient_email: sandboxAddress.trim(),
                })
              }
            >
              {t("notices.sandbox.send")}
            </button>
          </div>
          <WriteError t={t} write={sandboxSend} />
        </div>
      ) : null}
    </div>
  );
}

function ResponseForm({ ctx, notice }: { ctx: Ctx; notice: Notice }) {
  const { t } = ctx;
  const record = useWrite();
  const [outcome, setOutcome] = useState<"removed" | "refused" | "no_response">("removed");

  function submit(event: FormEvent) {
    event.preventDefault();
    void record.run(`/app/api/notices/${notice.id}/response`, "POST", { outcome });
  }

  return (
    <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={submit}>
      <label className={LABEL}>
        {t("notices.response.outcome")}
        <select
          className={INPUT}
          value={outcome}
          onChange={(event) => setOutcome(event.target.value as typeof outcome)}
        >
          <option value="removed">{t("notices.outcomes.removed")}</option>
          <option value="refused">{t("notices.outcomes.refused")}</option>
          <option value="no_response">{t("notices.outcomes.no_response")}</option>
        </select>
      </label>
      <button type="submit" className={PRIMARY} disabled={record.pending}>
        {t("notices.response.submit")}
      </button>
      <WriteError t={t} write={record} />
      <WritePending t={t} write={record} />
    </form>
  );
}

function WithdrawAndCorrect({ ctx, notice }: { ctx: Ctx; notice: Notice }) {
  const { t } = ctx;
  const withdraw = useWrite();
  const correct = useWrite();
  const [open, setOpen] = useState<"withdraw" | "correct" | null>(null);
  const [reason, setReason] = useState<WithdrawalReason | CorrectionReason | "">("");
  const [note, setNote] = useState("");

  if (open === null) {
    return (
      <div className="mt-3 flex gap-3 text-caption">
        <button
          type="button"
          className="rounded-sm text-ink-subtle underline hover:text-ink"
          onClick={() => setOpen("withdraw")}
        >
          {t("notices.withdraw.open")}
        </button>
        <button
          type="button"
          className="rounded-sm text-ink-subtle underline hover:text-ink"
          onClick={() => setOpen("correct")}
        >
          {t("notices.correct.open")}
        </button>
      </div>
    );
  }

  const reasons = open === "withdraw" ? WITHDRAWAL_REASONS : CORRECTION_REASONS;
  const write = open === "withdraw" ? withdraw : correct;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!reason) return;
    void write
      .run(`/app/api/notices/${notice.id}/${open === "withdraw" ? "withdraw" : "correct"}`, "POST", {
        reason,
        note,
      })
      .then((ok) => {
        if (ok) setOpen(null);
      });
  }

  return (
    <form className="mt-3 flex flex-col gap-2 border-t border-hairline pt-3" onSubmit={submit}>
      <label className={LABEL}>
        {open === "withdraw" ? t("notices.withdraw.reason") : t("notices.correct.reason")}
        <select
          className={INPUT}
          value={reason}
          onChange={(event) => setReason(event.target.value as typeof reason)}
          required
        >
          <option value="">{t("notices.reasonChoose")}</option>
          {reasons.map((code) => (
            <option key={code} value={code}>
              {t(
                `notices.${open === "withdraw" ? "withdrawalReasons" : "correctionReasons"}.${code}`,
              )}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("notices.noteLabel")}
        <textarea
          className={INPUT}
          rows={2}
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <div className="flex gap-2">
        <button
          type="submit"
          className={open === "withdraw" ? REJECT : PRIMARY}
          disabled={!reason || write.pending}
        >
          {open === "withdraw" ? t("notices.withdraw.submit") : t("notices.correct.submit")}
        </button>
        <button type="button" className={NEUTRAL} onClick={() => setOpen(null)}>
          {t("notices.cancel")}
        </button>
      </div>
      <WriteError t={t} write={write} />
      <WritePending t={t} write={write} />
    </form>
  );
}
