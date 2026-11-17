"use client";
/**
 * The rights status (`/app/rights`, ADR 0034): who the organisation is as a claimant, a
 * self-declaration per work, licensed partners, and rights documents for our staff to check for
 * completeness (ADR 0034 amendment 2026-09-18: formal, never what a document legally means) — and
 * the customer's own declaration that those documents give them the right or authority they claim,
 * which is the one statement nobody here may make for them.
 *
 * The one screen that is a client component from top to bottom, and for a plain reason: almost
 * every line of it is a form. Its words arrive as the `rights` and `common` slices of the request's
 * resources, so the language file still never ships as JavaScript (ADR 0036), and its writes go to
 * the named BFF routes under `/app/api/rights/…`.
 *
 * Factual wording only: a self-declaration is the customer's statement, a status is what our staff
 * recorded, and nothing here says anything about infringement. Declaration texts come from the API
 * and are shown verbatim.
 *
 * On production (ADR 0028 write path) a write answers 202: accepted, not yet applied. `WritePending`
 * renders the factual banner ("received, being processed") instead of a success state.
 */
import { type FormEvent, useState } from "react";

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
  type RightsOverview,
  type RightsWork,
  WITHDRAWAL_REASONS,
  type WithdrawalReason,
  canDeclare,
  canDeclareAuthority,
  canWithdraw,
  canWithdrawAuthority,
  declarationLanguage,
  errorKey,
  formatBytes,
  isPendingWrite,
  proofForm,
} from "@shared/app/rights";

import { RIGHTS_TONES } from "../lib/tones";
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

interface Ctx {
  t: Translate;
  language: string;
  overview: RightsOverview;
  /**
   * Whether the account-control step exists on this machine (ADR 0055, `me.can_verify_account`).
   *
   * With it on, the identity block points at `/app/verification` — the code, where to put it and the
   * standing all live there — instead of offering ADR 0034's bare token, which asked the creator to
   * "tell us where" in a channel this product does not have. With it off nothing changes.
   */
  accountStep: boolean;
}

export function RightsScreen({
  language,
  words,
  overview,
  accountStep = false,
}: {
  language: string;
  words: Resource;
  overview: RightsOverview;
  accountStep?: boolean;
}) {
  const t = translator(words, language);
  const ctx: Ctx = { t, language, overview, accountStep };
  return (
    <section aria-labelledby="rights-heading" className="flex flex-col gap-4">
      <div>
        <h1 id="rights-heading" className="font-display text-display-m text-ink">
          {t("rights.heading")}
        </h1>
        <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("rights.lead")}</p>
      </div>
      <ClaimantSection ctx={ctx} />
      <WorksSection ctx={ctx} />
      <PartnersSection ctx={ctx} />
      <ProofsSection ctx={ctx} />
    </section>
  );
}

function WriteError({ t, write }: { t: Translate; write: WriteState }) {
  if (!write.error) return null;
  return (
    <p role="alert" className="mt-2 text-body-s text-critical-ink">
      {t(errorKey(write.error))}
    </p>
  );
}

function WritePending({ t, write }: { t: Translate; write: WriteState }) {
  if (!isPendingWrite(write.data)) return null;
  return (
    <p role="status" className="mt-2 text-body-s text-ink-muted">
      {t("rights.pending")}
    </p>
  );
}

