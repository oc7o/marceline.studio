/**
 * The rights status (`/app/rights`, ADR 0034): who the organisation is as a claimant, a
 * self-declaration per work, licensed partners, and rights documents for our staff to check for
 * completeness (ADR 0034 amendment 2026-09-18: formal, never what a document legally means) — and
 * the customer's own declaration that those documents give them the right or authority they claim,
 * which is the one statement nobody here may make for them.
 *
 * Factual wording only: a self-declaration is the customer's statement, a status is what our staff
 * recorded, and nothing here says anything about infringement. Declaration texts come from the API
 * and are shown verbatim; everything else is codes turned into words from `src/i18n`.
 *
 * On production (ADR 0028 write path) a write answers 202: accepted, not yet applied. The mutation
 * hooks (`queries.ts`) surface that as a `pending` result; `WritePending` below renders the factual
 * banner ("received, being processed") in both languages instead of the usual success state.
 */
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../components/ui";
import { useLanguage } from "../i18n";
import { BADGE, LoadError, Loading, TitleRow, isNotFound, useFormatDate } from "./parts";
import {
  useAddPartner,
  useAffirmNoPartners,
  useCustomerRights,
  useDeclareWork,
  useRemovePartner,
  useRequestAccountToken,
  useSetClaimantType,
  useUploadProof,
  useDeclareAuthority,
  useWithdrawAuthority,
  useWithdrawDeclaration,
} from "./queries";
import {
  ASSERTED_RIGHTS,
  type AssertedRight,
  CAPACITIES,
  CLAIMANT_TYPES,
  type Capacity,
  type ClaimantType,
  PARTNER_KINDS,
  PROOF_KINDS,
  type PartnerKind,
  type ProofKind,
  RIGHTS_STATUS_TONES,
  WITHDRAWAL_REASONS,
  type WithdrawalReason,
  type RightsOverview,
  type RightsWork,
  canDeclare,
  canDeclareAuthority,
  canWithdraw,
  canWithdrawAuthority,
  declarationLanguage,
  errorKey,
  formatBytes,
  isPendingWrite,
  proofForm,
} from "./rights";

const INPUT = "mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm";
const LABEL = "block text-sm font-medium text-slate-700";
const SECTION = "rounded-lg border border-slate-200 bg-white p-4";

export function RightsScreen() {
  const { t } = useTranslation();
  const rights = useCustomerRights();

  return (
    <section aria-labelledby="rights-heading" className="flex flex-col gap-4">
      <div>
        <h1 id="rights-heading" className="text-xl font-bold">
          {t("rights.heading")}
        </h1>
        <p className="mt-1 text-sm text-slate-600">{t("rights.lead")}</p>
      </div>
      {rights.isPending ? (
        <Loading />
      ) : rights.isError && isNotFound(rights.error) ? (
        <p role="note" className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {t("rights.notOnline")}
        </p>
      ) : rights.isError ? (
        <LoadError onRetry={() => void rights.refetch()} />
      ) : (
        <>
          <ClaimantSection overview={rights.data} />
          <WorksSection overview={rights.data} />
          <PartnersSection overview={rights.data} />
          <ProofsSection overview={rights.data} />
        </>
      )}
    </section>
  );
}

function WriteError({ error }: { error: unknown }) {
  const { t } = useTranslation();
  if (!error) return null;
  return (
    <p role="alert" className="mt-2 text-sm text-rose-800">
      {t(errorKey(error))}
    </p>
  );
}

/** Production (ADR 0028 write path): a write answered 202, accepted but not yet applied. Factual,
 * both languages (`rights.pending`), never a claim that it already happened. */
function WritePending({ result }: { result: unknown }) {
  const { t } = useTranslation();
  if (!isPendingWrite(result)) return null;
  return (
    <p role="status" className="mt-2 text-sm text-slate-600">
      {t("rights.pending")}
    </p>
  );
}

