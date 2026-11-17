#!/usr/bin/env bash
# The watchdog (issue #39): alert when a backup has not *happened*, not only when one failed.
# A cron entry that stops firing — commented out, a host renamed, a crontab lost on reinstall —
# produces no failing run and no FAIL line to read. The only thing that catches it is asking
# "when did each of these last succeed", from a separate timer.
#
#   infra/backup/backup-check.sh          # check ages, alert (throttled) and exit non-zero if overdue
#   infra/backup/backup-check.sh --quiet  # same, but print only problems
#
# Checked:
#   local dump         newest marceline-*.dump in $BACKUP_DIR       BACKUP_MAX_AGE_HOURS (36)
#   backup run         last successful backup-run.sh                BACKUP_MAX_AGE_HOURS (36)
#   off-host copy      last successful restic-push.sh + the newest  OFFHOST_MAX_AGE_HOURS (36)
#                      snapshot actually in the repository
#   restore check      last successful pg-restore-check.sh or       RESTORE_CHECK_MAX_AGE_HOURS (240)
#                      restic-restore-check.sh
#   claude-home        newest claude-home-*.tar.gz in $BACKUP_DIR   CLAUDE_HOME_MAX_AGE_HOURS (36)
#     snapshot         (docs/runbooks/claude-home-restore.md)
#   claude-home        last successful push under tag               OFFHOST_MAX_AGE_HOURS (36)
#     off-host copy    marceline-claude-home + the newest snapshot
#                      actually in the repository
#   deletion journal   configured at all, and last successful push  OFFHOST_MAX_AGE_HOURS (36)
#                      under tag marceline-deletion-journal
#                      (ADR 0057: the only thing that can repair a
#                      restore)
#
# Env: the above, plus BACKUP_ALERT_* (see backup-run.sh) and BACKUP_ALERT_REPEAT_HOURS (24).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/backup/common.sh
source "$SCRIPT_DIR/common.sh"

BACKUP_MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-36}"
OFFHOST_MAX_AGE_HOURS="${OFFHOST_MAX_AGE_HOURS:-36}"
RESTORE_CHECK_MAX_AGE_HOURS="${RESTORE_CHECK_MAX_AGE_HOURS:-240}"
CLAUDE_HOME_MAX_AGE_HOURS="${CLAUDE_HOME_MAX_AGE_HOURS:-36}"
CLAUDE_HOME_TAG="marceline-claude-home"

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

quiet=0
case "${1:-}" in
  "") ;;
  --quiet) quiet=1 ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

problems=()

note() { ((quiet)) || echo "$(ts) $*"; }

# report NAME AGE_HOURS MAX_HOURS DETAIL
report() {
  local name="$1" age="$2" max="$3" detail="$4"
  if ((age > max)); then
    problems+=("$name last succeeded ${age}h ago (max ${max}h)")
    echo "$(ts) OVERDUE $name age=${age}h max=${max}h $detail"
  else
    note "OK       $name age=${age}h max=${max}h $detail"
  fi
}

# 1. A dump on disk, with a timestamp we can see without trusting any state file.
newest="$(list_dumps | tail -n 1 || true)"
if [[ -z "$newest" ]]; then
  problems+=("no dump in $BACKUP_DIR at all")
  echo "$(ts) MISSING  local dump: no marceline-*.dump in $BACKUP_DIR"
else
  dump_age=$((($(date +%s) - $(stat -c %Y "$BACKUP_DIR/$newest")) / 3600))
  report "local dump" "$dump_age" "$BACKUP_MAX_AGE_HOURS" "file=$newest"
fi

# 2. A backup run that finished cleanly (a dump can also be left over from a manual run).
if run_age="$(stamp_age_hours last-run)"; then
  report "backup run" "$run_age" "$BACKUP_MAX_AGE_HOURS" "stamp=$STATE_DIR/last-run"