function ClaimantSection({ ctx }: { ctx: Ctx }) {
  const { t, overview, accountStep } = ctx;
  const { claimant } = overview;
  const setType = useWrite();
  const requestToken = useWrite();
  const [choice, setChoice] = useState<ClaimantType | "">(claimant.claimant_type ?? "");

  return (
    <section aria-labelledby="rights-claimant" className={SECTION}>
      <h2 id="rights-claimant" className="font-display text-display-s text-ink">
        {t("rights.claimant.heading")}
      </h2>
      <p className="mt-1 text-body-s text-ink-muted">{t("rights.claimant.lead")}</p>
      <form
        className="mt-3 flex flex-wrap items-end gap-2"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          if (choice) {
            void setType.run("/app/api/rights/claimant", "PUT", { claimant_type: choice });
          }
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
        <button
          type="submit"
          className={PRIMARY}
          disabled={!choice || choice === claimant.claimant_type || setType.pending}
        >
          {t("rights.claimant.save")}
        </button>
      </form>
      <WriteError t={t} write={setType} />
      <WritePending t={t} write={setType} />
      <dl className="mt-3 text-body-s text-ink">
        <dt className="text-label text-ink-muted">{t("rights.claimant.identity")}</dt>
        <dd data-identity={claimant.identity_status}>
          {t(`rights.claimant.identityStatus.${claimant.identity_status}`)}
          {claimant.identity_method
            ? ` (${t(`rights.claimant.identityMethod.${claimant.identity_method}`)})`
            : ""}
        </dd>
      </dl>
      {claimant.identity_status !== "verified" && accountStep ? (
        // ADR 0055: one flow, one code, one place to read about it.
        <div className="mt-3 text-body-s text-ink">
          <p className="text-ink-muted">{t("rights.claimant.accountStep")}</p>
          <a
            href="/app/verification"
            className="tap mt-2 inline-block rounded-sm text-button text-brand-ink underline"
          >
            {t("rights.claimant.accountStepCta")}
          </a>
        </div>
      ) : null}
      {claimant.identity_status !== "verified" && !accountStep ? (
        <div className="mt-3 text-body-s text-ink">
          {claimant.account_control_token ? (
            <p>
              {t("rights.claimant.token")}:{" "}
              <code className="rounded-none border border-hairline bg-surface-inset px-1.5 py-0.5 font-mono text-hash break-all text-ink">
                {claimant.account_control_token}
              </code>
            </p>
          ) : null}
          <p className="mt-1 text-ink-muted">{t("rights.claimant.tokenHelp")}</p>
          <button
            type="button"
            className={`mt-2 ${NEUTRAL}`}
            onClick={() => void requestToken.run("/app/api/rights/claimant/token", "POST")}
            disabled={requestToken.pending}
          >
            {claimant.account_control_token
              ? t("rights.claimant.renewToken")
              : t("rights.claimant.requestToken")}
          </button>
          <WriteError t={t} write={requestToken} />
          <WritePending t={t} write={requestToken} />
        </div>
      ) : null}
    </section>
  );
}

