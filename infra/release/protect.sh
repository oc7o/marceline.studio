#!/usr/bin/env bash
# Configure the GitHub branch ruleset(s) that guard the release process (docs/runbooks/release.md).
#
# Two independent groups of rulesets. Each is created/updated on its own; this script never
# touches a ruleset outside the group it was asked to act on.
#
# 1. Default: `production`. The four gates on a promotion PR from a release branch into
#    `production`:
#
#      1. `check`            — a green full check              (make check-posted POST_STATUS=1)
#      2. `security-review`  — the security reviewer's verdict (.claude/agents/security-reviewer.md)
#      3. `legal-review`     — the legal reviewer's verdict    (.claude/agents/legal-reviewer.md)
#      4. the operator's approving review                      (a human, on the PR)
#
#    1–3 are commit statuses because an agent cannot be a GitHub reviewer; this script is what
#    makes GitHub *require* them. Gate 4 is a `pull_request` rule's one approving review. Also: a
#    pull request is required at all (no direct pushes to `production`), force-pushes are refused,
#    and the branch cannot be deleted.
#
#    Two rulesets here, not one, because a bypass in GitHub covers a whole ruleset, never one rule
#    (issue #130):
#      "<name>"                   deletion, force-push, the three statuses — nobody bypasses it
#      "<name> (operator review)" the pull-request rule — a repository admin may bypass it, and
#                                  only through a pull request (`bypass_mode: pull_request`), never
#                                  by a direct push. GitHub does not count an author's approval of
#                                  their own PR, and this repository has one human: without the
#                                  bypass gate 4 can never be met. The operator merging the PR *is*
#                                  gate 4.
#
# 2. `--release`: "release branch freeze", on `refs/heads/release/*`. A release branch
#    (`infra/release/cut.sh`) is frozen the moment it is cut — nobody commits to it, a fix goes to
#    development and a new release branch is cut instead (docs/runbooks/release.md). This ruleset
#    has exactly one rule, `update`, with no bypass actor: it blocks every push that would add a
#    commit to an existing `release/*` branch, for anyone, including the repository owner — the
#    only way past it is to consciously edit or disable the ruleset first, which is the point.
#    Deliberately **not** included: `creation` (a release branch must still be created by
#    `cut.sh`/`git push`) and `deletion` (a release branch is deleted once its deploy is verified,
#    docs/runbooks/release.md "Cutting a release branch" — a normal, expected, one-per-promotion
#    action). All three rulesets, on a repository with one human account behind every push — the
#    operator's own credentials, whether typed by hand or run by an agent on their behalf — cannot
#    be told apart by GitHub: there is no bypass actor that would exempt "the operator, deciding
#    to fix a release branch directly" while still blocking "an agent's accidental push to one",
#    because both arrive as the same authenticated identity. A rule with a bypass would exempt
#    both; a rule with none blocks both equally, which is what `update` does here. Restricting
#    `deletion` the same way would block the *expected* cleanup exactly as hard as an accidental
#    one, with nothing to gain, so it stays a written rule instead of a technical one (see
#    docs/runbooks/release.md).
#
# Usage:
#   protect.sh                 # print what it would do for `production`; change nothing (default)
#   protect.sh --apply         # create or update the `production` rulesets (gates + review)
#   protect.sh --release       # print what it would do for `release/*`; change nothing
#   protect.sh --release --apply
#   protect.sh --repo OWNER/REPO --branch production --name "production promotion gates"
#
# Idempotent: it looks each ruleset up by name and PUTs the same payload if it already exists, so
# running it twice is a no-op. It never touches any other ruleset, and never touches a branch.
#
# Needs `gh` (authenticated) with `admin:repo` scope. Read-only without `--apply`, except for the
# GET(s) it needs to find an existing ruleset by name.
set -euo pipefail

REPO=""
BRANCH=""
NAME=""
APPLY=0
RELEASE=0

GH="${GH:-}"
if [ -z "$GH" ]; then
  if command -v gh >/dev/null 2>&1; then GH="$(command -v gh)"
  elif [ -x "$HOME/.local/bin/gh" ]; then GH="$HOME/.local/bin/gh"
  else GH="gh"; fi
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --release) RELEASE=1; shift ;;
    --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
    --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,55p' "$0"; exit 0 ;;
    *) echo "protect.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$RELEASE" -eq 1 ]; then
  BRANCH="${BRANCH:-release/*}"
  NAME="${NAME:-release branch freeze}"
else
  BRANCH="${BRANCH:-production}"
  NAME="${NAME:-production promotion gates}"
fi

if [ -z "$REPO" ]; then
  REPO="$("$GH" repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
  [ -n "$REPO" ] || { echo "protect.sh: could not determine the repository; pass --repo OWNER/REPO" >&2; exit 1; }
fi