else
  problems+=("no successful backup-run.sh has ever been recorded")
  echo "$(ts) MISSING  backup run: no $STATE_DIR/last-run (has the cron entry ever fired?)"
fi

# 3. The off-host copy: the local stamp says we tried, the repository says it arrived.
if ! restic_configured; then
  note "SKIP     off-host copy: RESTIC_REPOSITORY unset — backups exist only on this machine"
elif ! command -v "$RESTIC_BIN" >/dev/null 2>&1; then
  problems+=("restic is configured but not installed")
  echo "$(ts) MISSING  off-host copy: restic is not installed"
else
  if push_age="$(stamp_age_hours last-push)"; then
    report "off-host push" "$push_age" "$OFFHOST_MAX_AGE_HOURS" "stamp=$STATE_DIR/last-push"
  else
    problems+=("no successful off-host push has ever been recorded")
    echo "$(ts) MISSING  off-host push: no $STATE_DIR/last-push"
  fi

  # Ask the repository itself. A stamp only proves this machine believed it succeeded; the
  # snapshot list proves something is actually there to restore from.
  snap_ok=1
  snap_json="$(run_restic snapshots --tag "$RESTIC_TAG" --latest 1 --json 2>&1)" || snap_ok=0
  if ((snap_ok == 0)) || [[ "$snap_json" != *short_id* ]]; then
    problems+=("the off-host repository could not be listed")
    echo "$(ts) FAIL     off-host repository is unreachable, uninitialised or the password is wrong"
  else
    snap_epoch="$(newest_snapshot_epoch "$snap_json")"
    if [[ -n "$snap_epoch" ]]; then
      snap_age=$((($(date +%s) - snap_epoch) / 3600))
      report "off-host snapshot" "$snap_age" "$OFFHOST_MAX_AGE_HOURS" "newest snapshot in repo"
    else
      note "WARN     off-host snapshot: could not read the snapshot timestamp"
    fi
  fi
fi

# 4. A restore that was actually exercised. An untested backup is not a backup.
if check_age="$(stamp_age_hours last-restore-check)"; then
  report "restore check" "$check_age" "$RESTORE_CHECK_MAX_AGE_HOURS" "stamp=$STATE_DIR/last-restore-check"
else
  problems+=("no restore check has ever succeeded")
  echo "$(ts) MISSING  restore check: no $STATE_DIR/last-restore-check"
fi

# 5. The curated ~/.claude snapshot on disk (docs/runbooks/claude-home-restore.md). Same idea as
# check 1, for the operator's memory notes rather than the database.
claude_newest="$(list_claude_snapshots | tail -n 1 || true)"
if [[ -z "$claude_newest" ]]; then
  problems+=("no claude-home-*.tar.gz in $BACKUP_DIR at all")
  echo "$(ts) MISSING  claude-home snapshot: no claude-home-*.tar.gz in $BACKUP_DIR"
else
  claude_age=$((($(date +%s) - $(stat -c %Y "$BACKUP_DIR/$claude_newest")) / 3600))
  report "claude-home snapshot" "$claude_age" "$CLAUDE_HOME_MAX_AGE_HOURS" "file=$claude_newest"
fi

# 6. Its off-host copy, under its own tag — same idea as check 3, kept separate so a stalled
# claude-home push can never hide behind, or hide, the database's.
if ! restic_configured; then
  note "SKIP     claude-home off-host copy: RESTIC_REPOSITORY unset — backups exist only on this machine"
elif ! command -v "$RESTIC_BIN" >/dev/null 2>&1; then
  problems+=("restic is configured but not installed")
  echo "$(ts) MISSING  claude-home off-host copy: restic is not installed"
