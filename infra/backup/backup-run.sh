#!/usr/bin/env bash
# Cron entrypoint (issue #39): dump the database, verify it, rotate, push an encrypted copy
# off-host — and mail the operator when any of that fails. A backup that fails silently is worse
# than no backup, because it buys confidence nobody earned. Rides along in the same run: a curated
# snapshot of the operator's ~/.claude (memory notes + self-written config, never credentials —
# docs/runbooks/claude-home-restore.md), pushed off-host under its own tag, and the deletion journal
# (ADR 0057) under a third tag — the record of what a restore of that dump would undo.
#
#   infra/backup/backup-run.sh             # dump + verify + rotate + off-host push, both kinds
#   infra/backup/backup-run.sh --no-push   # local dump + local claude-home snapshot only
#
# Env: everything pg-backup.sh, claude-home-backup.sh and restic-push.sh read, plus
#      BACKUP_ALERT_TO/BACKUP_ALERT_FROM (falling back to SAFETY_ALERT_TO/FROM) for the failure
#      mail, NO_ALERT=1 to suppress it, and BACKUP_ALERT_CMD to send it some other way.
#      See docs/runbooks/backup-restore.md and docs/runbooks/claude-home-restore.md.
#
# The off-host push is skipped with a WARN, not a failure, while RESTIC_REPOSITORY is unset: the
# operator turns it on once the Storage Box exists, and until then the local backups still run.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/backup/common.sh
source "$SCRIPT_DIR/common.sh"

usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

push=1
case "${1:-}" in
  "") ;;
  --no-push) push=0 ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [[ "$REPO_DIR" == */.claude/worktrees/* && "${FORCE:-}" != 1 ]]; then
  echo "refusing: $REPO_DIR is an agent worktree; run from the main checkout (FORCE=1 overrides)" >&2
  exit 1
fi

log="$(mktemp)"
trap 'rm -f -- "$log"' EXIT

failed=()

run_step() {
  local name="$1"
  shift
  echo "$(ts) --- $name" | tee -a "$log"
  if "$@" 2>&1 | tee -a "$log"; then
    return 0
  fi
  failed+=("$name")
  return 1
}

if ((push)) && ! restic_configured; then
  echo "$(ts) WARN off-host push skipped: RESTIC_REPOSITORY is not set, backups exist only on this machine (docs/runbooks/backup-restore.md)" | tee -a "$log"
  push=0
fi

if run_step "dump" "$SCRIPT_DIR/pg-backup.sh"; then
  if ((push)); then
    run_step "off-host push" "$SCRIPT_DIR/restic-push.sh" || true
  fi
else
  echo "$(ts) WARN off-host push skipped: there is no new dump to push" | tee -a "$log"
fi

# The curated ~/.claude snapshot (issue: survivable agent memory) is independent of the database
# dump: a failure in one must not hide, or be hidden by, the other. See
# docs/runbooks/claude-home-restore.md for what is and is not in it.
if run_step "claude-home snapshot" "$SCRIPT_DIR/claude-home-backup.sh"; then
  if ((push)); then
    claude_tar="$BACKUP_DIR/$(list_claude_snapshots | tail -n 1)"
    MAX_DUMP_MB="${CLAUDE_HOME_MAX_MB:-300}" \
      run_step "claude-home push" "$SCRIPT_DIR/restic-push.sh" "$claude_tar" marceline-claude-home ||
      true
  fi
else
  echo "$(ts) WARN claude-home push skipped: there is no new snapshot to push" | tee -a "$log"
fi

# The deletion journal (ADR 0057, DPIA R24, issue #137). Independent of the dump on purpose: it is
# the record of what the dump would *undo*, so it must travel even on a night the dump fails — and
# above all it must exist off-host when "the machine is gone" (docs/runbooks/backup-restore.md § D).
# It is tiny (one line per deleted row), so it is pushed whole every night rather than incrementally.
if ((push)); then
  if journal_configured; then
    # A journal nothing has been written to yet is a healthy state, not a failed push (the first
    # night after switching it on, 2026-09-19): restic refuses an empty directory, so the directory
    # carries a marker. `reapply_deletions` reads `*.jsonl` only and never sees it.
    [ -e "$DELETION_JOURNAL_DIR/CREATED" ] ||
      date -u +"journal directory created %Y-%m-%dT%H:%M:%SZ (ADR 0057)" >"$DELETION_JOURNAL_DIR/CREATED"
    MAX_DUMP_MB="${DELETION_JOURNAL_MAX_MB:-64}" \
      run_step "deletion journal push" "$SCRIPT_DIR/restic-push.sh" \
      "$DELETION_JOURNAL_DIR" "$DELETION_JOURNAL_TAG" || true
  else
    echo "$(ts) WARN deletion journal not pushed: DELETION_JOURNAL_DIR is unset or missing, so no restore can be repaired (ADR 0057, docs/runbooks/backup-restore.md)" | tee -a "$log"
  fi
fi

if ((${#failed[@]} > 0)); then
  steps="${failed[*]}"
  if [[ "${NO_ALERT:-}" != 1 ]]; then
    alert "run FAILED on $(hostname -s): $steps" "$(cat "$log")"
  fi
  echo "$(ts) FAIL backup-run steps=$steps"
  exit 1
fi

stamp_write last-run
echo "$(ts) OK backup-run offhost=$((push)) host=$(hostname -s)"