function WorksSection({ ctx }: { ctx: Ctx }) {
  const { t, overview } = ctx;
  return (
    <section aria-labelledby="rights-works" className={SECTION}>
      <h2 id="rights-works" className="font-display text-display-s text-ink">
        {t("rights.works.heading")}
      </h2>
      <p className="mt-1 text-body-s text-ink-muted">{t("rights.works.lead")}</p>
      {overview.works.length === 0 ? (
        <p className="mt-3 text-body-s text-ink-muted">{t("rights.works.empty")}</p>
      ) : (
        <ul className="mt-3 flex flex-col gap-2">
          {overview.works.map((work) => (
            <li key={work.original_id}>
              <WorkEntry ctx={ctx} work={work} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function WorkEntry({ ctx, work }: { ctx: Ctx; work: RightsWork }) {
  const { t, language } = ctx;
  const [open, setOpen] = useState(false);
  const [withdrawing, setWithdrawing] = useState(false);
  const [authority, setAuthority] = useState<"declare" | "withdraw" | null>(null);

  return (
    <article aria-label={work.title} className="rounded-md border border-hairline p-3">
      <TitleRow
        title={<div className="text-body-l font-medium text-ink">{work.title}</div>}
        badge={
          <span
            data-status={work.rights_status}
            className={`${BADGE} ${RIGHTS_TONES[work.rights_status]}`}
          >
            {t(`rights.works.status.${work.rights_status}`)}
          </span>
        }
      />
      {work.capacity && work.asserted_right ? (
        <p className="mt-1 text-caption text-ink-muted">
          {t(`rights.works.capacities.${work.capacity}`)}
          {work.underlying_owner_name ? ` (${work.underlying_owner_name})` : ""} ·{" "}
          {t(`rights.works.rights.${work.asserted_right}`)}
          {work.declared_at
            ? ` · ${t("rights.works.declaredAt", { date: formatDate(work.declared_at, language) })}`
            : ""}
          {work.authority_declared_at
            ? ` · ${t("rights.works.authorityDeclaredAt", { date: formatDate(work.authority_declared_at, language) })}`
            : ""}
        </p>
      ) : null}
      {work.notice_blockers.length === 0 ? (
        <p className="mt-2 text-caption text-ink-muted">{t("rights.works.ready")}</p>
      ) : (
        <div className="mt-2 text-caption text-ink-muted">
          <p>{t("rights.works.missing")}</p>
          <ul className="ml-4 list-disc">
            {work.notice_blockers.map((blocker) => (
              <li key={blocker}>{t(`rights.works.blockers.${blocker}`)}</li>
            ))}
          </ul>
        </div>
      )}
      {canDeclare(work) && !open ? (
        <button type="button" className={`mt-2 ${NEUTRAL}`} onClick={() => setOpen(true)}>
          {work.rights_status === "self_declared"
            ? t("rights.works.redeclare")
            : t("rights.works.declare")}
        </button>
      ) : null}
      {canWithdraw(work) && !open && !withdrawing ? (
        <button
          type="button"
          className={`mt-2 ml-2 ${NEUTRAL}`}
          onClick={() => setWithdrawing(true)}
        >
          {t("rights.works.withdraw")}
        </button>
      ) : null}
      {canDeclareAuthority(work) && authority === null ? (
        <button type="button" className={`mt-2 ${PRIMARY}`} onClick={() => setAuthority("declare")}>
          {t("rights.works.authority")}
        </button>
      ) : null}
      {canWithdrawAuthority(work) && authority === null ? (
        <button type="button" className={`mt-2 ${NEUTRAL}`} onClick={() => setAuthority("withdraw")}>
          {t("rights.works.authorityWithdraw")}
        </button>
      ) : null}
      {open ? <DeclarationForm ctx={ctx} work={work} onDone={() => setOpen(false)} /> : null}
      {withdrawing ? (
        <WithdrawalForm ctx={ctx} work={work} onDone={() => setWithdrawing(false)} />
      ) : null}
      {authority === "declare" ? (
        <AuthorityForm ctx={ctx} work={work} onDone={() => setAuthority(null)} />
      ) : null}
      {authority === "withdraw" ? (
        <AuthorityWithdrawalForm ctx={ctx} work={work} onDone={() => setAuthority(null)} />
      ) : null}
    </article>
  );
}

function DeclarationForm({
  ctx,
  work,
  onDone,
}: {
  ctx: Ctx;
  work: RightsWork;
  onDone: () => void;
}) {
  const { t, overview } = ctx;
  const language = declarationLanguage(ctx.language);
  const declare = useWrite();
  const [capacity, setCapacity] = useState<Capacity>(work.capacity ?? "owner");
  const [right, setRight] = useState<AssertedRight>(work.asserted_right ?? "copyright");
  const [owner, setOwner] = useState(work.underlying_owner_name ?? "");
  const [detail, setDetail] = useState(work.asserted_right_detail);
  const [confirmed, setConfirmed] = useState(false);
  const text = overview.work_declaration[language];

  return (
    <form
      aria-label={t("rights.works.declare")}
      className="mt-3 flex flex-col gap-3 border-t border-hairline pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void declare
          .run(
            `/app/api/rights/works/${work.original_id}/declaration`,
            "POST",
            {
              capacity,
              asserted_right: right,
              underlying_owner_name: capacity === "authorised_agent" ? owner : "",
              asserted_right_detail: detail,
              declaration_version: overview.work_declaration.version,
              language,
              confirmed,
            },
          )
          .then((ok) => {
            if (ok) onDone();
          });
      }}
    >
      <label className={LABEL}>
        {t("rights.works.capacity")}
        <select
          className={INPUT}
          value={capacity}
          onChange={(event) => setCapacity(event.target.value as Capacity)}
        >
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
          <input
            className={INPUT}
            value={owner}
            maxLength={200}
            onChange={(event) => setOwner(event.target.value)}
          />
        </label>
      ) : null}
      <label className={LABEL}>
        {t("rights.works.right")}
        <select
          className={INPUT}
          value={right}
          onChange={(event) => setRight(event.target.value as AssertedRight)}
        >
          {ASSERTED_RIGHTS.map((code) => (
            <option key={code} value={code}>
              {t(`rights.works.rights.${code}`)}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("rights.works.rightDetail")}
        <textarea
          className={INPUT}
          rows={2}
          maxLength={2000}
          value={detail}
          onChange={(event) => setDetail(event.target.value)}
        />
      </label>
      <label className="flex items-start gap-2 text-body-s text-ink">
        <input
          type="checkbox"
          className="mt-1"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.work_declaration.version}>{text}</span>
        </span>
      </label>
      <div className="flex gap-2">
        <button type="submit" className={PRIMARY} disabled={!confirmed || declare.pending}>
          {t("rights.works.submit")}
        </button>
        <button type="button" className={NEUTRAL} onClick={onDone}>
          {t("rights.works.cancel")}
        </button>
      </div>
      <WriteError t={t} write={declare} />
      <WritePending t={t} write={declare} />
    </form>
  );
}

function WithdrawalForm({
  ctx,
  work,
  onDone,
}: {
  ctx: Ctx;
  work: RightsWork;
  onDone: () => void;
}) {
  const { t, overview } = ctx;
  const language = declarationLanguage(ctx.language);
  const withdraw = useWrite();
  const [reason, setReason] = useState<WithdrawalReason>("rights_transferred");
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  return (
    <form
      aria-label={t("rights.works.withdraw")}
      className="mt-3 flex flex-col gap-3 border-t border-hairline pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void withdraw
          .run(`/app/api/rights/works/${work.original_id}/withdrawal`, "POST", {
            reason,
            note,
            declaration_version: overview.withdrawal_declaration.version,
            language,
            confirmed,
          })
          .then((ok) => {
            if (ok) onDone();
          });
      }}
    >
      <p className="text-body-s text-ink-muted">{t("rights.works.withdrawLead")}</p>
      <label className={LABEL}>
        {t("rights.works.withdrawReason")}
        <select
          className={INPUT}
          value={reason}
          onChange={(event) => setReason(event.target.value as WithdrawalReason)}
        >
          {WITHDRAWAL_REASONS.map((code) => (
            <option key={code} value={code}>
              {t(`rights.works.withdrawReasons.${code}`)}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("rights.works.withdrawNote")}
        <input
          className={INPUT}
          value={note}
          maxLength={500}
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <label className="flex items-start gap-2 text-body-s text-ink">
        <input
          type="checkbox"
          className="mt-1"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.withdrawal_declaration.version}>
            {overview.withdrawal_declaration[language]}
          </span>
        </span>
      </label>
      <div className="flex gap-2">
        <button type="submit" className={PRIMARY} disabled={!confirmed || withdraw.pending}>
          {t("rights.works.withdrawSubmit")}
        </button>
        <button type="button" className={NEUTRAL} onClick={onDone}>
          {t("rights.works.cancel")}
        </button>
      </div>
      <WriteError t={t} write={withdraw} />
      <WritePending t={t} write={withdraw} />
    </form>
  );
}

/**
 * The customer's own statement that the documents on file give them the right or authority they
 * declared (ADR 0034 amendment 2026-09-18). Our staff record only that the documents are formally
 * complete; this form is the only route to the rest, in either app.
 */
function AuthorityForm({ ctx, work, onDone }: { ctx: Ctx; work: RightsWork; onDone: () => void }) {
  const { t, overview } = ctx;
  const language = declarationLanguage(ctx.language);
  const declare = useWrite();
  const [confirmed, setConfirmed] = useState(false);

  return (
    <form
      aria-label={t("rights.works.authority")}
      className="mt-3 flex flex-col gap-3 border-t border-hairline pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void declare
          .run(`/app/api/rights/works/${work.original_id}/authority`, "POST", {
            declaration_version: overview.authority_declaration.version,
            language,
            confirmed,
          })
          .then((ok) => {
            if (ok) onDone();
          });
      }}
    >
      <p className="text-body-s text-ink-muted">{t("rights.works.authorityLead")}</p>
      <label className="flex items-start gap-2 text-body-s text-ink">
        <input
          type="checkbox"
          className="mt-1"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.authority_declaration.version}>
            {overview.authority_declaration[language]}
          </span>
        </span>
      </label>
      <div className="flex gap-2">
        <button type="submit" className={PRIMARY} disabled={!confirmed || declare.pending}>
          {t("rights.works.authoritySubmit")}
        </button>
        <button type="button" className={NEUTRAL} onClick={onDone}>
          {t("rights.works.cancel")}
        </button>
      </div>
      <WriteError t={t} write={declare} />
      <WritePending t={t} write={declare} />
    </form>
  );
}