else
  if claude_push_age="$(stamp_age_hours "last-push-$CLAUDE_HOME_TAG")"; then
    report "claude-home off-host push" "$claude_push_age" "$OFFHOST_MAX_AGE_HOURS" \
      "stamp=$STATE_DIR/last-push-$CLAUDE_HOME_TAG"
  else
    problems+=("no successful claude-home off-host push has ever been recorded")
    echo "$(ts) MISSING  claude-home off-host push: no $STATE_DIR/last-push-$CLAUDE_HOME_TAG"
  fi

  claude_snap_ok=1
  claude_snap_json="$(run_restic snapshots --tag "$CLAUDE_HOME_TAG" --latest 1 --json 2>&1)" ||
    claude_snap_ok=0
  if ((claude_snap_ok == 0)) || [[ "$claude_snap_json" != *short_id* ]]; then
    problems+=("the off-host repository could not be listed for tag $CLAUDE_HOME_TAG")
    echo "$(ts) FAIL     claude-home off-host repository is unreachable, uninitialised, the password is wrong, or no snapshot has ever been pushed under $CLAUDE_HOME_TAG"
  else
    claude_snap_epoch="$(newest_snapshot_epoch "$claude_snap_json")"
    if [[ -n "$claude_snap_epoch" ]]; then
      claude_snap_age=$((($(date +%s) - claude_snap_epoch) / 3600))
      report "claude-home off-host snapshot" "$claude_snap_age" "$OFFHOST_MAX_AGE_HOURS" \
        "newest snapshot in repo (tag $CLAUDE_HOME_TAG)"
    else
      note "WARN     claude-home off-host snapshot: could not read the snapshot timestamp"
    fi
  fi
fi

# 7. The deletion journal's off-host copy (ADR 0057, DPIA R24, issue #137). It is the only thing
# that can repair a restore, and it is the one file that is worthless if it is older than the dump
# it has to be replayed after. Its own tag, its own stamp — never hidden behind the database's.
if ! journal_configured; then
  problems+=("the deletion journal is not configured (DELETION_JOURNAL_DIR)")
  echo "$(ts) MISSING  deletion journal: DELETION_JOURNAL_DIR is unset or missing, so nothing records what a restore would undo (ADR 0057)"
elif ! restic_configured; then
  note "SKIP     deletion journal off-host copy: RESTIC_REPOSITORY unset"
elif ! command -v "$RESTIC_BIN" >/dev/null 2>&1; then
  problems+=("restic is configured but not installed")
  echo "$(ts) MISSING  deletion journal off-host copy: restic is not installed"
else
  if journal_push_age="$(stamp_age_hours "last-push-$DELETION_JOURNAL_TAG")"; then
    report "deletion journal push" "$journal_push_age" "$OFFHOST_MAX_AGE_HOURS" \
      "stamp=$STATE_DIR/last-push-$DELETION_JOURNAL_TAG"
  else
    problems+=("no successful deletion journal push has ever been recorded")
    echo "$(ts) MISSING  deletion journal push: no $STATE_DIR/last-push-$DELETION_JOURNAL_TAG"
  fi
fi

if ((${#problems[@]} == 0)); then
  note "OK backup-check everything within its limit"
  exit 0
fi

body="$(printf '%s\n' "Backup watchdog on $(hostname -s) found:" "" "${problems[@]/#/- }" "" \
  "Limits: dump/run ${BACKUP_MAX_AGE_HOURS}h, off-host ${OFFHOST_MAX_AGE_HOURS}h, restore check ${RESTORE_CHECK_MAX_AGE_HOURS}h, claude-home snapshot ${CLAUDE_HOME_MAX_AGE_HOURS}h." \
  "Backup directory: $BACKUP_DIR (log: $BACKUP_DIR/backup.log)." \
  "Runbooks: docs/runbooks/backup-restore.md, docs/runbooks/claude-home-restore.md.")"
if [[ "${NO_ALERT:-}" != 1 ]]; then
  alert_throttled overdue "overdue on $(hostname -s): ${#problems[@]} problem(s)" "$body"
fi
echo "$(ts) FAIL backup-check problems=${#problems[@]}"
exit 1
