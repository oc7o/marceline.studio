# Shared helpers for pg-backup.sh and pg-restore-check.sh. Sourced, not executed.
# Credentials never leave the db container: client tools run inside it over the local socket as
# the container's $POSTGRES_USER, so no password is read, passed or printed here.

# cron starts with a minimal PATH. ~/.local/bin is where `uv` (used by the failure alert) and a
# manually installed `restic` usually live.
PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"

BACKUP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$BACKUP_LIB_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_DIR/infra/compose.yaml"

# Backup configuration lives in the repository .env like every other secret (CLAUDE.md: secrets
# only via environment variables, never committed). cron gives these scripts almost no
# environment, so read the few keys we own out of .env ourselves — whitelisted, never overriding
# what is already set, and deliberately not touching POSTGRES_*/DJANGO_* (those belong to compose
# and to Django, and the database credentials stay inside the db container).
BACKUP_ENV_KEYS=(
  BACKUP_DIR BACKUP_ALERT_TO BACKUP_ALERT_FROM BACKUP_ALERT_REPEAT_HOURS
  BACKUP_MAX_AGE_HOURS OFFHOST_MAX_AGE_HOURS RESTORE_CHECK_MAX_AGE_HOURS MAX_DUMP_MB
  RESTIC_REPOSITORY RESTIC_PASSWORD_FILE RESTIC_PASSWORD_COMMAND RESTIC_PASSWORD
  RESTIC_KEEP_LAST RESTIC_KEEP_DAILY RESTIC_KEEP_WEEKLY RESTIC_KEEP_MONTHLY
  BACKUP_SSH_KEY BACKUP_SSH_PORT RESTIC_BIN
  DELETION_JOURNAL_DIR
  CLAUDE_HOME_DIR CLAUDE_HOME_KEEP_LOCAL CLAUDE_HOME_MAX_MB CLAUDE_HOME_INCLUDE_TRANSCRIPTS
  CLAUDE_HOME_MAX_AGE_HOURS
)

load_backup_env() {
  local file="$REPO_DIR/.env" line key value wanted found
  if [[ ! -r "$file" ]]; then
    return 0
  fi
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"  # ltrim
    if [[ -z "$line" || "$line" == \#* || "$line" != *=* ]]; then
      continue
    fi
    key="${line%%=*}"
    key="${key#export }"
    key="${key//[[:space:]]/}"
    found=0
    for wanted in "${BACKUP_ENV_KEYS[@]}"; do
      if [[ "$key" == "$wanted" ]]; then
        found=1
        break
      fi
    done
    if ((found == 0)) || [[ -n "${!key:-}" ]]; then
      continue
    fi
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 && ( ${value:0:1} == '"' || ${value:0:1} == "'" ) \
      && ${value:0:1} == "${value: -1}" ]]; then
      value="${value:1:-1}"
    fi
    if [[ -n "$value" ]]; then
      export "$key=$value"
    fi
  done <"$file"
}
load_backup_env

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/marceline}"
# Timestamps of the last successful run of each job. A cron entry that stops firing produces no
# failing run to report, so "when did this last succeed" is the only thing that can catch it.
STATE_DIR="${STATE_DIR:-$BACKUP_DIR/state}"
DB_NAME="${DB_NAME:-marceline}"

# Tables that must be present in every dump; the human review work lives in matching_*.
KEY_TABLES=(
  django_migrations
  catalog_creator
  index_page
  index_media
  index_framehash
  matching_pairlabel
  matching_referenceleak
  matching_cluster
  matching_clustermember
  matching_reference
  matching_referenceframehash
)

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

human_size() { numfmt --to=iec-i --suffix=B --format='%.1f' "$1"; }

# Print the id of the running compose `db` container (or $DB_CONTAINER if set).
db_container() {
  if [[ -n "${DB_CONTAINER:-}" ]]; then
    printf '%s\n' "$DB_CONTAINER"
    return 0
  fi
  local id
  local -a env_args=()
  [[ -f "$REPO_DIR/.env" ]] && env_args=(--env-file "$REPO_DIR/.env")
  id="$(docker compose -f "$COMPOSE_FILE" "${env_args[@]}" ps -q db 2>/dev/null || true)"
  [[ -n "$id" ]] || return 1
  printf '%s\n' "$id"
}