/** And the way back out of it: the documents stay, the customer's statement about them goes. */
function AuthorityWithdrawalForm({
  ctx,
  work,
  onDone,
}: {
  ctx: Ctx;
  work: RightsWork;
  onDone: () => void;
}) {
  const { t, overview } = ctx;
  const language = declarationLanguage(ctx.language);
  const withdraw = useWrite();
  const [reason, setReason] = useState<WithdrawalReason>("authority_ended");
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  return (
    <form
      aria-label={t("rights.works.authorityWithdraw")}
      className="mt-3 flex flex-col gap-3 border-t border-hairline pt-3"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void withdraw
          .run(`/app/api/rights/works/${work.original_id}/authority/withdrawal`, "POST", {
            reason,
            note,
            declaration_version: overview.authority_withdrawal_declaration.version,
            language,
            confirmed,
          })
          .then((ok) => {
            if (ok) onDone();
          });
      }}
    >
      <p className="text-body-s text-ink-muted">{t("rights.works.authorityWithdrawLead")}</p>
      <label className={LABEL}>
        {t("rights.works.withdrawReason")}
        <select
          className={INPUT}
          value={reason}
          onChange={(event) => setReason(event.target.value as WithdrawalReason)}
        >
          {WITHDRAWAL_REASONS.map((code) => (
            <option key={code} value={code}>
              {t(`rights.works.withdrawReasons.${code}`)}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL}>
        {t("rights.works.withdrawNote")}
        <input
          className={INPUT}
          value={note}
          maxLength={500}
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <label className="flex items-start gap-2 text-body-s text-ink">
        <input
          type="checkbox"
          className="mt-1"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          <span className="block font-medium">{t("rights.works.confirm")}</span>
          <span data-declaration-version={overview.authority_withdrawal_declaration.version}>
            {overview.authority_withdrawal_declaration[language]}
          </span>
        </span>
      </label>
      <div className="flex gap-2">
        <button type="submit" className={PRIMARY} disabled={!confirmed || withdraw.pending}>
          {t("rights.works.authorityWithdrawSubmit")}
        </button>
        <button type="button" className={NEUTRAL} onClick={onDone}>
          {t("rights.works.cancel")}
        </button>
      </div>
      <WriteError t={t} write={withdraw} />
      <WritePending t={t} write={withdraw} />
    </form>
  );
}

