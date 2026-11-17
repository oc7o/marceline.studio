/**
 * "Prepare notice draft" (`/app/originals/$originalId/notices`, issue #33, ADR 0035).
 *
 * Marceline detects and documents content matches and prepares a draft from facts it holds plus
 * the customer's own confirmations. It does not decide whether a use infringes, does not choose a
 * recipient for the customer, and never sends anything to a real recipient — the customer reviews,
 * confirms, and sends this draft themselves, from their own mailbox or provider account.
 */
import { useParams } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../components/ui";
import { useLanguage } from "../i18n";
import {
  type ChannelKind,
  type ConfirmationKind,
  type CorrectionReason,
  type NoticeLanguage,
  type NoticeVariant,
  type WithdrawalReason,
  CORRECTION_REASONS,
  EXPORT_FORMATS,
  WITHDRAWAL_REASONS,
  blockerLine,
  declarationLanguage,
  errorKey,
  isOpen,
  isPendingWrite,
  isSelectable,
  isUsableChannel,
  canExport,
  mailtoLink,
  noticeExportPath,
  onlyNeedsExceptionTick,
  variantForChannelKind,
} from "./notices";
import { NoticeSubmissionsBlock } from "./NoticeSubmissionsBlock";
import { parsePublicId } from "./caseFile";
import { BADGE, LoadError, Loading, NotFound, TitleRow, isNotFound, useFormatDate } from "./parts";
import {
  type Notice,
  type NoticeCandidateFinding,
  type NoticeCandidates,
  type NoticeProvider,
  useApproveNotice,
  useCorrectNotice,
  useCreateNotice,
  useCustomerAccount,
  useMarkNoticeSent,
  useNotice,
  useNoticeCandidates,
  useNotices,
  useRecordNoticeResponse,
  useSandboxSendNotice,
  useWithdrawNotice,
} from "./queries";

/**
 * Production accepted this step but has not applied it yet (202, ADR 0046): the public host holds
 * no findings, no gate and no draft, so it takes the write down and development applies it. The
 * line is factual — it never says the step happened, because it may still be refused.
 */
function WritePending({ result }: { result: unknown }) {
  const { t } = useTranslation();
  if (!isPendingWrite(result)) return null;
  return (
    <p role="status" className="text-sm text-slate-600">
      {t("notices.pending")}
    </p>
  );
}

const INPUT = "mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm";
const LABEL = "block text-sm font-medium text-slate-700";
const SECTION = "rounded-lg border border-slate-200 bg-white p-4";

export function NoticeDraftScreen() {
  const params = useParams({ from: "/app/originals/$originalId/notices" });
  const originalId = parsePublicId("orig", params.originalId);
  if (originalId === null) return <NotFound what="original" />;
  return <NoticeDraftLoader originalId={originalId} />;
}

