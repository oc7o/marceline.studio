#!/usr/bin/env bash
# Push a verified dump to the encrypted off-host restic repository (Hetzner Storage Box over SFTP)
# and apply snapshot retention. Prints one summary line; exits non-zero on failure.
#
#   infra/backup/restic-push.sh                       # newest dump in $BACKUP_DIR, tag marceline-db
#   infra/backup/restic-push.sh FILE.dump             # a specific dump, tag marceline-db
#   infra/backup/restic-push.sh FILE TAG              # any file, under its own tag and retention
#                                                      # group (e.g. the claude-home-backup.sh
#                                                      # tarball, tag marceline-claude-home)
#   infra/backup/restic-push.sh DIR TAG               # a named directory, for the deletion journal
#                                                      # (ADR 0057, tag marceline-deletion-journal)
#   infra/backup/restic-push.sh --list [TAG]          # snapshots in the repository
#
# Env: RESTIC_REPOSITORY (e.g. sftp:marceline-storagebox:/marceline), one of RESTIC_PASSWORD_FILE
#      / RESTIC_PASSWORD_COMMAND / RESTIC_PASSWORD, optional BACKUP_SSH_KEY and BACKUP_SSH_PORT,
#      RESTIC_KEEP_LAST/DAILY/WEEKLY/MONTHLY, MAX_DUMP_MB, RESTIC_BIN.
# One-time setup, retention policy and restore: docs/runbooks/backup-restore.md.
#
# The dump carries flagged URLs and safety records. restic encrypts and authenticates it on this
# machine, before anything is transmitted; the Storage Box only ever sees ciphertext, and the
# repository password never leaves .env.
#
# A distinct TAG (default: $RESTIC_TAG, "marceline-db") both scopes the "forget --prune" retention
# group and the per-tag "last-push[-TAG]" stamp backup-check.sh reads, so pushing a second kind of
# file here (the curated ~/.claude snapshot) never mixes its retention or its staleness with the
# database's.
set -euo pipefail

# shellcheck source=infra/backup/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# A dump is URLs, hashes, labels and safety records — tens of MB. Anything near a gigabyte means
# something got into the database that must never be there (CLAUDE.md: never store media), and
# the off-host copy is the last place that should be allowed to travel to.
MAX_DUMP_MB="${MAX_DUMP_MB:-2048}"

fail() {
  echo "$(ts) FAIL push: $*"
  exit 1
}

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

require_repo() {
  command -v "$RESTIC_BIN" >/dev/null 2>&1 ||
    fail "restic is not installed (see docs/runbooks/backup-restore.md)"
  restic_configured ||
    fail "RESTIC_REPOSITORY is not set; the off-host copy is not configured yet"
  restic_password_configured ||
    fail "no repository password: set RESTIC_PASSWORD_FILE (preferred), RESTIC_PASSWORD_COMMAND or RESTIC_PASSWORD in .env"
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
  --list)
    require_repo
    run_restic snapshots --tag "${2:-$RESTIC_TAG}"
    exit 0
    ;;
esac

require_repo

if [[ -n "${1:-}" ]]; then
  dump="$1"
else
  newest="$(list_dumps | tail -n 1)"
  [[ -n "$newest" ]] || fail "no marceline-*.dump in $BACKUP_DIR (run pg-backup.sh first)"
  dump="$BACKUP_DIR/$newest"
fi
tag="${2:-$RESTIC_TAG}"
# A directory is allowed only when it is named explicitly: the deletion journal (ADR 0057) is a
# directory of append-only segment files, not one file. The default path stays "the newest dump".
if [[ -d "$dump" ]]; then
  [[ -r "$dump" && -x "$dump" ]] || fail "cannot read $dump"
else
  [[ -f "$dump" && -r "$dump" ]] || fail "cannot read $dump"
fi

