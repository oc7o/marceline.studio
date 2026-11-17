#!/usr/bin/env bash
# Print (default) or install (--install) the user crontab entries for database backups:
#   daily 03:30        backup-run.sh          dump, verify, rotate, encrypted off-host copy —
#                                              and the same for the curated ~/.claude snapshot
#                                              (docs/runbooks/claude-home-restore.md)
#   daily 09:15        backup-check.sh        watchdog: alert when any of these is overdue
#   Sunday 04:30       pg-restore-check.sh    restore the newest local dump into a scratch database
#   Sunday 05:15       restic-restore-check.sh restore the newest off-host snapshot, same way
# Output goes to $BACKUP_DIR/backup.log (default ~/backups/marceline). Re-running --install
# replaces the previous entries (matched by marker comment), so it is idempotent.
# Run it from the main checkout: the crontab points at this script's own repository.
#
# The watchdog is a separate entry on purpose: if the 03:30 entry is removed, edited wrong or its
# script hangs, nothing else would ever notice (issue #39). It still cannot catch cron itself
# dying — see "What this does not catch" in docs/runbooks/backup-restore.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/marceline}"
MARK="# marceline-db-backup"

if [[ "$REPO_DIR" == */.claude/worktrees/* && "${FORCE:-}" != 1 ]]; then
  echo "refusing: $REPO_DIR is an agent worktree; run from the main checkout (FORCE=1 overrides)" >&2
  exit 1
fi

log="$(printf '%q' "$BACKUP_DIR/backup.log")"
env_prefix=""
[[ "$BACKUP_DIR" == "$HOME/backups/marceline" ]] || env_prefix="BACKUP_DIR=$(printf '%q' "$BACKUP_DIR") "

lines=(
  "30 3 * * * ${env_prefix}$(printf '%q' "$SCRIPT_DIR/backup-run.sh") >> $log 2>&1 $MARK"
  "15 9 * * * ${env_prefix}$(printf '%q' "$SCRIPT_DIR/backup-check.sh") >> $log 2>&1 $MARK"
  "30 4 * * 0 ${env_prefix}$(printf '%q' "$SCRIPT_DIR/pg-restore-check.sh") >> $log 2>&1 $MARK"
  "15 5 * * 0 ${env_prefix}$(printf '%q' "$SCRIPT_DIR/restic-restore-check.sh") >> $log 2>&1 $MARK"
)

case "${1:-}" in
  "")
    printf '%s\n' "${lines[@]}"
    echo "# not installed; run with --install" >&2
    ;;
  --install)
    if [[ ! -d "$BACKUP_DIR" ]]; then
      mkdir -p "$BACKUP_DIR"
      chmod 700 "$BACKUP_DIR"
    fi
    current="$(crontab -l 2>/dev/null || true)"
    kept="$(printf '%s\n' "$current" | grep -vF -- "$MARK" || true)"
    {
      [[ -z "$kept" ]] || printf '%s\n' "$kept"
      printf '%s\n' "${lines[@]}"
    } | crontab -
    echo "installed:"
    crontab -l | grep -F -- "$MARK"
    ;;
  *)
    echo "usage: $0 [--install]" >&2
    exit 2
    ;;
esac
