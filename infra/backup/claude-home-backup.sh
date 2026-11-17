#!/usr/bin/env bash
# Snapshot the curated, survivable part of the operator's Claude Code home (~/.claude) into the
# same local + off-host backup this directory already runs for the database. Nothing here is
# credentials: it is the memory the operator would otherwise have to rebuild by hand if this
# machine were lost — the per-project memory notes and the configuration the operator wrote
# himself (docs/runbooks/claude-home-restore.md).
#
#   infra/backup/claude-home-backup.sh             # build, verify, rotate
#   infra/backup/claude-home-backup.sh --dry-run   # print exactly what would be included; writes
#                                                   # nothing, never fails silently either
#
# Env: CLAUDE_HOME_DIR (default ~/.claude), BACKUP_DIR (default ~/backups/marceline),
#      CLAUDE_HOME_KEEP_LOCAL (default 5), CLAUDE_HOME_MAX_MB (default 300),
#      CLAUDE_HOME_INCLUDE_TRANSCRIPTS=1 to also carry session transcripts (off by default — see
#      the runbook for the trade-off, this is the operator's call, not a silent default).
#
# --- The allowlist, and why it is one -------------------------------------------------------
# ~/.claude mixes durable memory with runtime state and, right next to it, the credential file
# that authenticates as the operator. An exclude list is one new filename away from shipping that
# file off-host; an allowlist cannot leak a file it never names, so a new kind of file appearing
# in ~/.claude (a new token store, a new cache) is excluded by construction, not by a filter this
# script has to remember to add. Every entry below is deliberate:
#
#   settings.json, settings.local.json   configuration the operator edited or approved (theme,
#                                         model, permissions) — never a secret, Claude Code keeps
#                                         those in .credentials.json, which is never in this list.
#   CLAUDE.md                            the operator's own global instructions, if any.
#   agents/, commands/, skills/          custom agents/commands/skills the operator wrote, if any.
#   projects/*/memory/**                 the memory notes and their MEMORY.md index — the part
#                                         CLAUDE.md calls "the valuable part". This is the one
#                                         group whose absence fails the run on its own (below).
#
# Never in this list, on purpose: .credentials.json, *token*/*oauth*/keychain files (there are
# none today, and an allowlist means a new one is simply not picked up), ~/.claude.json (account
# and usage metadata; its "mcpServers" field can legitimately hold a server's own secret, and
# nothing here is worth that risk — restoring it means logging in and reconfiguring MCP servers,
# see the runbook), and every runtime/cache directory (sessions/, session-env/, shell-snapshots/,
# file-history/, cache/, backups/, downloads/, feedback/, plans/, history.jsonl) — none of it is
# needed to rebuild the operator's working memory, and history.jsonl in particular is a record of
# every prompt ever typed, unfiltered.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/backup/common.sh
source "$SCRIPT_DIR/common.sh"

CLAUDE_HOME_DIR="${CLAUDE_HOME_DIR:-$HOME/.claude}"
CLAUDE_HOME_KEEP_LOCAL="${CLAUDE_HOME_KEEP_LOCAL:-5}"
CLAUDE_HOME_MAX_MB="${CLAUDE_HOME_MAX_MB:-300}"
CLAUDE_HOME_INCLUDE_TRANSCRIPTS="${CLAUDE_HOME_INCLUDE_TRANSCRIPTS:-0}"

fail() {
  echo "$(ts) FAIL claude-home-backup: $*"
  exit 1
}

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

dry_run=0
case "${1:-}" in
  "") ;;
  --dry-run) dry_run=1 ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

# --- The allowlist itself, as code -----------------------------------------------------------
# Every function below prints NUL-separated paths, relative to $CLAUDE_HOME_DIR, of files that
# exist right now. A pattern matching nothing is not an error by itself (agents/commands/skills
# may genuinely not exist yet) — only the *whole* allowlist matching nothing, or the memory group
# specifically matching nothing, is (both checked with require_nonempty below).

# The memory notes and their index (issue: survivable agent memory). This is checked on its own
# because it is "the valuable part" per CLAUDE.md, distinct from configuration.
collect_memory_files_z() {
  [[ -d "$CLAUDE_HOME_DIR/projects" ]] || return 0
  (
    cd "$CLAUDE_HOME_DIR" || exit 1
    while IFS= read -r -d '' mdir; do
      find "$mdir" -type f -print0
    done < <(find projects -mindepth 2 -maxdepth 2 -type d -name memory -print0 2>/dev/null)
  )
}

# Session transcripts (projects/*/*.jsonl) — deliberately not in the default allowlist. Judgement
# call, stated in docs/runbooks/claude-home-restore.md: they are the only record of *why* many
# decisions were made, but they are large and hold everything ever discussed, unfiltered.
collect_transcript_files_z() {
  [[ "$CLAUDE_HOME_INCLUDE_TRANSCRIPTS" == 1 ]] || return 0
  [[ -d "$CLAUDE_HOME_DIR/projects" ]] || return 0
  (cd "$CLAUDE_HOME_DIR" && find projects -mindepth 2 -maxdepth 2 -type f -name '*.jsonl' -print0 2>/dev/null)
}