function NoticeDraftLoader({ originalId }: { originalId: string }) {
  const { t } = useTranslation();
  const candidates = useNoticeCandidates(originalId);
  const notices = useNotices();

  if (candidates.isPending || notices.isPending) return <Loading />;
  if (candidates.isError) {
    return isNotFound(candidates.error) ? <NotFound what="original" /> : <LoadError onRetry={() => void candidates.refetch()} />;
  }
  if (notices.isError) return <LoadError onRetry={() => void notices.refetch()} />;

  const forThisRecord = notices.data.filter((notice) => notice.original_id === originalId);

  return (
    <section aria-labelledby="notices-heading" className="flex flex-col gap-4">
      <div>
        <h1 id="notices-heading" className="text-xl font-bold">
          {t("notices.heading", { title: candidates.data.title })}
        </h1>
        <p className="mt-1 text-sm text-slate-600">{t("notices.lead")}</p>
        <p className="mt-1 text-xs text-slate-500">{t("notices.disclaimer")}</p>
      </div>
      <NoticeSubmissionsBlock />
      <CreateSection originalId={originalId} candidates={candidates.data} />
      {forThisRecord.length > 0 ? (
        <div className="flex flex-col gap-4">
          <h2 className="text-sm font-semibold text-slate-700">{t("notices.existing.heading")}</h2>
          {forThisRecord.map((notice) => (
            <NoticeCard key={notice.id} originalId={originalId} noticeId={notice.id} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function CreateSection({ originalId, candidates }: { originalId: string; candidates: NoticeCandidates }) {
  const { t } = useTranslation();
  const [uiLanguage] = useLanguage();
  const account = useCustomerAccount();
  const create = useCreateNotice(originalId);

  const [selectedHost, setSelectedHost] = useState<string | null>(null);
  const [selectedPages, setSelectedPages] = useState<Set<string>>(new Set());
  const [exceptionTicked, setExceptionTicked] = useState<Set<string>>(new Set());
  const [channelKind, setChannelKind] = useState<ChannelKind | "">("");
  const [language, setLanguage] = useState<NoticeLanguage>(declarationLanguage(uiLanguage));
  const [claimantName, setClaimantName] = useState(account.data?.organisation?.name ?? "");
  const [claimantEmail, setClaimantEmail] = useState("");
  const [claimantAddress, setClaimantAddress] = useState("");
  const [claimantPhone, setClaimantPhone] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");

  const selectable = candidates.findings.filter((finding) => isSelectable(finding, exceptionTicked));
  const hosts = [...new Set(selectable.map((finding) => finding.site))];
  const hostFindings = selectedHost ? selectable.filter((finding) => finding.site === selectedHost) : [];
  const providers: NoticeProvider[] = selectedHost
    ? (hostFindings[0]?.providers ?? [])
    : [];
  const usableChannels = providers.flatMap((role) => role.channels.filter(isUsableChannel).map((channel) => ({ role, channel })));

  function toggle(finding: NoticeCandidateFinding) {
    if (!isSelectable(finding, exceptionTicked)) return;
    if (selectedHost !== null && selectedHost !== finding.site && !selectedPages.has(finding.page_id)) return;
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
    create.mutate(
      {
        original_id: originalId,
        page_ids: [...selectedPages],
        exception_confirmed_page_ids: [...selectedPages].filter((pageId) => exceptionTicked.has(pageId)),
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
      },
      {
        onSuccess: () => {
          setSelectedPages(new Set());
          setSelectedHost(null);
          setChannelKind("");
        },
      },
    );
  }

  return (
    <section aria-labelledby="notices-create" className={SECTION}>
      <h2 id="notices-create" className="font-semibold">
        {t("notices.create.heading")}
      </h2>
      {candidates.findings.length === 0 ? (
        <p className="mt-2 text-sm text-slate-600">{t("notices.create.none")}</p>
      ) : (
        <>
          <p className="mt-1 text-sm text-slate-600">{t("notices.create.lead")}</p>
          <ul className="mt-3 flex flex-col gap-2">
            {candidates.findings.map((finding) => {
              const needsExceptionTick = onlyNeedsExceptionTick(finding);
              const selectable = isSelectable(finding, exceptionTicked);
              return (
                <li key={finding.page_id} className="rounded border border-slate-200 p-2 text-sm">
                  <label className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={selectedPages.has(finding.page_id)}
                      disabled={
                        !selectable || (selectedHost !== null && selectedHost !== finding.site && !selectedPages.has(finding.page_id))
                      }
                      onChange={() => toggle(finding)}
                    />
                    <span className="min-w-0">
                      <span className="block break-words font-medium">{finding.site}</span>
                      <span className="block break-all text-xs text-slate-500">{finding.url}</span>
                      {needsExceptionTick ? (
                        <label className="mt-1 flex items-start gap-2 text-xs text-slate-700">
                          <input
                            type="checkbox"
                            className="mt-0.5"
                            checked={exceptionTicked.has(finding.page_id)}
                            onChange={() => toggleException(finding.page_id)}
                          />
                          <span>{t("notices.create.exceptionTick")}</span>
                        </label>
                      ) : null}
                      {!selectable && !needsExceptionTick ? (
                        <span className="mt-1 block text-xs text-amber-800">
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
            <p className="mt-2 text-xs text-slate-500">{t("notices.create.hostHint")}</p>
          ) : null}

          {selectedHost !== null ? (
            <form className="mt-4 flex flex-col gap-3" onSubmit={submit}>
              <div>
                <span className={LABEL}>{t("notices.create.recipient")}</span>
                <div className="mt-1 flex flex-col gap-1">
                  {usableChannels.length === 0 ? (
                    <p className="text-xs text-amber-800">{t("notices.blockers.no_supported_channel")}</p>
                  ) : (
                    usableChannels.map(({ role, channel }) => (
                      <label key={`${role.slot}-${channel.kind}`} className="flex items-center gap-2 text-sm">
                        <input
                          type="radio"
                          name="channel"
                          checked={channelKind === channel.kind}
                          onChange={() => setChannelKind(channel.kind)}
                        />
                        <span>
                          {role.provider_name || role.hostname} — {t(`notices.channelKinds.${channel.kind}`)}
                          {role.suggested_channel_kind === channel.kind ? ` (${t("notices.create.suggested")})` : ""}
                        </span>
                      </label>
                    ))
                  )}
                </div>
              </div>

              <label className={LABEL}>
                {t("notices.create.language")}
                <select className={INPUT} value={language} onChange={(event) => setLanguage(event.target.value as NoticeLanguage)}>
                  <option value="en">{t("notices.languages.en")}</option>
                  <option value="de">{t("notices.languages.de")}</option>
                </select>
              </label>

              <label className={LABEL}>
                {t("notices.create.claimantName")}
                <input className={INPUT} value={claimantName} onChange={(event) => setClaimantName(event.target.value)} required />
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
                <input className={INPUT} value={claimantAddress} onChange={(event) => setClaimantAddress(event.target.value)} />
              </label>
              <label className={LABEL}>
                {t("notices.create.claimantPhone")}
                <input className={INPUT} value={claimantPhone} onChange={(event) => setClaimantPhone(event.target.value)} />
              </label>
              <label className={LABEL}>
                {t("notices.create.sourceUrl")}
                <input className={INPUT} value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} />
              </label>

              <Button
                type="submit"
                tone="primary"
                disabled={!channelKind || create.isPending || !claimantEmail.trim() || !claimantName.trim()}
              >
                {t("notices.create.submit")}
              </Button>
              {create.error ? (
                <p role="alert" className="text-sm text-rose-800">
                  {t(errorKey(create.error))}
                </p>
              ) : null}
              <WritePending result={create.data} />
            </form>
          ) : null}
        </>
      )}
    </section>
  );
}

function NoticeCard({ originalId, noticeId }: { originalId: string; noticeId: string }) {
  const { t } = useTranslation();
  const notice = useNotice(noticeId);
  const formatDate = useFormatDate();

  if (notice.isPending) return <Loading />;
  if (notice.isError) return <LoadError onRetry={() => void notice.refetch()} />;
  const data = notice.data;

  return (
    <article aria-label={data.work_title} className={SECTION} data-notice-status={data.status}>
      <TitleRow
        title={
          <div>
            <div className="font-medium">{data.recipient_provider_name || data.recipient_hostname}</div>
            <div className="text-xs text-slate-500">
              {t(`notices.variants.${data.variant}`)} · {formatDate(data.created_at)}
              {data.is_sandbox ? ` · ${t("notices.sandboxLabel")}` : ""}
            </div>
          </div>
        }
        badge={
          <span data-status={data.status} className={`${BADGE} bg-slate-100 text-slate-700`}>
            {t(`notices.statuses.${data.status}`)}
          </span>
        }
      />

      {data.status === "draft" ? <ApproveForm originalId={originalId} notice={data} /> : null}
      {data.status === "customer_approved" ? <ApprovedView originalId={originalId} notice={data} /> : null}
      {data.status === "marked_sent_by_customer" ? <ResponseForm originalId={originalId} notice={data} /> : null}
      {data.status === "provider_responded" && data.outcome ? (
        <p className="mt-2 text-sm text-slate-700">{t(`notices.outcomes.${data.outcome}`)}</p>
      ) : null}
      {data.removal_observed_at ? (
        <p className="mt-2 text-sm text-slate-700">
          {t("notices.removalObserved", { date: formatDate(data.removal_observed_at) })}
        </p>
      ) : null}
      {data.status === "withdrawn" && data.withdrawal_reason ? (
        <p className="mt-2 text-sm text-slate-700">{t(`notices.withdrawalReasons.${data.withdrawal_reason}`)}</p>
      ) : null}
      {data.status === "corrected" && data.corrected_into ? (
        <p className="mt-2 text-sm text-slate-700">{t("notices.correctedInto")}</p>
      ) : null}

      {isOpen(data.status) ? <WithdrawAndCorrect originalId={originalId} notice={data} /> : null}
    </article>
  );
}

function ApproveForm({ originalId, notice }: { originalId: string; notice: Notice }) {
  const { t } = useTranslation();
  const approve = useApproveNotice(originalId);
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
    approve.mutate({
      noticeId: notice.id,
      body: notice.required_confirmations.map((item) => ({
        kind: item.kind,
        version: item.version,
        language: notice.language,
        confirmed: true,
      })),
    });
  }

  return (
    <form className="mt-3 flex flex-col gap-3" onSubmit={submit}>
      <div>
        <h3 className="text-sm font-medium text-slate-700">{t("notices.preview.heading")}</h3>
        <pre className="mt-1 max-h-64 overflow-auto rounded border border-slate-200 bg-slate-50 p-2 text-xs whitespace-pre-wrap">
          {notice.preview_text}
        </pre>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium text-slate-700">{t("notices.confirmations.heading")}</legend>
        {notice.required_confirmations.map((item) => (
          <label key={item.kind} className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-0.5" checked={checked.has(item.kind)} onChange={() => toggle(item.kind)} />
            <span>{notice.language === "de" ? item.de : item.en}</span>
          </label>
        ))}
      </fieldset>
      <Button type="submit" tone="primary" disabled={!allChecked || approve.isPending}>
        {t("notices.confirmations.approve")}
      </Button>
      {approve.error ? (
        <p role="alert" className="text-sm text-rose-800">
          {t(errorKey(approve.error))}
        </p>
      ) : null}
      <WritePending result={approve.data} />
    </form>
  );
}

/**
 * The three files of an approved draft (ADR 0035 amendment 2026-09-18). Plain same-origin links
 * with `download`, exactly like the invoice PDF: the browser fetches them with the session
 * cookie, and nothing about the notice passes through JavaScript here.
 */
function DownloadLinks({ notice }: { notice: Notice }) {
  const { t } = useTranslation();
  const labels = {
    txt: "notices.approved.downloadText",
    eml: "notices.approved.downloadEml",
    annex: "notices.approved.downloadAnnex",
  } as const;
  return (
    <>
      {EXPORT_FORMATS.map((format) => (
        <a
          key={format}
          href={noticeExportPath(notice.id, format)}
          download
          className="text-sm text-sky-800 underline"
        >
          {t(labels[format])}
        </a>
      ))}
    </>
  );
}


function ApprovedView({ originalId, notice }: { originalId: string; notice: Notice }) {
  const { t } = useTranslation();
  const markSent = useMarkNoticeSent(originalId);
  const sandboxSend = useSandboxSendNotice(originalId);
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
      <pre className="max-h-64 overflow-auto rounded border border-slate-200 bg-slate-50 p-2 text-xs whitespace-pre-wrap">
        {notice.approved_text}
      </pre>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" onClick={() => void copy()}>
          {copied ? t("notices.approved.copied") : t("notices.approved.copy")}
        </Button>
        {link ? (
          <a href={link} className="text-sm text-sky-800 underline">
            {t("notices.approved.mailto")}
          </a>
        ) : null}
        {canExport(notice) ? <DownloadLinks notice={notice} /> : null}
        {!notice.is_sandbox ? (
          <Button type="button" tone="primary" disabled={markSent.isPending} onClick={() => markSent.mutate(notice.id)}>
            {t("notices.approved.markSent")}
          </Button>
        ) : null}
      </div>
      {canExport(notice) ? (
        <p className="text-xs text-slate-600">{t("notices.approved.downloadHint")}</p>
      ) : null}
      {markSent.error ? (
        <p role="alert" className="text-sm text-rose-800">
          {t(errorKey(markSent.error))}
        </p>
      ) : null}
      <WritePending result={markSent.data} />

      {notice.is_sandbox ? (
        <div className="rounded border border-dashed border-amber-300 bg-amber-50 p-2 text-sm">
          <p className="text-amber-900">{t("notices.sandbox.explain")}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              type="email"
              className={INPUT + " w-auto"}
              placeholder={t("notices.sandbox.addressPlaceholder")}
              value={sandboxAddress}
              onChange={(event) => setSandboxAddress(event.target.value)}
            />
            <Button
              type="button"
              disabled={!sandboxAddress.trim() || sandboxSend.isPending}
              onClick={() => sandboxSend.mutate({ noticeId: notice.id, body: { recipient_email: sandboxAddress.trim() } })}
            >
              {t("notices.sandbox.send")}
            </Button>
          </div>
          {sandboxSend.error ? (
            <p role="alert" className="mt-1 text-rose-800">
              {t(errorKey(sandboxSend.error))}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ResponseForm({ originalId, notice }: { originalId: string; notice: Notice }) {
  const { t } = useTranslation();
  const record = useRecordNoticeResponse(originalId);
  const [outcome, setOutcome] = useState<"removed" | "refused" | "no_response">("removed");

  function submit(event: FormEvent) {
    event.preventDefault();
    record.mutate({ noticeId: notice.id, body: { outcome } });
  }

  return (
    <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={submit}>
      <label className={LABEL}>
        {t("notices.response.outcome")}
        <select className={INPUT} value={outcome} onChange={(event) => setOutcome(event.target.value as typeof outcome)}>
          <option value="removed">{t("notices.outcomes.removed")}</option>
          <option value="refused">{t("notices.outcomes.refused")}</option>
          <option value="no_response">{t("notices.outcomes.no_response")}</option>
        </select>
      </label>
      <Button type="submit" tone="primary" disabled={record.isPending}>
        {t("notices.response.submit")}
      </Button>
      {record.error ? (
        <p role="alert" className="text-sm text-rose-800">
          {t(errorKey(record.error))}
        </p>
      ) : null}
      <WritePending result={record.data} />
    </form>
  );
}

function WithdrawAndCorrect({ originalId, notice }: { originalId: string; notice: Notice }) {
  const { t } = useTranslation();
  const withdraw = useWithdrawNotice(originalId);
  const correct = useCorrectNotice(originalId);
  const [open, setOpen] = useState<"withdraw" | "correct" | null>(null);
  const [reason, setReason] = useState<WithdrawalReason | CorrectionReason | "">("");
  const [note, setNote] = useState("");

  function submitWithdraw(event: FormEvent) {
    event.preventDefault();
    if (!reason) return;
    withdraw.mutate(
      { noticeId: notice.id, body: { reason, note } },
      { onSuccess: () => setOpen(null) },
    );
  }

  function submitCorrect(event: FormEvent) {
    event.preventDefault();
    if (!reason) return;
    correct.mutate(
      { noticeId: notice.id, body: { reason, note } },
      { onSuccess: () => setOpen(null) },
    );
  }

  if (open === null) {
    return (
      <div className="mt-3 flex gap-2 text-xs">
        <button type="button" className="text-slate-500 underline" onClick={() => setOpen("withdraw")}>
          {t("notices.withdraw.open")}
        </button>
        <button type="button" className="text-slate-500 underline" onClick={() => setOpen("correct")}>
          {t("notices.correct.open")}
        </button>
      </div>
    );
  }

  const reasons = open === "withdraw" ? WITHDRAWAL_REASONS : CORRECTION_REASONS;
  const error = open === "withdraw" ? withdraw.error : correct.error;
  const accepted = open === "withdraw" ? withdraw.data : correct.data;

  return (
    <form className="mt-3 flex flex-col gap-2 border-t border-slate-200 pt-3" onSubmit={open === "withdraw" ? submitWithdraw : submitCorrect}>
      <label className={LABEL}>
        {open === "withdraw" ? t("notices.withdraw.reason") : t("notices.correct.reason")}
        <select className={INPUT} value={reason} onChange={(event) => setReason(event.target.value as typeof reason)} required>
          <option value="">{t("notices.reasonChoose")}</option>
          {reasons.map((code) => (
            <option key={code} value={code}>
              {t(`notices.${open === "withdraw" ? "withdrawalReasons" : "correctionReasons"}.${code}`)}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("notices.noteLabel")}
        <textarea className={INPUT} rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
      </label>
      <div className="flex gap-2">
        <Button type="submit" tone={open === "withdraw" ? "reject" : "primary"} disabled={!reason}>
          {open === "withdraw" ? t("notices.withdraw.submit") : t("notices.correct.submit")}
        </Button>
        <Button type="button" onClick={() => setOpen(null)}>
          {t("notices.cancel")}
        </Button>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-rose-800">
          {t(errorKey(error))}
        </p>
      ) : null}
      <WritePending result={accepted} />
    </form>
  );
}
