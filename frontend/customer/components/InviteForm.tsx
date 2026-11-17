"use client";
/**
 * The invite redemption's one island (issue #115, ADR 0052).
 *
 * One form per step, posted to this server's own route handlers, and after each success the page
 * is asked for again — the server is the only thing that knows which step is next, and the step is
 * derived from the row rather than held here (`accounts/invites.step_of`). That is also what makes
 * the flow resumable: reopening the link renders whatever the server says.
 *
 * Refusals are **codes**, never sentences: Django answers `{"problems": ["password_too_short"]}`
 * and the words live in the `onboarding` slice of `@shared/i18n`. A code with no word falls back
 * to the generic sentence rather than rendering the code to a person.
 */
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { csrfToken } from "../lib/csrf";
import type { InviteState } from "../lib/reads";
import { type Resource, translator } from "../lib/translate";

/**
 * The country's name in the reader's language, from the browser's own data — so 27 country names
 * cost this route nothing and need no translation of ours. The code itself if the browser has no
 * such table.
 */
function countryName(code: string, language: string): string {
  try {
    return new Intl.DisplayNames([language], { type: "region" }).of(code) ?? code;
  } catch {
    return code;
  }
}

/** Whether the server's answer to the country step says the pilot does not cover it. */
function saysOutsideEu(body: unknown): boolean {
  return (
    body !== null &&
    typeof body === "object" &&
    "outside_eu" in body &&
    (body as { outside_eu?: unknown }).outside_eu === true
  );
}

/**
 * Whether the last step was **accepted** rather than finished (ADR 0052 §9).
 *
 * On the public host the account is not created here: the redemption goes into the outbox, Django
 * answers 202 `{"pending": true}` and the browser is not signed in. So the end of the flow has two
 * honest endings, and this is the only thing that tells them apart. Never guessed from the host name
 * — the server says which it was in the answer it gives.
 */
function saysPending(body: unknown): boolean {
  return (
    body !== null &&
    typeof body === "object" &&
    "pending" in body &&
    (body as { pending?: unknown }).pending === true
  );
}

/**
 * The published document's address, or null. `http(s)` only — the value comes from
 * `ONBOARDING_DOCUMENTS`, which an operator sets, and a `javascript:` URL in a link a
 * customer clicks is the mistake the SPA's `safeHref` exists to prevent. The same rule in
 * four lines, rather than pulling a server module's whole file into this island.
 */
function safeDocumentHref(url: string): string | null {
  return /^https?:\/\//i.test(url) ? url : null;
}

/** The five writes of the redemption, in order. */
type Step = "password" | "declaration" | "documents" | "country" | "complete";

const KNOWN_PASSWORD_PROBLEMS = [
  "password_too_short",
  "password_too_common",
  "password_entirely_numeric",
  "password_too_similar",
  "password_invalid",
];