function ClaimantSection({ overview }: { overview: RightsOverview }) {
  const { t } = useTranslation();
  const { claimant } = overview;
  const setType = useSetClaimantType();
  const requestToken = useRequestAccountToken();
  const [choice, setChoice] = useState<ClaimantType | "">(claimant.claimant_type ?? "");

  return (
    <section aria-labelledby="rights-claimant" className={SECTION}>
      <h2 id="rights-claimant" className="font-semibold">
        {t("rights.claimant.heading")}
      </h2>
      <p className="mt-1 text-sm text-slate-600">{t("rights.claimant.lead")}</p>
      <form
        className="mt-3 flex flex-wrap items-end gap-2"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          if (choice) setType.mutate(choice);
        }}
      >
        <label className={LABEL}>
          {t("rights.claimant.label")}
          <select
            className={INPUT}
            value={choice}
            onChange={(event) => setChoice(event.target.value as ClaimantType | "")}
          >
            <option value="">{t("rights.claimant.choose")}</option>
            {CLAIMANT_TYPES.map((type) => (
              <option key={type} value={type}>
                {t(`rights.claimant.types.${type}`)}
              </option>
            ))}
          </select>
        </label>
        <Button type="submit" tone="primary" disabled={!choice || choice === claimant.claimant_type || setType.isPending}>
          {t("rights.claimant.save")}
        </Button>
      </form>
      <WriteError error={setType.error} />
      <WritePending result={setType.data} />
      <dl className="mt-3 text-sm">
        <dt className="font-medium">{t("rights.claimant.identity")}</dt>
        <dd data-identity={claimant.identity_status}>
          {t(`rights.claimant.identityStatus.${claimant.identity_status}`)}
          {claimant.identity_method ? ` (${t(`rights.claimant.identityMethod.${claimant.identity_method}`)})` : ""}
        </dd>
      </dl>
      {claimant.identity_status !== "verified" ? (
        <div className="mt-3 text-sm">
          {claimant.account_control_token ? (
            <p>
              {t("rights.claimant.token")}: <code className="rounded bg-slate-100 px-1">{claimant.account_control_token}</code>
            </p>
          ) : null}
          <p className="mt-1 text-slate-600">{t("rights.claimant.tokenHelp")}</p>
          <Button className="mt-2" onClick={() => requestToken.mutate()} disabled={requestToken.isPending}>
            {claimant.account_control_token ? t("rights.claimant.renewToken") : t("rights.claimant.requestToken")}
          </Button>
          <WriteError error={requestToken.error} />
          <WritePending result={requestToken.data} />
        </div>
      ) : null}
    </section>
  );
}