function PartnersSection({ ctx }: { ctx: Ctx }) {
  const { t, overview } = ctx;
  const language = declarationLanguage(ctx.language);
  const add = useWrite();
  const remove = useWrite();
  const affirm = useWrite();
  const [kind, setKind] = useState<PartnerKind>("domain");
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [pathPrefix, setPathPrefix] = useState("");
  const [confirmedNone, setConfirmedNone] = useState(false);

  return (
    <section aria-labelledby="rights-partners" className={SECTION}>
      <h2 id="rights-partners" className="font-display text-display-s text-ink">
        {t("rights.partners.heading")}
      </h2>
      <p className="mt-1 text-body-s text-ink-muted">{t("rights.partners.lead")}</p>
      {overview.partners.length === 0 ? (
        <p className="mt-3 text-body-s text-ink-muted">
          {overview.partners_none_affirmed_at
            ? t("rights.partners.noneAffirmed", {
                date: formatDate(overview.partners_none_affirmed_at, ctx.language),
              })
            : t("rights.partners.empty")}
        </p>
      ) : (
        <ul className="mt-3 flex flex-col gap-1 text-body-s text-ink">
          {overview.partners.map((partner) => (
            <li key={partner.id} className="flex flex-wrap items-center justify-between gap-2">
              <span>
                <span className="font-medium">{partner.name}</span> ·{" "}
                {t(`rights.partners.kinds.${partner.kind}`)}
                {partner.domain ? ` · ${partner.domain}${partner.path_prefix ?? ""}` : ""}
              </span>
              <button
                type="button"
                className={NEUTRAL}
                onClick={() =>
                  void remove.run(`/app/api/rights/partners/${partner.id}`, "DELETE")
                }
                disabled={remove.pending}
              >
                {t("rights.partners.remove", { name: partner.name })}
              </button>
            </li>
          ))}
        </ul>
      )}
      <WriteError t={t} write={remove} />
      <WritePending t={t} write={remove} />
      <form
        aria-label={t("rights.partners.add")}
        className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          void add
            .run("/app/api/rights/partners", "POST", {
              kind,
              name,
              domain,
              path_prefix: pathPrefix,
              note: "",
            })
            .then((ok) => {
              if (ok) {
                setName("");
                setDomain("");
                setPathPrefix("");
              }
            });
        }}
      >
        <label className={LABEL}>
          {t("rights.partners.kind")}
          <select
            className={INPUT}
            value={kind}
            onChange={(event) => setKind(event.target.value as PartnerKind)}
          >
            {PARTNER_KINDS.map((code) => (
              <option key={code} value={code}>
                {t(`rights.partners.kinds.${code}`)}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          {t("rights.partners.name")}
          <input
            className={INPUT}
            value={name}
            maxLength={200}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className={LABEL}>
          {t("rights.partners.domain")}
          <input
            className={INPUT}
            value={domain}
            maxLength={253}
            onChange={(event) => setDomain(event.target.value)}
          />
        </label>
        <label className={LABEL}>
          {t("rights.partners.pathPrefix")}
          <input
            className={INPUT}
            value={pathPrefix}
            maxLength={500}
            onChange={(event) => setPathPrefix(event.target.value)}
          />
        </label>
        <div className="sm:col-span-2">
          <button type="submit" className={PRIMARY} disabled={!name.trim() || add.pending}>
            {t("rights.partners.add")}
          </button>
          <WriteError t={t} write={add} />
          <WritePending t={t} write={add} />
        </div>
      </form>
      {overview.partners.length === 0 && !overview.partners_none_affirmed_at ? (
        <form
          aria-label={t("rights.partners.affirmNone")}
          className="mt-4 flex flex-col gap-3 border-t border-hairline pt-3"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            void affirm.run("/app/api/rights/partners/none", "POST", {
              declaration_version: overview.partner_declaration.version,
              language,
              confirmed: confirmedNone,
            });
          }}
        >
          <label className="flex items-start gap-2 text-body-s text-ink">
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
            <button type="submit" className={PRIMARY} disabled={!confirmedNone || affirm.pending}>
              {t("rights.partners.affirmNone")}
            </button>
          </div>
          <WriteError t={t} write={affirm} />
          <WritePending t={t} write={affirm} />
        </form>
      ) : null}
    </section>
  );
}