# Run a Postgres client tool inside the db container as $POSTGRES_USER.
# in_db_i attaches stdin (for feeding a dump file).
in_db() {
  docker exec "$CONTAINER" sh -c 'export PGUSER="$POSTGRES_USER"; exec "$@"' sh "$@"
}
in_db_i() {
  docker exec -i "$CONTAINER" sh -c 'export PGUSER="$POSTGRES_USER"; exec "$@"' sh "$@"
}

# Dump files we own, oldest first (names sort chronologically).
list_dumps() {
  find "$BACKUP_DIR" -maxdepth 1 -type f -regextype posix-extended \
    -regex '.*/marceline-[0-9]{8}-[0-9]{6}\.dump' -printf '%f\n' | sort
}

# Curated ~/.claude snapshots we own (claude-home-backup.sh), oldest first.
list_claude_snapshots() {
  find "$BACKUP_DIR" -maxdepth 1 -type f -regextype posix-extended \
    -regex '.*/claude-home-[0-9]{8}-[0-9]{6}\.tar\.gz' -printf '%f\n' | sort
}

ensure_backup_dir() {
  if [[ ! -d "$BACKUP_DIR" ]]; then
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
  fi
}

# Serialize backup, rotation and restore-check runs. acquire_lock WAIT_SECONDS
acquire_lock() {
  ensure_backup_dir
  exec 9>"$BACKUP_DIR/.lock"
  flock -w "$1" 9 || fail "another backup or restore-check holds $BACKUP_DIR/.lock"
}

# --- Off-host copy (issue #39) -------------------------------------------------------------
# restic encrypts and authenticates every byte before it leaves the machine (repository format:
# AES-256-CTR + Poly1305). The dump carries flagged URLs and safety records, so it may only ever
# reach the Storage Box in that form. The password is never a literal here: restic reads it from
# RESTIC_PASSWORD_FILE / RESTIC_PASSWORD_COMMAND / RESTIC_PASSWORD, which come from .env, and
# nothing in these scripts ever prints it.

RESTIC_BIN="${RESTIC_BIN:-restic}"
# Retention on the off-host repository (docs/runbooks/backup-restore.md). Deliberately shorter
# than "forever": a snapshot is a copy of sensitive metadata.
RESTIC_KEEP_LAST="${RESTIC_KEEP_LAST:-3}"
RESTIC_KEEP_DAILY="${RESTIC_KEEP_DAILY:-7}"
RESTIC_KEEP_WEEKLY="${RESTIC_KEEP_WEEKLY:-4}"
RESTIC_KEEP_MONTHLY="${RESTIC_KEEP_MONTHLY:-6}"
RESTIC_TAG="${RESTIC_TAG:-marceline-db}"
# The deletion journal (ADR 0057, DPIA R24, issue #137): the append-only record of every
# irreversible privacy action, which lives **outside** the database precisely so that a restore
# cannot roll it back. It is pushed under its own tag, so its retention and its staleness are never
# mixed with the dump's — the same reason the ~/.claude snapshot has one. It must outlive every
# snapshot it could ever be replayed against, so its keep policy is deliberately generous.
DELETION_JOURNAL_TAG="${DELETION_JOURNAL_TAG:-marceline-deletion-journal}"

# Is the deletion journal configured, with a directory to push and to replay from?
journal_configured() {
  [[ -n "${DELETION_JOURNAL_DIR:-}" && -d "${DELETION_JOURNAL_DIR}" ]]
}

restic_configured() {
  [[ -n "${RESTIC_REPOSITORY:-}" ]]
}

restic_password_configured() {
  [[ -n "${RESTIC_PASSWORD_FILE:-}${RESTIC_PASSWORD_COMMAND:-}${RESTIC_PASSWORD:-}" ]]
}

# Run restic. The documented setup puts the Storage Box behind an ~/.ssh/config alias and needs no
# extra options; BACKUP_SSH_KEY/BACKUP_SSH_PORT are the explicit alternative for the sftp backend.
run_restic() {
  local -a opts=()
  if [[ -n "${BACKUP_SSH_KEY:-}" ]]; then
    opts+=(-o "sftp.args=-i ${BACKUP_SSH_KEY} -p ${BACKUP_SSH_PORT:-23} -o BatchMode=yes")
  fi
  "$RESTIC_BIN" "${opts[@]}" "$@"
}