export function InviteForm({
  language,
  token,
  state,
  words,
}: {
  language: string;
  token: string;
  state: InviteState;
  words: Resource;
}) {
  const t = translator(words, language);
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [ticked, setTicked] = useState(false);
  // The majority and legal-capacity declaration's own box (issue #123). A second piece of state,
  // never the same one: two sentences are agreed to separately or the tick means nothing.
  const [ageTicked, setAgeTicked] = useState(false);
  const [country, setCountry] = useState("");
  const [pending, setPending] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  // The last step is the only one that does not come back from the server: the invite is used up,
  // so asking for it again would render the neutral page to the person who just finished it.
  const [finished, setFinished] = useState(false);
  // Accepted on the public host, where the account appears a minute or two later (ADR 0052 §9).
  const [pendingAccount, setPendingAccount] = useState(false);
  // Answered with a country the pilot does not cover. Held here because the *invite* keeps no
  // record of which country it was (ADR 0052 §5) — only that it happened, as a count.
  const [outside, setOutside] = useState(false);

  /**
   * Every BFF path this island can send to, written out literally.
   *
   * Not one template with the step interpolated: `lib/routes.test.ts` matches the paths an island
   * names against the route files that exist, and it can only see inside a string as far as the
   * static parts go. A computed step would make that check vacuous — and a typo in it a 404 the
   * customer reads as "something went wrong". (For the same reason this comment names no path:
   * the check cannot tell prose from a request.)
   */
  const paths: Record<Step, string> = {
    password: `/app/api/invite/${token}/password`,
    declaration: `/app/api/invite/${token}/declaration`,
    documents: `/app/api/invite/${token}/documents`,
    country: `/app/api/invite/${token}/country`,
    complete: `/app/api/invite/${token}/complete`,
  };

  const post = async (step: Step, body: unknown): Promise<{ ok: boolean; body: unknown }> => {
    setPending(true);
    setProblems([]);
    try {
      const response = await fetch(paths[step], {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() ?? "" },
        body: JSON.stringify(body),
      });
      const parsed: unknown = await response.json().catch(() => null);
      if (response.ok) return { ok: true, body: parsed };
      const listed =
        parsed && typeof parsed === "object" && "problems" in parsed && Array.isArray(parsed.problems)
          ? (parsed.problems as string[])
          : [];
      if (listed.length > 0) setProblems(listed);
      else if (response.status === 403) setProblems(["formExpired"]);
      else if (response.status === 429) setProblems(["rateLimited"]);
      // 503: the public host cannot pass the redemption on right now (the outbox is off, or
      // development has not pulled for hours). Nothing is lost and the link still works.
      else if (response.status === 503) setProblems(["unavailable"]);
      else setProblems(["generic"]);
      return { ok: false, body: parsed };
    } catch {
      setProblems(["generic"]);
      return { ok: false, body: null };
    } finally {
      setPending(false);
    }
  };

  /** Every step but the last: advance, then let the server render what comes next. */
  const advance = async (step: Step, body: unknown) => {
    if ((await post(step, body)).ok) router.refresh();
  };

  const submit = (step: Step, body: unknown) => (event: FormEvent) => {
    event.preventDefault();
    void advance(step, body);
  };

  const finish = (event: FormEvent) => {
    event.preventDefault();
    // Two submits in the same frame would be two redemptions attempted; the second one is refused
    // by the server (one item, one account) but the person would read its 404 as a failure.
    if (pending) return;
    void (async () => {
      const answered = await post("country", { country });
      if (!answered.ok) return;
      // The country answer decides, and the server says which it was in the same answer. Outside
      // the EU the redemption ends here: the invite is deliberately **not** consumed, so a typo on
      // this select box costs nobody their invitation (ADR 0052 §5).
      if (saysOutsideEu(answered.body)) {
        setOutside(true);
        return;
      }
      // Inside it, `complete` creates the account and signs this browser in — on development. On
      // the public host it is accepted into the outbox instead, and the account exists a minute or
      // two later, so that answer gets its own screen and no link into the app.
      const completed = await post("complete", {});
      if (!completed.ok) {
        router.refresh();
        return;
      }
      if (saysPending(completed.body)) {
        setPendingAccount(true);
        return;
      }
      setFinished(true);
    })();
  };

  const message = (code: string): string => {
    if (KNOWN_PASSWORD_PROBLEMS.includes(code)) {
      return t(`onboarding.password.errors.${code}`, { count: state.password_min_length });
    }
    if (code === "declaration_required") return t("onboarding.declaration.required");
    if (code === "age_declaration_required") return t("onboarding.declaration.ageRequired");
    if (code === "declaration_version_outdated") return t("onboarding.declaration.outdated");
    if (code === "all_documents_required") return t("onboarding.documents.allRequired");
    if (code === "documents_not_published") return t("onboarding.documents.notPublished");
    if (code === "country_required") return t("onboarding.country.required");
    if (code === "business_status_not_evidenced") return t("onboarding.errors.businessStatus");
    if (code === "unavailable" || code === "processing_paused" || code === "invite_write_unavailable")
      return t("onboarding.errors.unavailable");
    if (code === "formExpired") return t("onboarding.errors.formExpired");
    if (code === "rateLimited") return t("onboarding.errors.rateLimited");
    return t("onboarding.errors.generic");
  };

  const alerts =
    problems.length === 0 ? null : (
      <ul role="alert" className="mb-3 space-y-1 text-body-s text-critical-ink">
        {problems.map((code) => (
          <li key={code}>{message(code)}</li>
        ))}
      </ul>
    );

  const button = (label: string, busy: string) => (
    <button
      type="submit"
      disabled={pending}
      className="inline-flex w-full items-center justify-center gap-2 rounded-sm border border-brand bg-brand px-3 py-2 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-45"
    >
      {pending ? busy : label}
    </button>
  );

  if (pendingAccount) {
    return (
      <div>
        <h2 className="font-display text-display-s text-ink">{t("onboarding.pending.heading")}</h2>
        <p className="mt-2 text-body text-ink-muted">{t("onboarding.pending.body")}</p>
        {/* The same honest note as the finished screen: a tick converts nobody (issue #92). */}
        <div className="mt-5 rounded-sm border border-hairline bg-surface-200 p-4">
          <p className="text-body-s text-ink">{t("onboarding.checking.heading")}</p>
          <p className="mt-1 text-body-s text-ink-muted">{t("onboarding.checking.body")}</p>
        </div>
        {/* Sign-in, not the app: there is no session here, and following a link into /app would
            bounce the person off the session gate. Written out like the button below it. */}
        <a
          href="/app/sign-in"
          className="tap mt-5 w-full justify-center gap-1 rounded-sm border border-brand bg-brand px-3 py-1.5 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover sm:w-auto sm:justify-start"
        >
          {t("onboarding.pending.cta")}
        </a>
      </div>
    );
  }

  if (finished) {
    return (
      <div>
        <h2 className="font-display text-display-s text-ink">{t("onboarding.done.heading")}</h2>
        <p className="mt-2 text-body text-ink-muted">{t("onboarding.done.body")}</p>
        {/* Honest about what is not finished: a tick does not make anybody a business, so a person
            still looks at the account (issue #92). Never rendered as "everything is settled". */}
        <div className="mt-5 rounded-sm border border-hairline bg-surface-200 p-4">
          <p className="text-body-s text-ink">{t("onboarding.checking.heading")}</p>
          <p className="mt-1 text-body-s text-ink-muted">{t("onboarding.checking.body")}</p>
        </div>
        {/* `LinkButton` primary + `block` (issue #117), written out rather than imported: no
            island in this app imports `components/ui.tsx`, and doing so would pull the whole
            primitives module — Veil, StatTile, Pagination — into this route's client chunk. `.tap`
            is the 44 px floor on a finger-driven browser (globals.css). */}
        <a
          href="/app/originals/new"
          className="tap mt-5 w-full justify-center gap-1 rounded-sm border border-brand bg-brand px-3 py-1.5 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover sm:w-auto sm:justify-start"
        >
          {t("onboarding.done.cta")}
        </a>
        {/* ADR 0055: what comes after the first work, and only where that step exists — the flag is
            a fact about this deployment, so it is the same answer for every caller and tells nobody
            anything about their invite. */}
        {state.account_step_next ? (
          <div className="mt-5">
            <p className="text-body-s text-ink-muted">{t("onboarding.done.accountStep")}</p>
            <a
              href="/app/verification"
              className="tap mt-2 inline-block rounded-sm text-button text-brand-ink underline"
            >
              {t("onboarding.done.accountStepCta")}
            </a>
          </div>
        ) : null}
      </div>
    );
  }

  if (outside || state.outside_eu) {
    return (
      <div>
        <h2 className="font-display text-display-s text-ink">
          {t("onboarding.country.outside.heading")}
        </h2>
        <p className="mt-2 text-body text-ink-muted">{t("onboarding.country.outside.body")}</p>
      </div>
    );
  }

  if (state.step === "password") {
    return (
      <form onSubmit={submit("password", { password })}>
        <h2 className="font-display text-display-s text-ink">{t("onboarding.password.heading")}</h2>
        <p className="mt-2 mb-4 text-body text-ink-muted">{t("onboarding.password.lead")}</p>
        <label className="mb-2 block">
          <span className="mb-1 block text-label text-ink-muted">
            {t("onboarding.password.label")}
          </span>
          <input
            className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-body text-ink"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
            autoFocus
          />
        </label>
        <p className="mb-4 text-caption text-ink-subtle">
          {t("onboarding.password.requirement", { count: state.password_min_length })}
        </p>
        {alerts}
        {button(t("onboarding.password.submit"), t("onboarding.password.submitting"))}
      </form>
    );
  }

  if (state.step === "declaration") {
    // Both declarations travel with the version the server displayed, so a page left open across a
    // change to the wording is refused rather than signing text nobody is showing (issue #123).
    return (
      <form
        onSubmit={submit("declaration", {
          accepted: ticked,
          declaration_version: state.declaration_version,
          age_accepted: ageTicked,
          age_declaration_version: state.age_declaration_version,
        })}
      >
        <h2 className="font-display text-display-s text-ink">
          {t("onboarding.declaration.heading")}
        </h2>
        <p className="mt-2 mb-4 text-body text-ink-muted">{t("onboarding.declaration.lead")}</p>
        <blockquote
          lang={state.declaration_language}
          className="mb-2 border-l-2 border-hairline pl-4 text-body text-ink"
        >
          {state.declaration_text}
        </blockquote>
        <p className="mb-4 text-caption text-ink-subtle">
          {t("onboarding.declaration.version", { version: state.declaration_version })}
        </p>
        <label className="mb-6 flex items-start gap-2 text-body text-ink">
          <input
            type="checkbox"
            className="mt-1"
            checked={ticked}
            onChange={(event) => setTicked(event.target.checked)}
          />
          <span>{t("onboarding.declaration.tick")}</span>
        </label>
        <h3 className="text-label text-ink">{t("onboarding.declaration.ageHeading")}</h3>
        <p className="mt-1 mb-4 text-body text-ink-muted">{t("onboarding.declaration.ageLead")}</p>
        <blockquote
          lang={state.declaration_language}
          className="mb-2 border-l-2 border-hairline pl-4 text-body text-ink"
        >
          {state.age_declaration_text}
        </blockquote>
        <p className="mb-4 text-caption text-ink-subtle">
          {t("onboarding.declaration.version", { version: state.age_declaration_version })}
        </p>
        <label className="mb-4 flex items-start gap-2 text-body text-ink">
          <input
            type="checkbox"
            className="mt-1"
            checked={ageTicked}
            onChange={(event) => setAgeTicked(event.target.checked)}
          />
          <span>{t("onboarding.declaration.ageTick")}</span>
        </label>
        {alerts}
        {button(t("onboarding.declaration.submit"), t("onboarding.declaration.submitting"))}
      </form>
    );
  }

  if (state.step === "documents") {
    const ids = state.documents.map((doc) => doc.document_id);
    return (
      <form onSubmit={submit("documents", { accepted: ticked ? ids : [], language })}>
        <h2 className="font-display text-display-s text-ink">{t("onboarding.documents.heading")}</h2>
        <p className="mt-2 mb-4 text-body text-ink-muted">{t("onboarding.documents.lead")}</p>
        {state.documents_published ? (
          <ul className="mb-4 space-y-3">
            {state.documents.map((doc) => {
              const href = safeDocumentHref(doc.url);
              const name = t(`onboarding.documents.names.${doc.document_id}`);
              return (
                <li key={doc.document_id}>
                  {href === null ? (
                    <span className="text-body text-ink">{name}</span>
                  ) : (
                    <a
                      className="tap text-body text-brand underline"
                      href={href}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      {name} — {t("onboarding.documents.open")}
                    </a>
                  )}
                  <p className="text-caption text-ink-subtle">
                    {t("onboarding.documents.version", { version: doc.version })}
                  </p>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="mb-4 text-body text-ink-muted">{t("onboarding.documents.notPublished")}</p>
        )}
        {state.documents_published ? (
          <>
            <label className="mb-4 flex items-start gap-2 text-body text-ink">
              <input
                type="checkbox"
                className="mt-1"
                checked={ticked}
                onChange={(event) => setTicked(event.target.checked)}
              />
              <span>{t("onboarding.documents.tick")}</span>
            </label>
            {alerts}
            {button(t("onboarding.documents.submit"), t("onboarding.documents.submitting"))}
          </>
        ) : null}
      </form>
    );
  }

  return (
    <form onSubmit={finish}>
      <h2 className="font-display text-display-s text-ink">{t("onboarding.country.heading")}</h2>
      <p className="mt-2 mb-4 text-body text-ink-muted">{t("onboarding.country.lead")}</p>
      <label className="mb-4 block">
        <span className="mb-1 block text-label text-ink-muted">{t("onboarding.country.label")}</span>
        <select
          className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-body text-ink"
          value={country}
          onChange={(event) => setCountry(event.target.value)}
          required
        >
          <option value="">{t("onboarding.country.choose")}</option>
          {state.eu_countries.map((code) => (
            <option key={code} value={code}>
              {countryName(code, language)}
            </option>
          ))}
        </select>
      </label>
      {alerts}
      {button(t("onboarding.country.submit"), t("onboarding.country.submitting"))}
    </form>
  );
}
