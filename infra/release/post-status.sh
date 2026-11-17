#!/usr/bin/env bash
# Post a GitHub commit status for one of the promotion gates (docs/runbooks/release.md).
#
# The promotion PR from `development` into `production` may only merge once three contexts are
# green — `check`, `security-review`, `legal-review` — plus the operator's own approving review.
# Two of those three are produced by agents, and an agent cannot be a GitHub reviewer, so each
# posts its verdict here instead and the branch ruleset (infra/release/protect.sh) requires the
# context. A veto is a `failure` status: it blocks the merge until a *fresh* review of the same
# context posts `success` on the same commit.
#
# Usage:
#   post-status.sh --context check --state success [--description TEXT] [--sha SHA]
#                  [--target-url URL] [--repo OWNER/REPO] [--soft] [--dry-run]
#
#   --context      the status context, i.e. the gate: check | security-review | legal-review
#   --state        success | failure | pending | error
#   --description  one line shown next to the status in the PR (GitHub truncates at 140 chars).
#                  It is public to anyone who can read the repository: name identifiers, never
#                  content (CLAUDE.md "Findings name identifiers, never content") — a file:line,
#                  a page id, a rule name, a count. Never a URL from a crawled site, never a
#                  pirated title, never a secret's value.
#   --sha          the commit to attach the status to (default: HEAD of this checkout)
#   --target-url   a link the status points at (a run log, a report comment); optional
#   --repo         OWNER/REPO (default: whatever `gh` resolves for this checkout)
#   --soft         never fail the caller: a missing `gh`, no network, an unknown commit or any
#                  other error is reported on stderr and the script still exits 0. `make
#                  check-posted POST_STATUS=1` (and `remote-check`) uses this, so a check result
#                  is never lost to a bookkeeping problem. It does NOT cover posting against a
#                  commit GitHub has never seen (2026-09-20: the release branch must be pushed
#                  before the status is posted, or the post silently no-ops under --soft and has
#                  to be re-run by hand once the sha is public — see docs/runbooks/release.md
#                  "State, 2026-09-20").
#   --dry-run      print what would be posted and exit without calling GitHub.
#
# Needs `gh` (authenticated) with `repo` scope. Makes exactly one network call.
set -euo pipefail

CONTEXT=""
STATE=""
DESCRIPTION=""
SHA=""
TARGET_URL=""
REPO=""
SOFT=0
DRY_RUN=0

# `gh` is not on every PATH here: the operator keeps it in ~/.local/bin.
GH="${GH:-}"
if [ -z "$GH" ]; then
  if command -v gh >/dev/null 2>&1; then
    GH="$(command -v gh)"
  elif [ -x "$HOME/.local/bin/gh" ]; then
    GH="$HOME/.local/bin/gh"
  else
    GH="gh"
  fi
fi

die() {
  # With --soft a problem is a warning: the caller (a check run, a review) keeps its own exit code.
  echo "post-status.sh: $*" >&2
  if [ "$SOFT" -eq 1 ]; then
    echo "post-status.sh: --soft, so not failing the caller" >&2
    exit 0
  fi
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --context) CONTEXT="${2:?--context needs a value}"; shift 2 ;;
    --state) STATE="${2:?--state needs a value}"; shift 2 ;;
    --description) DESCRIPTION="${2:?--description needs a value}"; shift 2 ;;
    --sha) SHA="${2:?--sha needs a value}"; shift 2 ;;
    --target-url) TARGET_URL="${2:?--target-url needs a value}"; shift 2 ;;
    --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
    --soft) SOFT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "post-status.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$CONTEXT" ] || die "--context is required (check | security-review | legal-review)"
case "$STATE" in
  success|failure|pending|error) ;;
  "") die "--state is required (success | failure | pending | error)" ;;
  *) die "--state must be success, failure, pending or error, not '$STATE'" ;;
esac

if [ -z "$SHA" ]; then
  SHA="$(git rev-parse HEAD 2>/dev/null || true)"
  [ -n "$SHA" ] || die "not a git repository and no --sha given; nothing to attach a status to"
fi

if [ -z "$REPO" ]; then
  REPO="$("$GH" repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
  [ -n "$REPO" ] || die "could not determine the repository (no gh, not authenticated, or no remote); pass --repo OWNER/REPO"
fi

# GitHub truncates a longer description anyway; cutting it here keeps the request honest.
if [ "${#DESCRIPTION}" -gt 140 ]; then
  DESCRIPTION="${DESCRIPTION:0:137}..."
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "post-status.sh: would POST repos/$REPO/statuses/$SHA"
  echo "  state=$STATE context=$CONTEXT"
  echo "  description=${DESCRIPTION:-<none>}"
  echo "  target_url=${TARGET_URL:-<none>}"
  exit 0
fi

args=(api --method POST "repos/$REPO/statuses/$SHA"
  -f "state=$STATE" -f "context=$CONTEXT" -f "description=$DESCRIPTION")
# An `[ … ] && args+=(…)` one-liner would exit the script under `set -e` when the test is false.
if [ -n "$TARGET_URL" ]; then
  args+=(-f "target_url=$TARGET_URL")
fi

if out="$("$GH" "${args[@]}" --jq '.state + " " + .context' 2>&1)"; then
  echo "post-status.sh: $REPO@${SHA:0:8} $out"
else
  die "posting the status failed: $out"
fi