function ProofsSection({ ctx }: { ctx: Ctx }) {
  const { t, language, overview } = ctx;
  const upload = useWrite();
  const [kind, setKind] = useState<ProofKind>("contract_excerpt");
  const [originalId, setOriginalId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [formKey, setFormKey] = useState(0);
  const titles = new Map(overview.works.map((work) => [work.original_id, work.title]));

  return (
    <section aria-labelledby="rights-proofs" className={SECTION}>
      <h2 id="rights-proofs" className="font-display text-display-s text-ink">
        {t("rights.proofs.heading")}
      </h2>
      <p className="mt-1 text-body-s text-ink-muted">
        {t("rights.proofs.lead", {
          size: formatBytes(overview.proof_upload.max_bytes, language),
        })}
      </p>
      {overview.proofs.length === 0 ? (
        <p className="mt-3 text-body-s text-ink-muted">{t("rights.proofs.empty")}</p>
      ) : (
        <ul className="mt-3 flex flex-col gap-1 text-body-s text-ink">
          {overview.proofs.map((proof) => (
            <li key={proof.id}>
              <span className="font-mono text-mono break-all text-ink">{proof.filename}</span> ·{" "}
              {t(`rights.proofs.kinds.${proof.kind}`)}
              {proof.original_id !== null && titles.has(proof.original_id)
                ? ` · ${titles.get(proof.original_id) ?? ""}`
                : ""}
              {" · "}
              {t("rights.proofs.uploadedAt", { date: formatDate(proof.uploaded_at, language) })}
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
            void upload
              .run("/app/api/rights/proofs", "POST", proofForm(file, kind, originalId || null, ""))
              .then((ok) => {
                if (ok) {
                  setFile(null);
                  setFormKey((value) => value + 1);
                }
              });
          }}
        >
          <label className={LABEL}>
            {t("rights.proofs.kind")}
            <select
              className={INPUT}
              value={kind}
              onChange={(event) => setKind(event.target.value as ProofKind)}
            >
              {PROOF_KINDS.map((code) => (
                <option key={code} value={code}>
                  {t(`rights.proofs.kinds.${code}`)}
                </option>
              ))}
            </select>
          </label>
          <label className={LABEL}>
            {t("rights.proofs.original")}
            <select
              className={INPUT}
              value={originalId}
              onChange={(event) => setOriginalId(event.target.value)}
            >
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
            <button type="submit" className={PRIMARY} disabled={!file || upload.pending}>
              {upload.pending ? t("rights.proofs.uploading") : t("rights.proofs.upload")}
            </button>
            <WriteError t={t} write={upload} />
          </div>
        </form>
      ) : (
        <p className="mt-3 text-body-s text-ink-muted">{t("rights.proofs.unavailable")}</p>
      )}
    </section>
  );
}
