#!/usr/bin/env bash
# Restore the newest snapshot from the ENCRYPTED OFF-HOST repository into a scratch database and
# verify it. This is what turns the off-host copy from a hope into a backup: it proves the
# repository is readable with the password we have, that the dump inside it is intact, and that
# Postgres accepts it.
#
#   infra/backup/restic-restore-check.sh                # newest snapshot
#   infra/backup/restic-restore-check.sh --snapshot ID  # a specific snapshot
#   infra/backup/restic-restore-check.sh --keep         # leave the scratch database in place
#
# Env: RESTIC_* and BACKUP_SSH_* (see restic-push.sh), DB_CONTAINER (default: the compose `db`
#      container), RESTORE_TMPDIR (default: a mktemp directory).
# Never touches the live database: it restores into marceline_offhost_check and drops it again.
# Full procedure, including restoring for real: docs/runbooks/backup-restore.md.
set -euo pipefail

# shellcheck source=infra/backup/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly RESTORE_DB="marceline_offhost_check"
# PDQ hashes are 32 bytes. Nothing else in this database is binary, and nothing else may become
# binary: if a bytea column ever holds more than this, media reached the database (CLAUDE.md) and
# the backup has been quietly carrying it off-host.
MAX_BINARY_BYTES="${MAX_BINARY_BYTES:-64}"

fail() {
  echo "$(ts) FAIL offhost-restore-check: $*"
  exit 1
}

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

snapshot="latest"
keep=0
while (($# > 0)); do
  case "$1" in
    --snapshot)
      snapshot="${2:-}"
      [[ -n "$snapshot" ]] || fail "--snapshot needs an id"
      shift 2
      ;;
    --keep)
      keep=1
      shift
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
done

target_is_safe() {
  [[ "$RESTORE_DB" == *_offhost_check && "$RESTORE_DB" != "$DB_NAME" ]]
}

drop_restore_db() {
  target_is_safe || return 1
  in_db dropdb --if-exists --force "$RESTORE_DB"
}

count_rows() {
  in_db env PGOPTIONS='-c default_transaction_read_only=on' \
    psql -X -At -v ON_ERROR_STOP=1 --dbname="$1" -c "SELECT count(*) FROM public.\"$2\"" 2>/dev/null
}

# Fails loudly if any bytea column holds more than MAX_BINARY_BYTES, i.e. if something that is not
# a fingerprint got stored.
media_tripwire() {
  in_db psql -X -At -v ON_ERROR_STOP=1 --dbname="$RESTORE_DB" -c "
DO \$\$
DECLARE r record; n bigint; worst bigint := 0; culprit text := '(none)';
BEGIN
  FOR r IN SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = 'public' AND data_type = 'bytea' LOOP
    EXECUTE format('SELECT coalesce(max(octet_length(%I)), 0) FROM public.%I',
                   r.column_name, r.table_name) INTO n;
    IF n > worst THEN worst := n; culprit := r.table_name || '.' || r.column_name; END IF;
  END LOOP;
  IF worst > ${MAX_BINARY_BYTES} THEN
    RAISE EXCEPTION 'binary column % holds % bytes (max ${MAX_BINARY_BYTES}): media in the database is a bug', culprit, worst;
  END IF;
  RAISE NOTICE 'widest binary value: % bytes in %', worst, culprit;
END \$\$;"
}

target_is_safe || fail "refusing to restore into $RESTORE_DB (live database is $DB_NAME)"
# Before the operator has a Storage Box there is nothing off-host to check. Skipping cleanly keeps
# the weekly cron entry harmless until then, instead of writing a FAIL line every Sunday.
if ! restic_configured; then
  echo "$(ts) SKIP offhost-restore-check: RESTIC_REPOSITORY is not set, there is no off-host copy yet"
  exit 0
fi
command -v "$RESTIC_BIN" >/dev/null 2>&1 || fail "restic is not installed"
restic_password_configured || fail "no restic password source is set (see restic-push.sh)"

start=$(date +%s)
acquire_lock 3600
CONTAINER="$(db_container)" || fail "no database container (compose service db is not running)"

tmp="${RESTORE_TMPDIR:-$(mktemp -d)}"
cleanup() {
  if [[ -n "${tmp:-}" && -d "$tmp" ]]; then
    rm -rf -- "$tmp"
  fi
  if ((keep == 0)); then
    drop_restore_db >/dev/null 2>&1 ||
      echo "$(ts) WARN offhost-restore-check: could not drop $RESTORE_DB"
  fi
}
trap cleanup EXIT
chmod 700 "$tmp"

run_restic restore "$snapshot" --tag "$RESTIC_TAG" --target "$tmp" ||
  fail "restic restore $snapshot failed (repository unreachable, wrong password, or no snapshot)"

dump="$(find "$tmp" -type f -name 'marceline-*.dump' | sort | tail -n 1)"
[[ -n "$dump" ]] || fail "the snapshot contains no marceline-*.dump"
size="$(stat -c %s "$dump")"
((size > 0)) || fail "the restored dump is empty"

toc="$(in_db_i pg_restore --list <"$dump")" || fail "pg_restore --list could not read the dump"
for table in "${KEY_TABLES[@]}"; do
  grep -Fq " TABLE DATA public $table " <<<"$toc" || fail "table $table missing from the dump"
done

drop_restore_db >/dev/null 2>&1 || fail "could not drop a leftover $RESTORE_DB"
in_db createdb --template=template0 "$RESTORE_DB" || fail "createdb $RESTORE_DB failed"
in_db_i pg_restore --no-owner --no-privileges --exit-on-error --dbname="$RESTORE_DB" <"$dump" ||
  fail "pg_restore into $RESTORE_DB failed"

problems=()
printf '%-30s %12s\n' table rows
for table in "${KEY_TABLES[@]}"; do
  rows="$(count_rows "$RESTORE_DB" "$table")" || rows="MISSING"
  [[ "$rows" =~ ^[0-9]+$ ]] || problems+=("$table: $rows")
  printf '%-30s %12s\n' "$table" "$rows"
done

migrations="$(count_rows "$RESTORE_DB" django_migrations || true)"
[[ "$migrations" =~ ^[1-9][0-9]*$ ]] ||
  problems+=("django_migrations is empty: this is not a marceline database")

media_tripwire || problems+=("binary column over $MAX_BINARY_BYTES bytes: media in the backup")

((${#problems[@]} == 0)) || fail "$(
  IFS=';'
  echo "${problems[*]}"
)"

stamp_write last-restore-check
echo "$(ts) OK offhost-restore-check snapshot=$snapshot dump=$(basename "$dump")" \
  "size=$(human_size "$size") migrations=$migrations tables=${#KEY_TABLES[@]}" \
  "duration=$(($(date +%s) - start))s"
