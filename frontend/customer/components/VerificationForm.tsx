"use client";
/**
 * The two writes of the account-control step (ADR 0055): claim an account, and "I have placed it".
 *
 * An island for one reason: both are forms, and after either one the server has to be asked for the
 * page again (`router.refresh()`), because the code and the standing are the server's answer and not
 * this component's state. Everything a browser does not need — the heading, the standing, the code,
 * the list of claimed accounts — is rendered by `VerificationScreen` on the server.
 *
 * Its words arrive as the `verification` and `common` slices, so no resource file is ever a client
 * chunk. A refusal arrives as a **code** and the words are ours; anything unmapped reads one neutral
 * sentence rather than a code the creator cannot act on.
 *
 * On production a write answers 202 (ADR 0028 write path): the factual "received, it will show here
 * shortly", never a claim that anything happened.
 */
import { type FormEvent, useState } from "react";

import { ApiError } from "@shared/api/errors";

import type { ClaimedAccount, VerificationStatus } from "../lib/reads";
import { type Resource, type Translate, translator } from "../lib/translate";
import { useWrite } from "../lib/write";

const INPUT =
  "mt-1 block w-full rounded-sm border border-border-control bg-surface-100 px-2 py-1.5 text-body-s text-ink";
const LABEL = "block text-label text-ink";
const BUTTON =
  "tap inline-flex items-center gap-2 rounded-sm border px-3 py-1.5 text-button transition-colors duration-120 ease-standard disabled:cursor-not-allowed disabled:opacity-45";
const PRIMARY = `${BUTTON} border-brand bg-brand text-on-brand hover:bg-brand-hover`;
const NEUTRAL = `${BUTTON} border-border-control bg-surface-100 text-ink hover:bg-surface-300`;

/** Refusal codes with a sentence of their own; anything else reads `verification.errors.generic`. */
const CODES = [
  "invalid_handle",
  "host_not_allowed",
  "url_not_accepted_for_platform",
  "unknown_platform",
  "code_expired",
  "not_open",
  "video_call_required",
  "account_verification_unavailable",
] as const;

function errorKey(error: unknown): string {
  if (error instanceof ApiError && error.status !== 404) {
    const code = error.message;
    if ((CODES as readonly string[]).includes(code)) return `verification.errors.${code}`;
  }
  return "verification.errors.generic";
}

/** A 202 from the public host: accepted into the outbox, not applied yet. */
function isPending(answer: unknown): boolean {
  return (
    typeof answer === "object" &&
    answer !== null &&
    "pending" in answer &&
    (answer as { pending?: unknown }).pending === true
  );
}

/** States in which the creator still has something to do with the code. */
const OPEN = new Set<ClaimedAccount["state"]>(["pending", "placed"]);

export function VerificationForm({
  language,
  words,
  status,
}: {
  language: string;
  words: Resource;
  status: VerificationStatus;
}) {
  const t = translator(words, language);
  const claimed = status.accounts.length > 0;
  return (
    <div className="flex flex-col gap-4">
      {status.accounts
        // A claim with no code has nothing to have placed (the video-call platforms), so it gets no
        // button — the service refuses it too, and a button that always fails is worse than none.
        .filter((account) => OPEN.has(account.state) && account.code !== null)
        .map((account) => (
          <PlacedButton key={account.id} t={t} account={account} />
        ))}
      <ClaimForm t={t} status={status} again={claimed} />
    </div>
  );
}

function ClaimForm({
  t,
  status,
  again,
}: {
  t: Translate;
  status: VerificationStatus;
  again: boolean;
}) {
  const write = useWrite();
  const [platform, setPlatform] = useState("");
  const [handle, setHandle] = useState("");
  const [url, setUrl] = useState("");
  const chosen = status.platforms.find((entry) => entry.code === platform);
  const hosts = chosen?.hosts ?? [];
  // ADR 0055 (operator review 2026-09-19): no platform identity check stands behind these, so a
  // code could not establish anything and none is issued — the screen says so before they submit.
  const videoCallOnly = chosen !== undefined && !chosen.code_accepted;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const sent = await write.run("/app/api/verification/accounts", "POST", {
      platform,
      handle,
      profile_url: url,
    });
    if (sent) {
      setHandle("");
      setUrl("");
    }
  }

  return (
    <form onSubmit={submit} className="rounded-md border border-hairline bg-surface-100 p-4">
      <h2 className="text-label text-ink-muted">{t("verification.claim.heading")}</h2>
      <div className="mt-2 flex flex-col gap-3 sm:max-w-md">
        <div>
          <label className={LABEL} htmlFor="verification-platform">
            {t("verification.claim.platform")}
          </label>
          <select
            id="verification-platform"
            className={INPUT}
            value={platform}
            onChange={(event) => setPlatform(event.target.value)}
            required
          >
            <option value="">{t("verification.claim.platformPlaceholder")}</option>
            {status.platforms.map((entry) => (
              <option key={entry.code} value={entry.code}>
                {entry.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className={LABEL} htmlFor="verification-handle">
            {t("verification.claim.handle")}
          </label>
          <input
            id="verification-handle"
            className={INPUT}
            value={handle}
            onChange={(event) => setHandle(event.target.value)}
            maxLength={64}
            required
          />
          <p className="mt-1 text-caption text-ink-subtle">{t("verification.claim.handleHelp")}</p>
        </div>
        {videoCallOnly ? (
          <p className="max-w-[68ch] text-body-s text-ink">{t("verification.claim.videoCallOnly")}</p>
        ) : null}
        {hosts.length > 0 ? (
          <div>
            <label className={LABEL} htmlFor="verification-url">
              {t("verification.claim.url")}
            </label>
            <input
              id="verification-url"
              className={INPUT}
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              maxLength={300}
              inputMode="url"
            />
            <p className="mt-1 text-caption text-ink-subtle">{t("verification.claim.urlHelp")}</p>
          </div>
        ) : null}
      </div>
      <button type="submit" className={`${PRIMARY} mt-3`} disabled={write.pending}>
        {write.pending
          ? t("verification.claim.submitting")
          : again
            ? t("verification.claim.again")
            : t("verification.claim.submit")}
      </button>
      <Answer t={t} write={write} pendingKey="verification.claim.pending" />
    </form>
  );
}

function PlacedButton({ t, account }: { t: Translate; account: ClaimedAccount }) {
  const write = useWrite();
  return (
    <div>
      <button
        type="button"
        className={NEUTRAL}
        disabled={write.pending || account.state === "placed"}
        onClick={() => {
          void write.run(`/app/api/verification/accounts/${account.id}/placed`, "POST");
        }}
      >
        {write.pending ? t("verification.code.placing") : t("verification.code.placed")}
      </button>
      <Answer t={t} write={write} pendingKey="verification.claim.pending" />
    </div>
  );
}

function Answer({
  t,
  write,
  pendingKey,
}: {
  t: Translate;
  write: ReturnType<typeof useWrite>;
  pendingKey: string;
}) {
  if (write.error) {
    return (
      <p role="alert" className="mt-2 text-body-s text-critical-ink">
        {t(errorKey(write.error))}
      </p>
    );
  }
  if (isPending(write.data)) {
    return (
      <p role="status" className="mt-2 text-body-s text-ink-muted">
        {t(pendingKey)}
      </p>
    );
  }
  return null;
}
