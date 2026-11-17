#!/usr/bin/env bash
# Dump the marceline database from the running compose `db` container (pg_dump -Fc), verify the
# dump, and rotate old dumps. Prints one summary line; exits non-zero on failure.
#
#   infra/backup/pg-backup.sh                 # dump + verify + rotate
#   infra/backup/pg-backup.sh --rotate-only   # only apply retention to $BACKUP_DIR
#
# Env: BACKUP_DIR (default ~/backups/marceline), KEEP_DAILY (14), KEEP_WEEKLY (8),
#      DB_NAME (marceline), DB_CONTAINER (default: resolved from infra/compose.yaml).
# See docs/operations/backups.md.
set -euo pipefail

# shellcheck source=infra/backup/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

KEEP_DAILY="${KEEP_DAILY:-14}"
KEEP_WEEKLY="${KEEP_WEEKLY:-8}"

fail() {
  echo "$(ts) FAIL backup: $*"
  exit 1
}

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Retention: keep every dump from the newest KEEP_DAILY distinct dates, plus the newest dump of
# each of the newest KEEP_WEEKLY Sundays. Everything else matching marceline-*.dump is deleted.
# Sets KEPT and DELETED.
rotate() {
  local -a files
  mapfile -t files < <(list_dumps | sort -r)  # newest first

  local -A daily_dates=() weekly_dates=() keep=()
  local f d weekday n_daily=0 n_weekly=0
  for f in "${files[@]}"; do
    d="${f:10:8}"
    if ! weekday="$(date -d "$d" +%u 2>/dev/null)"; then
      keep[$f]=1  # not a real date: not ours to judge, leave it alone
      continue
    fi
    if [[ -z "${daily_dates[$d]:-}" ]] && ((n_daily < KEEP_DAILY)); then
      daily_dates[$d]=1
      n_daily=$((n_daily + 1))
    fi
    [[ -n "${daily_dates[$d]:-}" ]] && keep[$f]=1

    if [[ -z "${weekly_dates[$d]:-}" ]] && ((n_weekly < KEEP_WEEKLY)) \
      && [[ "$(date -d "$d" +%u 2>/dev/null || true)" == 7 ]]; then
      weekly_dates[$d]=1
      n_weekly=$((n_weekly + 1))
      keep[$f]=1  # newest dump of that Sunday, since files are newest first
    fi
  done

  KEPT=0
  DELETED=0
  for f in "${files[@]}"; do
    if [[ -n "${keep[$f]:-}" ]]; then
      KEPT=$((KEPT + 1))
    else
      rm -f -- "$BACKUP_DIR/$f"
      DELETED=$((DELETED + 1))
    fi
  done
}

[[ "$KEEP_DAILY" =~ ^[1-9][0-9]*$ ]] || fail "KEEP_DAILY must be an integer >= 1"
[[ "$KEEP_WEEKLY" =~ ^[0-9]+$ ]] || fail "KEEP_WEEKLY must be an integer >= 0"

case "${1:-}" in
  "") ;;
  --rotate-only)
    acquire_lock 600
    rotate
    echo "$(ts) OK rotate dir=$BACKUP_DIR kept=$KEPT deleted=$DELETED"
    exit 0
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

start=$(date +%s)
umask 077
acquire_lock 1800

CONTAINER="$(db_container)" || fail "compose service db is not running"

name="marceline-$(date +%Y%m%d-%H%M%S).dump"
final="$BACKUP_DIR/$name"
tmp="$BACKUP_DIR/.$name.tmp"
[[ ! -e "$final" ]] || fail "$final already exists"
trap 'rm -f -- "$tmp"' EXIT

in_db pg_dump --format=custom --no-password --dbname="$DB_NAME" >"$tmp" \
  || fail "pg_dump of $DB_NAME failed"
chmod 600 "$tmp"

size=$(stat -c %s "$tmp")
((size > 0)) || fail "dump is empty"

toc="$(in_db_i pg_restore --list <"$tmp")" || fail "pg_restore --list could not read the dump"
for table in "${KEY_TABLES[@]}"; do
  grep -Fq " TABLE DATA public $table " <<<"$toc" || fail "table $table missing from dump TOC"
done
tables=$(grep -Fc " TABLE DATA " <<<"$toc" || true)

sync "$tmp"
mv -T -- "$tmp" "$final"
trap - EXIT

rotate
echo "$(ts) OK backup file=$final size=$(human_size "$size") tables=$tables" \
  "duration=$(($(date +%s) - start))s kept=$KEPT deleted=$DELETED"