collect_files_z() {
  [[ -d "$CLAUDE_HOME_DIR" ]] || return 0
  (
    cd "$CLAUDE_HOME_DIR" || exit 1
    local f dir
    for f in settings.json settings.local.json CLAUDE.md; do
      [[ -f "$f" ]] && printf '%s\0' "$f"
    done
    for dir in agents commands skills; do
      [[ -d "$dir" ]] && find "$dir" -type f -print0
    done
  )
  collect_memory_files_z
  collect_transcript_files_z
}

# require_nonempty COUNT LABEL — fail loudly, not with an empty backup nobody notices.
require_nonempty() {
  local count="$1" label="$2"
  ((count > 0)) || fail "$label matched 0 files — the ~/.claude layout changed, or CLAUDE_HOME_DIR=$CLAUDE_HOME_DIR is wrong. Fix the allowlist before trusting any backup taken since."
}

[[ -d "$CLAUDE_HOME_DIR" ]] || fail "CLAUDE_HOME_DIR=$CLAUDE_HOME_DIR does not exist"

mapfile -d '' -t all_files < <(collect_files_z | sort -z -u)
mapfile -d '' -t memory_files < <(collect_memory_files_z | sort -z -u)

require_nonempty "${#all_files[@]}" "the allowlist"
require_nonempty "${#memory_files[@]}" "the memory group (projects/*/memory/**)"

if ((dry_run)); then
  total=0
  for f in "${all_files[@]}"; do
    size="$(stat -c %s -- "$CLAUDE_HOME_DIR/$f" 2>/dev/null || echo 0)"
    total=$((total + size))
    printf '%12s  %s\n' "$(human_size "$size")" "$f"
  done
  echo
  echo "$(ts) OK claude-home-backup dry-run root=$CLAUDE_HOME_DIR files=${#all_files[@]}" \
    "memory_files=${#memory_files[@]} total=$(human_size "$total")" \
    "transcripts=$CLAUDE_HOME_INCLUDE_TRANSCRIPTS"
  exit 0
fi

start=$(date +%s)
acquire_lock 1800

name="claude-home-$(date +%Y%m%d-%H%M%S).tar.gz"
final="$BACKUP_DIR/$name"
tmp="$BACKUP_DIR/.$name.tmp"
[[ ! -e "$final" ]] || fail "$final already exists"
trap 'rm -f -- "$tmp"' EXIT

printf '%s\0' "${all_files[@]}" |
  tar -czf "$tmp" --null -C "$CLAUDE_HOME_DIR" -T - || fail "tar failed"
chmod 600 "$tmp"

size="$(stat -c %s "$tmp")"
((size > 0)) || fail "archive is empty"
size_mb=$((size / 1024 / 1024))
((size <= CLAUDE_HOME_MAX_MB * 1024 * 1024)) || fail \
  "$name is ${size_mb} MB (CLAUDE_HOME_MAX_MB=${CLAUDE_HOME_MAX_MB}): memory notes and small config files should be tens of KB to a few MB — this size means something unexpected got in. Run 'claude-home-backup.sh --dry-run' and check the listing before raising the limit"

# Verify the archive is readable and actually carries at least one memory file, the same way
# pg-backup.sh checks its dump's TOC for KEY_TABLES before trusting it.
tar -tzf "$tmp" >/dev/null 2>&1 || fail "the archive built but does not read back cleanly"
# `grep -c`, not `grep -q`: -q exits at the first match, tar then dies of SIGPIPE on a long listing,
# and under `pipefail` a healthy archive read as "no memory file" (2026-09-19, once the archive had
# grown past one pipe buffer). -c reads the listing to its end.
[ "$(tar -tzf "$tmp" 2>/dev/null | grep -Ec '/memory/[^/]+\.md$')" -gt 0 ] ||
  fail "the archive was built but contains no projects/*/memory/*.md — refusing to keep it"

sync "$tmp"
mv -T -- "$tmp" "$final"
trap - EXIT

# Local rotation: keep the newest CLAUDE_HOME_KEEP_LOCAL copies. Long-term retention lives
# off-host (restic --keep-last/daily/weekly/monthly, same as the database), so this is just
# housekeeping against unbounded local growth, not the real retention policy.
mapfile -t existing < <(list_claude_snapshots | sort -r)  # newest first
KEPT=0
DELETED=0
for f in "${existing[@]}"; do
  if ((KEPT < CLAUDE_HOME_KEEP_LOCAL)); then
    KEPT=$((KEPT + 1))
  else
    rm -f -- "$BACKUP_DIR/$f"
    DELETED=$((DELETED + 1))
  fi
done

echo "$(ts) OK claude-home-backup file=$final size=$(human_size "$size") files=${#all_files[@]}" \
  "memory_files=${#memory_files[@]} transcripts=$CLAUDE_HOME_INCLUDE_TRANSCRIPTS" \
  "duration=$(($(date +%s) - start))s kept=$KEPT deleted=$DELETED"
