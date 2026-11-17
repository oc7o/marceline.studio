<!--
Promotion PR: a release branch -> production. docs/runbooks/release.md.

Use this template only for a promotion. Nothing else merges into `production`. The head is always
a FROZEN `release/YYYY-MM-DD` branch (docs/runbooks/release.md, "Cutting a release branch") — never
`development`'s moving tip, and never a branch anyone commits to after it is cut.

Cut one with `infra/release/cut.sh` (it refuses until the commit's `check` status is already
green); it prints the branch name it created. Take the head from that, not from a hardcoded
`main`/`development`:
  RELEASE_BRANCH=release/2026-09-20   # exactly what cut.sh just pushed
  : "${RELEASE_BRANCH:?set RELEASE_BRANCH to the branch infra/release/cut.sh just pushed}"
  gh pr create --base production --head "$RELEASE_BRANCH" \
    --title "Promote $RELEASE_BRANCH to production: <what changed, in a few words>" \
    --body-file .github/PULL_REQUEST_TEMPLATE/promotion.md
or, in the browser, add ?template=promotion.md to the compare URL and set the head branch by hand.

"development" elsewhere in this template means `main` until the operator renames it
(docs/runbooks/release.md, the "State" note).

Nothing in this description may name content: no piracy URL, no pirated title, no cast name read
off a pirate page, no detail of one safety-flagged page, no secret's value. Identifiers only —
a page id, a site's role, a rule name, a file:line. (CLAUDE.md, "Findings name identifiers,
never content".)
-->

## What is being promoted

<!-- Two or three sentences, for the operator in six months. What changed, and why now. -->

**Review scope:** <!-- "surface" (the diff is large enough that the two reviewer agents and the
     operator read it for shape and risk, not line by line — say this plainly rather than implying
     a line review that didn't happen) or "line" (small enough to read in full). The rehearsal's
     first promotion was surface-only; docs/runbooks/release.md records why. -->

**What a customer will notice:**

**What would have to be rolled back if this is wrong:**
<!-- The rollback unit is the image tag (MARCELINE_RELEASE = this release branch's head sha,
     docs/runbooks/release.md "The release tag and the image") — set it back and
     `docker compose up -d --no-build` (docs/runbooks/production.md §9), unless a migration below
     is not simply reversible. -->

**Migrations in this diff, and which (if any) make rollback non-trivial:**
<!-- none / app_00xx names. For each: reversible by `migrate app <previous>` alone, or does
     rolling back the image while the migration stays applied break something (a column the old
     code doesn't know about, a data migration that isn't its own inverse)? Name those explicitly
     — this is the thing a rollback five months from now needs and the diff alone won't say. -->

**New settings or environment variables production needs:** <!-- none, or name them (never values) -->

---

## The four gates

All four must be true before merging. The first three are commit statuses on this PR's head commit
and the branch ruleset requires them; the fourth is the operator.

- [ ] **`check`** — a green full check on this exact head commit.
      `make check-posted POST_STATUS=1` (docs/runbooks/check-runner.md). This is the definition of
      done from CLAUDE.md, run on the whole promoted tree: lint, mypy strict, import-linter, the
      migration graph, the test suite, OpenAPI drift, both frontends and the customer app's build.

- [ ] **`security-review`** — the `security-reviewer` agent worked through
      `docs/reviews/security-checklist.md` and posted `success`. It asks whether this diff can put
      data, a credential or a request somewhere it does not belong: staff surfaces never public, no
      CORS headers ever, CSRF on every write, a foreign object is a byte-identical 404, secrets only
      in the environment, no media stored anywhere, robots.txt for pages *and* media hosts, no
      login/CAPTCHA/paywall/age-verification bypass, production's only credential still a scoped
      read-only export token, customer writes only through the outbox, no external resource
      auto-loaded in any UI, and nothing new that puts a URL or personal data into a log or a Sentry
      event.

- [ ] **`legal-review`** — the `legal-reviewer` agent worked through
      `docs/reviews/legal-checklist.md` and posted `success`. It asks whether this diff stays inside
      the positions already recorded: the customer is the sender and we never assert infringement
      (ADR 0035), the rights gate stays formal-completeness, the
      customer wording rules enforced by `frontend/src/i18n/wording.test.ts`, suppression (ADR
      0038), the safety gate and its retention, and GDPR minimisation. It
      does not give legal advice: a new legal question is escalated to counsel, not decided here.

- [ ] **The operator's approval** — read both reports and the diff, and approve on GitHub. An
      approval is dismissed automatically when new commits are pushed.

**Review reports go in the comments of this PR.** A status carries 140 characters; the report is
what gets read, and it is stored nowhere else. Each reviewer agent writes its report to a file and
says the path; paste it in verbatim with `gh pr comment <PR> --body-file <path>` — never retype or
summarise it by hand (2026-09-20 lesson, docs/runbooks/release.md "State, 2026-09-20").

**A veto is a `failure` status.** It blocks the merge until a *fresh* review of the same context
posts `success` on the same commit — a fix means a new head commit and a re-run of the whole
checklist, not a flipped status. docs/runbooks/release.md, "Reading a veto" and "Re-running a
review".

---

## Merge and deploy

- [ ] Merge commit (not squash — a squash flattens the release branch's history and the next
      promotion diff would show everything as changed).
- [ ] Deploy per **docs/runbooks/production.md §7** (and §11 if the customer app's
      `CUSTOMER_APP` variable changes). Remember: `docker compose restart` re-reads the Caddyfile
      but **not** `.env` — use `up -d --no-build <service>`.
- [ ] Rollback plan understood: **docs/runbooks/production.md §9** for the deploy,
      docs/runbooks/release.md "Rolling back a production merge" if the merge itself must come out.
