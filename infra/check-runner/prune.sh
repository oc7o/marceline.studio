#!/usr/bin/env bash
# Remove check-runner checkouts nobody is using any more.
#
# `make remote-check` rsyncs the tree to /srv/checks/$CHECK_NAME, and CHECK_NAME defaults to the
# working directory's name — so every agent worktree leaves a full copy of the repository behind
# when it finishes. On 2026-09-18 that was 51 directories and 45 GB, of which two were live.
#
# Nothing here is data: a pruned directory is re-created by the next `remote-check` that needs it,
# at the cost of one rsync. Even so this defaults to a DRY RUN and prints what it would remove,
# because a script that deletes by default is a script that eventually deletes the wrong thing.
#
#   bash infra/check-runner/prune.sh                 # dry run, 3 days
#   bash infra/check-runner/prune.sh --days 7        # dry run, 7 days
#   bash infra/check-runner/prune.sh --apply         # actually remove
#
# Run it ON the runner, or over ssh:
#   ssh "$CHECK_HOST" 'bash -s' < infra/check-runner/prune.sh
set -euo pipefail

ROOT="${CHECK_ROOT:-/srv/checks}"
DAYS=3
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -d "$ROOT" ] || { echo "no $ROOT here — run this on the check runner"; exit 1; }

# A directory a check is running in right now must never be touched, however old it looks.
live=""
for pid in $(pgrep -f 'run-check.sh' || true); do
  cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
  [ -n "$cwd" ] && live="$live $cwd"
done

removed=0
kept=0
freed=0
for dir in "$ROOT"/*/; do
  dir="${dir%/}"
  name=$(basename "$dir")
  case " $live " in *" $dir "*) echo "keep    $name (a check is running in it)"; kept=$((kept + 1)); continue ;; esac
  # `.last-check` is written by run-check.sh at the start of every run. rsync touches the directory
  # itself on each sync, so its own mtime says nothing; the stamp says when a check last ran here.
  # A checkout with no stamp predates the stamp and is prunable on age alone.
  stamp="$dir/.last-check"
  if [ -f "$stamp" ] && [ -n "$(find "$stamp" -maxdepth 0 -mtime "-$DAYS")" ]; then
    kept=$((kept + 1))
    continue
  fi
  if [ ! -f "$stamp" ] && [ -n "$(find "$dir" -maxdepth 0 -mmin -60)" ]; then
    echo "keep    $name (no stamp yet, but synced within the hour)"
    kept=$((kept + 1))
    continue
  fi
  size=$(du -sm "$dir" 2>/dev/null | cut -f1)
  freed=$((freed + size))
  removed=$((removed + 1))
  if [ "$APPLY" -eq 1 ]; then
    rm -rf -- "$dir"
    echo "removed $name (${size} MB)"
  else
    echo "would remove $name (${size} MB, untouched for $DAYS+ days)"
  fi
done

if [ "$APPLY" -eq 1 ]; then
  echo "removed $removed directories, freed ${freed} MB; kept $kept"
else
  echo "would remove $removed directories, freeing ${freed} MB; keeping $kept. Pass --apply to do it."
fi
