#!/usr/bin/env bash
# Cut a release branch for a promotion (docs/runbooks/release.md).
#
# A promotion is reviewed and merged from a frozen `release/YYYY-MM-DD` branch, never from the
# moving tip of `development` (operator decision, 2026-09-18 — during the rehearsal `main` moved 8
# commits while the three gates ran, so every status but the last was checked against a diff that
# no longer existed by the time it was read). This script does the cutting, and nothing else:
#
#   1. verifies the chosen commit is on `development` (currently `main`, see docs/runbooks/
#      release.md "State");
#   2. verifies its `check` commit status is already `success` — it never runs the check itself,
#      only reads what `make check-posted POST_STATUS=1` already posted;
#   3. picks a branch name (`release/YYYY-MM-DD`, `-2`, `-3`, … if today's is already taken);
#   4. with --apply, creates and pushes that branch, then prints the `gh pr create` line for the
#      promotion PR (release/YYYY-MM-DD -> production). It never opens the PR itself.
#
# Usage:
#   cut.sh                          # dry run against the tip of origin/main (development); prints
#                                    # what it would do, changes nothing
#   cut.sh --commit <sha>           # dry run against a specific commit
#   cut.sh --commit <sha> --apply   # create + push the branch, print the PR command
#   cut.sh --date YYYY-MM-DD ...    # override today's date (a cut just after UTC midnight, or a
#                                    # test run)
#   cut.sh --repo OWNER/REPO --dev-branch main ...
#
# Refuses a dirty working tree: the branch it creates must be exactly the named commit, not
# whatever a half-finished edit in this checkout happens to add. Never touches `production` —
# not a push, not a checkout, not a read past resolving `origin/production` for context — and
# never commits anything to the release branch it creates (nobody commits to a release branch;
# a fix goes to `development` and a *new* release branch is cut for it).
#
# Needs `gh` (authenticated) with `repo` scope, for the one read of the commit's `check` status.
set -euo pipefail

REPO=""
DEV_BRANCH="main"          # "development" is still called `main`; docs/runbooks/release.md "State"
COMMIT=""
DATE=""
APPLY=0

GH="${GH:-}"
if [ -z "$GH" ]; then
  if command -v gh >/dev/null 2>&1; then GH="$(command -v gh)"
  elif [ -x "$HOME/.local/bin/gh" ]; then GH="$HOME/.local/bin/gh"
  else GH="gh"; fi
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="${2:?--commit needs a value}"; shift 2 ;;
    --date) DATE="${2:?--date needs a value}"; shift 2 ;;
    --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
    --dev-branch) DEV_BRANCH="${2:?--dev-branch needs a value}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "cut.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -n "$(git status --porcelain 2>&1)" ]; then
  echo "cut.sh: working tree is dirty. Commit, discard or set your changes aside first — this" >&2
  echo "        script cuts a branch from a named commit, never from whatever is checked out" >&2
  echo "        plus local edits." >&2
  exit 1
fi

if [ -z "$REPO" ]; then
  REPO="$("$GH" repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
  [ -n "$REPO" ] || { echo "cut.sh: could not determine the repository; pass --repo OWNER/REPO" >&2; exit 1; }
fi

echo "cut.sh: repository $REPO, development branch '$DEV_BRANCH'"

git fetch origin --quiet "$DEV_BRANCH" || { echo "cut.sh: could not fetch origin/$DEV_BRANCH" >&2; exit 1; }

if [ -z "$COMMIT" ]; then
  COMMIT="$(git rev-parse "origin/$DEV_BRANCH")"
  echo "cut.sh: no --commit given; using the current tip of origin/$DEV_BRANCH: $COMMIT"
fi

FULL_SHA="$(git rev-parse --verify "${COMMIT}^{commit}" 2>/dev/null || true)"
if [ -z "$FULL_SHA" ]; then
  echo "cut.sh: '$COMMIT' does not resolve to a commit in this checkout (fetch it first)" >&2
  exit 1
fi
SHORT_SHA="$(git rev-parse --short "$FULL_SHA")"

# 1. the commit must actually be on development.
if ! git merge-base --is-ancestor "$FULL_SHA" "origin/$DEV_BRANCH"; then
  echo "cut.sh: $FULL_SHA is not an ancestor of origin/$DEV_BRANCH — refusing to cut a release" >&2
  echo "        branch from a commit that is not on development." >&2
  exit 1
fi
echo "cut.sh: $FULL_SHA is on origin/$DEV_BRANCH"

# 2. its `check` status must already be success. This script never runs the check itself.
CHECK_STATE="$("$GH" api "repos/$REPO/commits/$FULL_SHA/status" \
  --jq '.statuses[] | select(.context=="check") | .state' 2>/dev/null | head -n1 || true)"

if [ "$CHECK_STATE" != "success" ]; then
  echo >&2
  echo "cut.sh: no green 'check' status on $SHORT_SHA (found: '${CHECK_STATE:-none}')." >&2
  echo "        Run this first, from a checkout AT THAT EXACT COMMIT (the status attaches to" >&2
  echo "        whatever 'git rev-parse HEAD' is when it posts):" >&2
  echo >&2
  echo "          git checkout $FULL_SHA   # or a worktree at that commit" >&2
  echo "          make check-posted POST_STATUS=1" >&2
  echo >&2
  echo "        Then re-run: cut.sh --commit $FULL_SHA" >&2
  exit 1
fi
echo "cut.sh: check is success on $SHORT_SHA"

# 3. pick a branch name: release/YYYY-MM-DD, or -2, -3, ... if today's is taken.
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
CANDIDATE="release/$DATE"
SUFFIX=1
while git ls-remote --exit-code --heads origin "$CANDIDATE" >/dev/null 2>&1; do
  SUFFIX=$((SUFFIX + 1))
  CANDIDATE="release/$DATE-$SUFFIX"
  if [ "$SUFFIX" -gt 20 ]; then
    echo "cut.sh: more than 20 release branches for $DATE already exist — something is wrong," >&2
    echo "        pass --date to pick a different day or check origin/release/* by hand." >&2
    exit 1
  fi
done
echo "cut.sh: branch name: $CANDIDATE"

PR_TITLE="Promote $CANDIDATE to production: <what changed, in a few words>"
PR_CMD="gh pr create --base production --head $CANDIDATE --title \"$PR_TITLE\" --body-file .github/PULL_REQUEST_TEMPLATE/promotion.md"

if [ "$APPLY" -ne 1 ]; then
  cat <<EOF

Nothing was changed. With --apply this would:
  1. create local branch '$CANDIDATE' at $FULL_SHA
  2. push it: git push origin $CANDIDATE
  3. print the PR command below, unrun

Once pushed, open the promotion PR with:
  $PR_CMD

Re-run with --apply to actually cut it.
EOF
  exit 0
fi

echo
echo "cut.sh: creating and pushing $CANDIDATE..."
git branch "$CANDIDATE" "$FULL_SHA"
git push origin "$CANDIDATE"

cat <<EOF

cut.sh: $CANDIDATE is pushed, at $FULL_SHA (check: success). Nobody commits to it from here —
        a fix goes to $DEV_BRANCH and a new release branch is cut.

Open the promotion PR with:
  $PR_CMD
EOF