function WorksSection({ overview }: { overview: RightsOverview }) {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="rights-works" className={SECTION}>
      <h2 id="rights-works" className="font-semibold">
        {t("rights.works.heading")}
      </h2>
      <p className="mt-1 text-sm text-slate-600">{t("rights.works.lead")}</p>
      {overview.works.length === 0 ? (
        <p className="mt-3 text-sm text-slate-600">{t("rights.works.empty")}</p>
      ) : (
        <ul className="mt-3 flex flex-col gap-2">
          {overview.works.map((work) => (
            <li key={work.original_id}>
              <WorkEntry work={work} overview={overview} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function WorkEntry({ work, overview }: { work: RightsWork; overview: RightsOverview }) {
  const { t } = useTranslation();
  const formatDate = useFormatDate();
  const [open, setOpen] = useState(false);
  const [withdrawing, setWithdrawing] = useState(false);
  const [authority, setAuthority] = useState<"declare" | "withdraw" | null>(null);

  return (
    <article aria-label={work.title} className="rounded border border-slate-200 p-3">
      <TitleRow
        title={<div className="font-medium">{work.title}</div>}
        badge={
          <span data-status={work.rights_status} className={`${BADGE} ${RIGHTS_STATUS_TONES[work.rights_status]}`}>
            {t(`rights.works.status.${work.rights_status}`)}
          </span>
        }
      />
      {work.capacity && work.asserted_right ? (
        <p className="mt-1 text-xs text-slate-600">
          {t(`rights.works.capacities.${work.capacity}`)}
          {work.underlying_owner_name ? ` (${work.underlying_owner_name})` : ""} ·{" "}
          {t(`rights.works.rights.${work.asserted_right}`)}
          {work.declared_at ? ` · ${t("rights.works.declaredAt", { date: formatDate(work.declared_at) })}` : ""}
          {work.authority_declared_at
            ? ` · ${t("rights.works.authorityDeclaredAt", { date: formatDate(work.authority_declared_at) })}`
            : ""}
        </p>
      ) : null}
      {work.notice_blockers.length === 0 ? (
        <p className="mt-2 text-xs text-slate-600">{t("rights.works.ready")}</p>
      ) : (
        <div className="mt-2 text-xs text-slate-600">
          <p>{t("rights.works.missing")}</p>
          <ul className="ml-4 list-disc">
            {work.notice_blockers.map((blocker) => (
              <li key={blocker}>{t(`rights.works.blockers.${blocker}`)}</li>
            ))}
          </ul>
        </div>
      )}
      {canDeclare(work) && !open ? (
        <Button className="mt-2" onClick={() => setOpen(true)}>
          {work.rights_status === "self_declared" ? t("rights.works.redeclare") : t("rights.works.declare")}
        </Button>
      ) : null}
      {canWithdraw(work) && !open && !withdrawing ? (
        <Button className="mt-2 ml-2" onClick={() => setWithdrawing(true)}>
          {t("rights.works.withdraw")}
        </Button>
      ) : null}
      {canDeclareAuthority(work) && authority === null ? (
        <Button className="mt-2" tone="primary" onClick={() => setAuthority("declare")}>
          {t("rights.works.authority")}
        </Button>
      ) : null}
      {canWithdrawAuthority(work) && authority === null ? (
        <Button className="mt-2" onClick={() => setAuthority("withdraw")}>
          {t("rights.works.authorityWithdraw")}
        </Button>
      ) : null}
      {open ? <DeclarationForm work={work} overview={overview} onDone={() => setOpen(false)} /> : null}
      {withdrawing ? (
        <WithdrawalForm work={work} overview={overview} onDone={() => setWithdrawing(false)} />
      ) : null}
      {authority === "declare" ? (
        <AuthorityForm work={work} overview={overview} onDone={() => setAuthority(null)} />
      ) : null}
      {authority === "withdraw" ? (
        <AuthorityWithdrawalForm work={work} overview={overview} onDone={() => setAuthority(null)} />
      ) : null}
    </article>
  );
}

function DeclarationForm({
  work,
  overview,
  onDone,
}: {
  work: RightsWork;
  overview: RightsOverview;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [uiLanguage] = useLanguage();
  const language = declarationLanguage(uiLanguage);
  const declare = useDeclareWork();
  const [capacity, setCapacity] = useState<Capacity>(work.capacity ?? "owner");
  const [right, setRight] = useState<AssertedRight>(work.asserted_right ?? "copyright");
  const [owner, setOwner] = useState(work.underlying_owner_name ?? "");
  const [detail, setDetail] = useState(work.asserted_right_detail);
  const [confirmed, setConfirmed] = useState(false);
  const text = overview.work_declaration[language];

  return (
    <form
      aria-label={t("rights.works.declare")}
      className="mt-3 flex flex-col gap-2 border-t border-slate-200 pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        declare.mutate(
          {
            originalId: work.original_id,
            body: {
              capacity,
              asserted_right: right,
              underlying_owner_name: capacity === "authorised_agent" ? owner : "",
              asserted_right_detail: detail,
              declaration_version: overview.work_declaration.version,
              language,
              confirmed,
            },
          },
          { onSuccess: onDone },
        );
      }}
    >
      <label className={LABEL}>
        {t("rights.works.capacity")}
        <select className={INPUT} value={capacity} onChange={(event) => setCapacity(event.target.value as Capacity)}>
          {CAPACITIES.map((code) => (
            <option key={code} value={code}>
              {t(`rights.works.capacities.${code}`)}
            </option>
          ))}
        </select>
      </label>
      {capacity === "authorised_agent" ? (
        <label className={LABEL}>
          {t("rights.works.underlyingOwner")}
          <input className={INPUT} value={owner} maxLength={200} onChange={(event) => setOwner(event.target.value)} />
        </label>
      ) : null}
      <label className={LABEL}>
        {t("rights.works.right")}
        <select className={INPUT} value={right} onChange={(event) => setRight(event.target.value as AssertedRight)}>
          {ASSERTED_RIGHTS.map((code) => (
            <option key={code} value={code}>
              {t(`rights.works.rights.${code}`)}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("rights.works.rightDetail")}
        <textarea className={INPUT} rows={2} maxLength={2000} value={detail} onChange={(event) => setDetail(event.target.value)} />
      </label>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.work_declaration.version}>{text}</span>
        </span>
      </label>
      <div className="flex gap-2">
        <Button type="submit" tone="primary" disabled={!confirmed || declare.isPending}>
          {t("rights.works.submit")}
        </Button>
        <Button onClick={onDone}>{t("rights.works.cancel")}</Button>
      </div>
      <WriteError error={declare.error} />
      <WritePending result={declare.data} />
    </form>
  );
}

function WithdrawalForm({
  work,
  overview,
  onDone,
}: {
  work: RightsWork;
  overview: RightsOverview;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [uiLanguage] = useLanguage();
  const language = declarationLanguage(uiLanguage);
  const withdraw = useWithdrawDeclaration();
  const [reason, setReason] = useState<WithdrawalReason>("rights_transferred");
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  return (
    <form
      aria-label={t("rights.works.withdraw")}
      className="mt-3 flex flex-col gap-2 border-t border-slate-200 pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        withdraw.mutate(
          {
            originalId: work.original_id,
            body: {
              reason,
              note,
              declaration_version: overview.withdrawal_declaration.version,
              language,
              confirmed,
            },
          },
          { onSuccess: onDone },
        );
      }}
    >
      <p className="text-sm text-slate-600">{t("rights.works.withdrawLead")}</p>
      <label className={LABEL}>
        {t("rights.works.withdrawReason")}
        <select className={INPUT} value={reason} onChange={(event) => setReason(event.target.value as WithdrawalReason)}>
          {WITHDRAWAL_REASONS.map((code) => (
            <option key={code} value={code}>
              {t(`rights.works.withdrawReasons.${code}`)}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("rights.works.withdrawNote")}
        <input className={INPUT} value={note} maxLength={500} onChange={(event) => setNote(event.target.value)} />
      </label>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.withdrawal_declaration.version}>
            {overview.withdrawal_declaration[language]}
          </span>
        </span>
      </label>
      <div className="flex gap-2">
        <Button type="submit" tone="primary" disabled={!confirmed || withdraw.isPending}>
          {t("rights.works.withdrawSubmit")}
        </Button>
        <Button onClick={onDone}>{t("rights.works.cancel")}</Button>
      </div>
      <WriteError error={withdraw.error} />
      <WritePending result={withdraw.data} />
    </form>
  );
}

/**
 * The customer's own statement that the documents on file give them the right or authority they
 * declared (ADR 0034 amendment 2026-09-18). Marceline never makes it: our staff record only that
 * the documents are formally complete, and this form is the only route to the rest.
 */
function AuthorityForm({
  work,
  overview,
  onDone,
}: {
  work: RightsWork;
  overview: RightsOverview;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [uiLanguage] = useLanguage();
  const language = declarationLanguage(uiLanguage);
  const declare = useDeclareAuthority();
  const [confirmed, setConfirmed] = useState(false);

  return (
    <form
      aria-label={t("rights.works.authority")}
      className="mt-3 flex flex-col gap-2 border-t border-slate-200 pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        declare.mutate(
          {
            originalId: work.original_id,
            body: { declaration_version: overview.authority_declaration.version, language, confirmed },
          },
          { onSuccess: onDone },
        );
      }}
    >
      <p className="text-sm text-slate-600">{t("rights.works.authorityLead")}</p>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.authority_declaration.version}>
            {overview.authority_declaration[language]}
          </span>
        </span>
      </label>
      <div className="flex gap-2">
        <Button type="submit" tone="primary" disabled={!confirmed || declare.isPending}>
          {t("rights.works.authoritySubmit")}
        </Button>
        <Button onClick={onDone}>{t("rights.works.cancel")}</Button>
      </div>
      <WriteError error={declare.error} />
      <WritePending result={declare.data} />
    </form>
  );
}

/** And the way back out of it: the documents stay, the customer's statement about them goes. */
function AuthorityWithdrawalForm({
  work,
  overview,
  onDone,
}: {
  work: RightsWork;
  overview: RightsOverview;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [uiLanguage] = useLanguage();
  const language = declarationLanguage(uiLanguage);
  const withdraw = useWithdrawAuthority();
  const [reason, setReason] = useState<WithdrawalReason>("authority_ended");
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  return (
    <form
      aria-label={t("rights.works.authorityWithdraw")}
      className="mt-3 flex flex-col gap-2 border-t border-slate-200 pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        withdraw.mutate(
          {
            originalId: work.original_id,
            body: {
              reason,
              note,
              declaration_version: overview.authority_withdrawal_declaration.version,
              language,
              confirmed,
            },
          },
          { onSuccess: onDone },
        );
      }}
    >
      <p className="text-sm text-slate-600">{t("rights.works.authorityWithdrawLead")}</p>
      <label className={LABEL}>
        {t("rights.works.withdrawReason")}
        <select className={INPUT} value={reason} onChange={(event) => setReason(event.target.value as WithdrawalReason)}>
          {WITHDRAWAL_REASONS.map((code) => (
            <option key={code} value={code}>
              {t(`rights.works.withdrawReasons.${code}`)}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("rights.works.withdrawNote")}
        <input className={INPUT} value={note} maxLength={500} onChange={(event) => setNote(event.target.value)} />
      </label>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.authority_withdrawal_declaration.version}>
            {overview.authority_withdrawal_declaration[language]}
          </span>
        </span>
      </label>
      <div className="flex gap-2">
        <Button type="submit" tone="primary" disabled={!confirmed || withdraw.isPending}>
          {t("rights.works.authorityWithdrawSubmit")}
        </Button>
        <Button onClick={onDone}>{t("rights.works.cancel")}</Button>
      </div>
      <WriteError error={withdraw.error} />
      <WritePending result={withdraw.data} />
    </form>
  );
}