size="$(du -sb "$dump" | cut -f1)"
((size > 0)) || fail "$dump is empty"
size_mb=$((size / 1024 / 1024))
# What too large actually means depends on what we are pushing; say so specifically rather than
# with a message written for the database dump alone.
case "$(basename "$dump")" in
  marceline-*.dump)
    oversize_reason="the database holds URLs, hashes and metadata only, so a dump this size means media reached it — that is a bug, not something to copy off-host"
    ;;
  claude-home-*.tar.gz)
    oversize_reason="the curated ~/.claude snapshot is memory notes and small config files; this size means something unexpected (session transcripts, a stray large file) got swept into the allowlist — check with claude-home-backup.sh --dry-run before raising the limit"
    ;;
  journal)
    oversize_reason="the deletion journal is one short line per deleted row (ADR 0057); this size means something is writing to it in a loop, or something other than the journal is in that directory — read it before raising the limit"
    ;;
  *)
    oversize_reason="check what is actually in this file before raising the limit"
    ;;
esac
# Compare in bytes, not in truncated megabytes: integer division would let a 1.9 GB dump through a
# 1 GB limit, and would make MAX_DUMP_MB=0 accept anything under a megabyte instead of nothing.
((size <= MAX_DUMP_MB * 1024 * 1024)) || fail \
  "$(basename "$dump") is ${size_mb} MB (MAX_DUMP_MB=${MAX_DUMP_MB}): $oversize_reason"

start=$(date +%s)
acquire_lock 1800

# Prove the repository answers and the password is right before uploading anything. This also
# turns "the operator never ran restic init" into a clear failure instead of a half-written repo:
# nothing here ever creates a repository, because an auto-init would happily create an empty one
# at a mistyped path and report success for months.
run_restic cat config >/dev/null 2>&1 ||
  fail "repository is unreachable, uninitialised or the password is wrong (restic cat config failed)"

run_restic backup --tag "$tag" --host "$(hostname -s)" "$dump" ||
  fail "restic backup of $(basename "$dump") failed"

# Retention (docs/runbooks/backup-restore.md): deleted safety records must not live forever in
# snapshots (issue #66), so old snapshots are actually pruned, not merely forgotten.
# --group-by host,tags is load-bearing. restic groups snapshots by host AND PATHS by default, and
# every dump has a unique filename, so each snapshot would land in a group of its own, keep itself
# as "last snapshot", and nothing would ever be pruned — retention would silently do nothing
# forever (seen on the first drill run: two pushes, two groups, both kept). Grouping by tag puts
# every push under the same tag into one bucket that the policy can actually thin, and keeps the
# claude-home tarball's retention from mixing with the database dump's.
run_restic forget --tag "$tag" --group-by host,tags --prune \
  --keep-last "$RESTIC_KEEP_LAST" \
  --keep-daily "$RESTIC_KEEP_DAILY" \
  --keep-weekly "$RESTIC_KEEP_WEEKLY" \
  --keep-monthly "$RESTIC_KEEP_MONTHLY" ||
  fail "restic forget --prune failed (the snapshot was uploaded; retention was not applied)"

snapshots="$(run_restic snapshots --tag "$tag" --json 2>/dev/null |
  grep -o '"short_id"' | wc -l || true)"

# The default tag keeps writing the original "last-push" stamp name (backup-check.sh's existing
# check reads it); any other tag gets its own stamp so its staleness is never hidden behind, or
# hides, the database's.
stamp_name="last-push"
[[ "$tag" == "$RESTIC_TAG" ]] || stamp_name="last-push-$tag"
stamp_write "$stamp_name"
echo "$(ts) OK push file=$(basename "$dump") tag=$tag size=$(human_size "$size") snapshots=$snapshots" \
  "keep=last$RESTIC_KEEP_LAST/${RESTIC_KEEP_DAILY}d/${RESTIC_KEEP_WEEKLY}w/${RESTIC_KEEP_MONTHLY}m" \
  "duration=$(($(date +%s) - start))s"
