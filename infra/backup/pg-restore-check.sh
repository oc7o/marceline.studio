#!/usr/bin/env bash
# Prove a dump restores **and can be repaired**: load it into the scratch database
# marceline_restore_check on the same Postgres, compare key-table row counts with the live database
# (read-only), replay the deletion journal onto it and require zero entries left un-applied
# (ADR 0057), then drop the scratch database. Never targets the live database.
#
#   infra/backup/pg-restore-check.sh              # newest dump in $BACKUP_DIR
#   infra/backup/pg-restore-check.sh FILE.dump    # a specific dump
#
# Env: BACKUP_DIR (default ~/backups/marceline), MAX_AGE_HOURS (36; newest dump older than this
#      fails the check), DB_NAME (marceline), DB_CONTAINER, DELETION_JOURNAL_DIR (the journal to
#      replay; the check fails while it is unset).
# Live counts keep moving while workers run, so small positive diffs are expected. The check
# fails if the restore errors, a key table is missing, a table is empty in the dump but not live,
# or the deletion journal leaves an entry un-applied.
set -euo pipefail

# shellcheck source=infra/backup/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly RESTORE_DB="marceline_restore_check"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"

fail() {
  echo "$(ts) FAIL restore-check: $*"
  exit 1
}

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

target_is_safe() {
  [[ "$RESTORE_DB" == *_restore_check && "$RESTORE_DB" != "marceline" && "$RESTORE_DB" != "$DB_NAME" ]]
}

drop_restore_db() {
  target_is_safe || return 1
  in_db dropdb --if-exists --force "$RESTORE_DB"
}

# count DB TABLE -> row count, in a read-only session
count_rows() {
  in_db env PGOPTIONS='-c default_transaction_read_only=on' \
    psql -X -At -v ON_ERROR_STOP=1 --dbname="$1" -c "SELECT count(*) FROM public.\"$2\"" 2>/dev/null
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
esac

target_is_safe || fail "refusing to restore into $RESTORE_DB (live database is $DB_NAME)"

stale=""
if [[ -n "${1:-}" ]]; then
  dump="$1"
else
  latest="$(list_dumps | tail -n 1)"
  [[ -n "$latest" ]] || fail "no marceline-*.dump in $BACKUP_DIR"
  dump="$BACKUP_DIR/$latest"
  age_h=$((($(date +%s) - $(stat -c %Y "$dump")) / 3600))
  ((age_h <= MAX_AGE_HOURS)) || stale="newest dump is ${age_h}h old (max ${MAX_AGE_HOURS}h)"
fi
[[ -f "$dump" && -r "$dump" ]] || fail "cannot read $dump"

acquire_lock 3600
CONTAINER="$(db_container)" || fail "compose service db is not running"

start=$(date +%s)
trap 'drop_restore_db >/dev/null 2>&1 || echo "$(ts) WARN restore-check: could not drop $RESTORE_DB"' EXIT

drop_restore_db >/dev/null 2>&1 || fail "could not drop leftover $RESTORE_DB"
target_is_safe && in_db createdb --template=template0 "$RESTORE_DB" || fail "createdb $RESTORE_DB failed"
target_is_safe && in_db_i pg_restore --no-owner --no-privileges --exit-on-error \
  --dbname="$RESTORE_DB" <"$dump" || fail "pg_restore of $dump failed"
restore_s=$(($(date +%s) - start))

problems=()
printf '%-30s %12s %12s %8s\n' table live restored diff
for table in "${KEY_TABLES[@]}"; do
  live="$(count_rows "$DB_NAME" "$table")" || live="ERR"
  restored="$(count_rows "$RESTORE_DB" "$table")" || restored="MISSING"
  if [[ "$live" =~ ^[0-9]+$ && "$restored" =~ ^[0-9]+$ ]]; then
    diff=$((live - restored))
    ((restored > 0 || live == 0)) || problems+=("$table empty in dump")
  else
    diff="-"
    problems+=("$table live=$live restored=$restored")
  fi
  printf '%-30s %12s %12s %8s\n' "$table" "$live" "$restored" "$diff"
done
[[ -z "$stale" ]] || problems+=("$stale")

# The deletion journal replays with nothing left over (ADR 0057, DPIA R24, issue #137).
#
# A dump that restores is only half of what a restore needs: it puts back every row deleted since
# the dump, so the journal has to be replayable against it before it may serve anything. This is
# where that is proved, on the scratch database, weekly, with no human present — `--apply` is safe
# here and only here, because it runs against $RESTORE_DB and that database is dropped a few lines
# below. Any entry the replay could not apply, or could not read, fails the check.
#
# Django is run from the host (like the failure alert in common.sh) and pointed at the scratch
# database by POSTGRES_DB; everything else comes from .env through settings.py's own load_dotenv.
# The sequence is `migrate` then `reapply_deletions`, exactly as docs/runbooks/backup-restore.md § C.
# `migrate` first, for the same reason the real procedure does it in that order: the dump is up to a
# day old, so a migration merged since it would make every ORM query here fail for a reason that has
# nothing to do with the journal.
journal_summary="not configured"
if journal_configured; then
  journal_out="$(
    cd "$REPO_DIR/backend" &&
      POSTGRES_DB="$RESTORE_DB" uv run python manage.py migrate --noinput 2>&1 &&
      POSTGRES_DB="$RESTORE_DB" uv run python manage.py reapply_deletions --apply --quiet 2>&1
  )" && journal_status=0 || journal_status=$?
  journal_summary="$(grep -o 'reapply-deletions .*' <<<"$journal_out" | tail -n 1)"
  if ((journal_status != 0)); then
    problems+=("the deletion journal did not replay onto the restore (${journal_summary:-no summary line})")
  elif [[ -z "$journal_summary" ]]; then
    problems+=("the deletion journal replay printed no summary line")
  fi
else
  problems+=("the deletion journal is not configured (DELETION_JOURNAL_DIR): a restore of this dump would silently undo every deletion since it (ADR 0057)")
fi

drop_restore_db >/dev/null || fail "could not drop $RESTORE_DB"
trap - EXIT

summary="dump=$dump size=$(human_size "$(stat -c %s "$dump")") restore=${restore_s}s tables=${#KEY_TABLES[@]} journal=[${journal_summary}]"
if ((${#problems[@]})); then
  fail "$summary problems: $(IFS=';'; echo "${problems[*]}")"
fi
stamp_write last-restore-check  # backup-check.sh alerts when no restore check has succeeded
echo "$(ts) OK restore-check $summary"