function PartnersSection({ overview }: { overview: RightsOverview }) {
  const { t } = useTranslation();
  const [uiLanguage] = useLanguage();
  const language = declarationLanguage(uiLanguage);
  const formatDate = useFormatDate();
  const add = useAddPartner();
  const remove = useRemovePartner();
  const affirm = useAffirmNoPartners();
  const [kind, setKind] = useState<PartnerKind>("domain");
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [pathPrefix, setPathPrefix] = useState("");
  const [confirmedNone, setConfirmedNone] = useState(false);

  return (
    <section aria-labelledby="rights-partners" className={SECTION}>
      <h2 id="rights-partners" className="font-semibold">
        {t("rights.partners.heading")}
      </h2>
      <p className="mt-1 text-sm text-slate-600">{t("rights.partners.lead")}</p>
      {overview.partners.length === 0 ? (
        <p className="mt-3 text-sm text-slate-600">
          {overview.partners_none_affirmed_at
            ? t("rights.partners.noneAffirmed", { date: formatDate(overview.partners_none_affirmed_at) })
            : t("rights.partners.empty")}
        </p>
      ) : (
        <ul className="mt-3 flex flex-col gap-1 text-sm">
          {overview.partners.map((partner) => (
            <li key={partner.id} className="flex flex-wrap items-center justify-between gap-2">
              <span>
                <span className="font-medium">{partner.name}</span> · {t(`rights.partners.kinds.${partner.kind}`)}
                {partner.domain ? ` · ${partner.domain}${partner.path_prefix ?? ""}` : ""}
              </span>
              <Button onClick={() => remove.mutate(partner.id)} disabled={remove.isPending}>
                {t("rights.partners.remove", { name: partner.name })}
              </Button>
            </li>
          ))}
        </ul>
      )}
      <WriteError error={remove.error} />
      <WritePending result={remove.data} />
      <form
        aria-label={t("rights.partners.add")}
        className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          add.mutate(
            { kind, name, domain, path_prefix: pathPrefix, note: "" },
            {
              onSuccess: () => {
                setName("");
                setDomain("");
                setPathPrefix("");
              },
            },
          );
        }}
      >
        <label className={LABEL}>
          {t("rights.partners.kind")}
          <select className={INPUT} value={kind} onChange={(event) => setKind(event.target.value as PartnerKind)}>
            {PARTNER_KINDS.map((code) => (
              <option key={code} value={code}>
                {t(`rights.partners.kinds.${code}`)}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          {t("rights.partners.name")}
          <input className={INPUT} value={name} maxLength={200} onChange={(event) => setName(event.target.value)} />
        </label>
        <label className={LABEL}>
          {t("rights.partners.domain")}
          <input className={INPUT} value={domain} maxLength={253} onChange={(event) => setDomain(event.target.value)} />
        </label>
        <label className={LABEL}>
          {t("rights.partners.pathPrefix")}
          <input className={INPUT} value={pathPrefix} maxLength={500} onChange={(event) => setPathPrefix(event.target.value)} />
        </label>
        <div className="sm:col-span-2">
          <Button type="submit" tone="primary" disabled={!name.trim() || add.isPending}>
            {t("rights.partners.add")}
          </Button>
          <WriteError error={add.error} />
          <WritePending result={add.data} />
        </div>
      </form>
      {overview.partners.length === 0 && !overview.partners_none_affirmed_at ? (
        <form
          aria-label={t("rights.partners.affirmNone")}
          className="mt-4 flex flex-col gap-2 border-t border-slate-200 pt-3"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            affirm.mutate({
              declaration_version: overview.partner_declaration.version,
              language,
              confirmed: confirmedNone,
            });
          }}
        >
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={confirmedNone}
              onChange={(event) => setConfirmedNone(event.target.checked)}
            />
            <span>
              <span className="block font-medium">{t("rights.partners.confirmNone")}</span>
              <span data-declaration-version={overview.partner_declaration.version}>
                {overview.partner_declaration[language]}
              </span>
            </span>
          </label>
          <div>
            <Button type="submit" tone="primary" disabled={!confirmedNone || affirm.isPending}>
              {t("rights.partners.affirmNone")}
            </Button>
          </div>
          <WriteError error={affirm.error} />
          <WritePending result={affirm.data} />
        </form>
      ) : null}
    </section>
  );
}