# Drop the repository location and key path out of anything that gets mailed: a failure alert
# should say that the off-host copy failed, not hand over where it lives.
redact() {
  local text="$1"
  if [[ -n "${RESTIC_REPOSITORY:-}" ]]; then
    text="${text//${RESTIC_REPOSITORY}/<restic-repository>}"
  fi
  if [[ -n "${BACKUP_SSH_KEY:-}" ]]; then
    text="${text//${BACKUP_SSH_KEY}/<ssh-key>}"
  fi
  printf '%s\n' "$text"
}

# --- Run stamps ----------------------------------------------------------------------------

ensure_state_dir() {
  ensure_backup_dir
  if [[ ! -d "$STATE_DIR" ]]; then
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
  fi
}

stamp_write() {
  ensure_state_dir
  date +%s >"$STATE_DIR/$1"
}

# stamp_age_hours NAME -> prints whole hours since the stamp; returns 1 when there is no stamp.
stamp_age_hours() {
  local file="$STATE_DIR/$1" value
  if [[ ! -f "$file" ]]; then
    return 1
  fi
  value="$(cat "$file" 2>/dev/null || true)"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  printf '%s\n' $((($(date +%s) - value) / 3600))
}

# --- Failure alert (issue #39, crawler/backup_alerts.py) ------------------------------------
# Mails the operator through the same Django mailer as the safety alert (issue #40). Off unless
# BACKUP_ALERT_TO/FROM (or SAFETY_ALERT_TO/FROM) are set. Never fails the caller: the caller has
# already failed and still has to report that itself.
# alert SUBJECT BODY
alert() {
  local subject="$1" body status=0
  body="$(redact "$2")"
  if [[ -n "${BACKUP_ALERT_CMD:-}" ]]; then
    # Escape hatch for a non-Django transport, and the hook the restore drill uses to prove the
    # alert path fires without a mailbox.
    printf '%s\n' "$body" | "$BACKUP_ALERT_CMD" "$subject" || status=$?
  else
    printf '%s\n' "$body" |
      (cd "$REPO_DIR/backend" && uv run python manage.py backup_alert \
        --subject "$subject" --body-file -) || status=$?
  fi
  if ((status != 0)); then
    echo "$(ts) WARN alert could not be sent (exit $status): $subject"
  fi
  return 0
}

# alert_throttled KIND SUBJECT BODY — like alert, but at most one mail per KIND every
# BACKUP_ALERT_REPEAT_HOURS (default 24), so an overdue backup does not mail on every check.
alert_throttled() {
  local kind="$1" age
  local repeat="${BACKUP_ALERT_REPEAT_HOURS:-24}"
  if age="$(stamp_age_hours "last-alert-$kind")" && ((age < repeat)); then
    echo "$(ts) INFO alert suppressed for $kind (last one ${age}h ago, repeat ${repeat}h)"
    return 0
  fi
  alert "$2" "$3"
  stamp_write "last-alert-$kind"
}

# newest_snapshot_epoch JSON — the newest snapshot time in a `restic snapshots --json` answer,
# as a Unix epoch, or empty when none could be read.
#
# This exists because `restic snapshots --latest 1` does NOT return one snapshot. `--latest n`
# keeps the last n **per (host, paths, tags) group**, and every nightly dump has its own filename
# (marceline-<date>-<time>.dump), so each snapshot is its own group and all of them come back.
# The first entry is then the OLDEST, restic listing ascending by time. Reading it with `head -n 1`
# reported the age of the oldest surviving snapshot, which crossed the 36 h threshold on
# 2026-09-18 and mailed "OVERDUE off-host snapshot age=39h" while that night's push had in fact
# succeeded six hours earlier. Take the maximum, never the first.
newest_snapshot_epoch() {
  local json="$1" time epoch newest=""
  while IFS= read -r time; do
    [[ -n "$time" ]] || continue
    epoch="$(date -d "$time" +%s 2>/dev/null)" || continue
    if [[ -z "$newest" ]] || ((epoch > newest)); then
      newest="$epoch"
    fi
  done < <(grep -o '"time":"[^"]*"' <<<"$json" | cut -d'"' -f4)
  printf '%s' "$newest"
}