if [ "$RELEASE" -eq 1 ]; then
  read -r -d '' PAYLOAD <<JSON || true
{
  "name": "$NAME",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/$BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "update" }
  ]
}
JSON
else
  REVIEW_NAME="$NAME (operator review)"

  # `strict_required_status_checks_policy` is deliberately false. It would require the PR branch to
  # be up to date with the base before merging; `production` only ever receives merge commits from
  # a release branch, so each merge leaves the release branch one commit behind and every promotion
  # would need a merge-back first. The three contexts are still required — they just do not have to
  # have run on a commit that already contains `production`'s head.
  read -r -d '' PAYLOAD_GATES <<JSON || true
{
  "name": "$NAME",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": { "ref_name": { "include": ["refs/heads/$BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "check" },
          { "context": "security-review" },
          { "context": "legal-review" }
        ]
      }
    }
  ]
}
JSON

  # actor_id 5 is GitHub's built-in repository role "admin".
  read -r -d '' PAYLOAD_REVIEW <<JSON || true
{
  "name": "$REVIEW_NAME",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request" }
  ],
  "conditions": { "ref_name": { "include": ["refs/heads/$BRANCH"], "exclude": [] } },
  "rules": [
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "allowed_merge_methods": ["merge"]
      }
    }
  ]
}
JSON
fi

echo "protect.sh: repository $REPO, branch '$BRANCH', ruleset '$NAME'"
echo

plan() { # name -> "METHOD endpoint"
  local id
  id="$("$GH" api "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$1\") | .id" 2>/dev/null || true)"
  if [ -n "$id" ]; then echo "PUT repos/$REPO/rulesets/$id"; else echo "POST repos/$REPO/rulesets"; fi
}

if [ "$RELEASE" -eq 1 ]; then
  read -r method endpoint <<<"$(plan "$NAME")"
  echo "'$NAME': $method $endpoint (PUT = updated in place, POST = created)"

  cat <<EOF

It would enforce, on '$BRANCH':
  - every push that would add a commit to an existing matching branch is refused (rule type
    'update'), for anyone, with no bypass actor — including the repository owner
  - ref creation is NOT restricted: 'infra/release/cut.sh' (or a plain 'git push') can still
    create a new release/YYYY-MM-DD branch
  - ref deletion is NOT restricted: deleting a release branch once its deploy is verified
    (docs/runbooks/release.md) stays an ordinary operation — see this script's header comment
    for why deletion is a written rule here, not a technical one

Request: $method $endpoint
Payload:
EOF
  echo "$PAYLOAD"
else
  read -r method_gates endpoint_gates <<<"$(plan "$NAME")"
  read -r method_review endpoint_review <<<"$(plan "$REVIEW_NAME")"
  echo "'$NAME': $method_gates $endpoint_gates (PUT = updated in place, POST = created)"
  echo "'$REVIEW_NAME': $method_review $endpoint_review"

  cat <<EOF

It would enforce, on '$BRANCH':
  - a pull request is required (no direct push), with 1 approving review,
    and an approval is dismissed when new commits are pushed; a repository admin
    may merge their own PR without that review (issue #130) — never push directly
  - three status checks must be green on the head commit:
      check            the full \`make check\` (make check-posted POST_STATUS=1)
      security-review  .claude/agents/security-reviewer.md
      legal-review     .claude/agents/legal-reviewer.md
  - force-push refused (non_fast_forward)
  - branch deletion refused
  - merge commits only (a squash would discard the release branch's own history)

Payloads:
EOF
  echo "$PAYLOAD_GATES"
  echo "$PAYLOAD_REVIEW"
fi

if [ "$APPLY" -ne 1 ]; then
  cat <<'EOF'

Nothing was changed. Re-run with --apply to create or update the ruleset(s).

Two things to know before you do:
  - the branch(es) it targets must exist first (or, for release/*, will exist later) — a
    ruleset whose condition matches no branch simply protects nothing yet.
  - Repository rulesets on a **private** repository need a paid plan (Pro, Team or
    Enterprise). On a free personal account the API answers 403 with an upgrade message.
    Until then the three statuses (production ruleset) are still posted and still visible on
    the PR — they are just advisory, and the operator enforces them by reading them before
    merging.
EOF
  exit 0
fi

echo
echo "protect.sh: applying..."
apply() { # method endpoint payload
  local out
  if out="$(printf '%s' "$3" | "$GH" api --method "$1" "$2" --input - \
      --jq '"ruleset \(.id) \"\(.name)\" is \(.enforcement) on \(.conditions.ref_name.include | join(", ")), bypass actors: \(.bypass_actors | length)"' 2>&1)"; then
    echo "protect.sh: $out"
  else
    echo "protect.sh: the request failed:" >&2
    echo "$out" >&2
    echo "protect.sh: a 403 mentioning an upgrade means rulesets are not available for a private" >&2
    echo "            repository on this plan; see the note printed by a dry run." >&2
    exit 1
  fi
}
if [ "$RELEASE" -eq 1 ]; then
  apply "$method" "$endpoint" "$PAYLOAD"
else
  # The review ruleset first: if it fails, the existing ruleset still holds the pull-request rule.
  apply "$method_review" "$endpoint_review" "$PAYLOAD_REVIEW"
  apply "$method_gates" "$endpoint_gates" "$PAYLOAD_GATES"
fi