function ProofsSection({ overview }: { overview: RightsOverview }) {
  const { t } = useTranslation();
  const [language] = useLanguage();
  const formatDate = useFormatDate();
  const upload = useUploadProof();
  const [kind, setKind] = useState<ProofKind>("contract_excerpt");
  const [originalId, setOriginalId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [formKey, setFormKey] = useState(0);
  const titles = new Map(overview.works.map((work) => [work.original_id, work.title]));

  return (
    <section aria-labelledby="rights-proofs" className={SECTION}>
      <h2 id="rights-proofs" className="font-semibold">
        {t("rights.proofs.heading")}
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        {t("rights.proofs.lead", { size: formatBytes(overview.proof_upload.max_bytes, language) })}
      </p>
      {overview.proofs.length === 0 ? (
        <p className="mt-3 text-sm text-slate-600">{t("rights.proofs.empty")}</p>
      ) : (
        <ul className="mt-3 flex flex-col gap-1 text-sm">
          {overview.proofs.map((proof) => (
            <li key={proof.id}>
              <span className="font-medium">{proof.filename}</span> · {t(`rights.proofs.kinds.${proof.kind}`)}
              {proof.original_id !== null && titles.has(proof.original_id) ? ` · ${titles.get(proof.original_id) ?? ""}` : ""}
              {" · "}
              {t("rights.proofs.uploadedAt", { date: formatDate(proof.uploaded_at) })}
            </li>
          ))}
        </ul>
      )}
      {overview.proof_upload.available ? (
        <form
          key={formKey}
          aria-label={t("rights.proofs.upload")}
          className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            if (!file) return;
            upload.mutate(proofForm(file, kind, originalId || null, ""), {
              onSuccess: () => {
                setFile(null);
                setFormKey((value) => value + 1);
              },
            });
          }}
        >
          <label className={LABEL}>
            {t("rights.proofs.kind")}
            <select className={INPUT} value={kind} onChange={(event) => setKind(event.target.value as ProofKind)}>
              {PROOF_KINDS.map((code) => (
                <option key={code} value={code}>
                  {t(`rights.proofs.kinds.${code}`)}
                </option>
              ))}
            </select>
          </label>
          <label className={LABEL}>
            {t("rights.proofs.original")}
            <select className={INPUT} value={originalId} onChange={(event) => setOriginalId(event.target.value)}>
              <option value="">{t("rights.proofs.noOriginal")}</option>
              {overview.works.map((work) => (
                <option key={work.original_id} value={work.original_id}>
                  {work.title}
                </option>
              ))}
            </select>
          </label>
          <label className={`${LABEL} sm:col-span-2`}>
            {t("rights.proofs.file")}
            <input
              type="file"
              className={INPUT}
              accept={overview.proof_upload.content_types.join(",")}
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </label>
          <div className="sm:col-span-2">
            <Button type="submit" tone="primary" disabled={!file || upload.isPending}>
              {upload.isPending ? t("rights.proofs.uploading") : t("rights.proofs.upload")}
            </Button>
            <WriteError error={upload.error} />
          </div>
        </form>
      ) : (
        <p className="mt-3 text-sm text-slate-600">{t("rights.proofs.unavailable")}</p>
      )}
    </section>
  );
}
