#!/usr/bin/env bash
# Runs on the check-runner host only, inside a caller's synced checkout (/srv/checks/<name>/).
# Brings up the shared Postgres (infra/check-runner/compose.yaml), gives this check its own
# database, then runs `make check` against it the same way a worktree does on the development
# server (see the ENV_GUARD comment in the root Makefile): COMPOSE=true so the checked-out
# Makefile's own `db` target becomes a no-op, and POSTGRES_* pointed at this shared instance.
#
# Usage: run-check.sh <name>
set -euo pipefail

NAME="${1:?usage: run-check.sh <name>}"
DB="marceline_${NAME}"
PORT="${CHECK_RUNNER_PG_PORT:-5433}"
COMPOSE="docker compose -f infra/check-runner/compose.yaml"
DBLOCK="/srv/checks/.db.lock"

mkdir -p "$(dirname "$DBLOCK")"

# When this checkout was last actually used, for infra/check-runner/prune.sh. rsync keeps touching
# the directory itself, so its mtime says nothing about whether anyone still needs it; this stamp
# does. Every agent worktree leaves a full copy of the repository here when it finishes (51 of
# them, 45 GB, on 2026-09-18), and nothing had told them apart from a live one until now.
date -u +%s > "$PWD/.last-check"

# Queue behind every other check on this host before touching anything else (Postgres, npm,
# `make check` itself): issue #122, docs/runbooks/check-runner.md, and the reasoning in
# check-lock.sh for why one slot rather than N.
source infra/check-runner/check-lock.sh
acquire_check_lock /srv/checks/.locks || exit 1

# Idempotent; locked so parallel checks starting at once don't race the same `up`. The fixed
# project name in compose.yaml means this is the same container no matter which checkout asked.
# Redundant with the queue above now that only one check runs at a time, but harmless to keep as a
# second line of defence if the queue is ever widened to N slots.
flock "$DBLOCK" $COMPOSE up -d --wait db

# Fresh database per check: drop any stale test DB a previous crashed run left (Django's test
# runner uses "test_<NAME>"), then recreate the base one clean.
$COMPOSE exec -T db dropdb -U marceline --if-exists "test_${DB}"
$COMPOSE exec -T db dropdb -U marceline --if-exists "${DB}"
$COMPOSE exec -T db createdb -U marceline "${DB}"

# node_modules survives between runs of the same CHECK_NAME (the sync excludes it), so install when
# it is missing **or older than the lockfile it was installed from**. Missing-only was not enough:
# a run that changes a dependency would otherwise build against the previous one and report a green
# check for code nobody ran (2026-09-17, upgrading the customer app from Next 15.5 to 16.3).
needs_install() {
  local project="$1"
  [ ! -d "$project/node_modules" ] && return 0
  [ "$project/package-lock.json" -nt "$project/node_modules" ] && return 0
  return 1
}

if needs_install frontend; then
  make frontend-install
fi
# The Next.js customer app is a second npm project (ADR 0036), with its own node_modules.
if needs_install frontend/customer; then
  make customer-install
fi

export POSTGRES_HOST=localhost
export POSTGRES_PORT="$PORT"
export POSTGRES_DB="$DB"
export POSTGRES_USER=marceline
export POSTGRES_PASSWORD=marceline

make COMPOSE=true check
