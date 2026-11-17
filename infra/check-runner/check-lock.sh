#!/usr/bin/env bash
# The check-runner's queue (issue #122, docs/runbooks/check-runner.md): only one `make check` runs
# on the host at a time. Sourced by run-check.sh, and directly by this repo's own
# infra/check-runner/test-check-lock.sh, so the queueing behaviour has a test that needs neither
# the runner nor a real `make check`.
#
# **One slot, not N.** A single check phase already saturates the host by itself:
# backend/pyproject.toml's addopts run pytest with `-n auto --dist load` (pytest-xdist), one worker
# per core — 8 on the runner's 8 vCPUs — for the whole `test` phase, before `frontend-check`'s own
# vitest worker pool or `customer-check`'s Next.js production build even start. A second concurrent
# check does not share that work, it doubles it: 16 pytest-xdist workers scheduled onto 8 cores.
# That is exactly the state an agent found on 2026-09-18 (load average 27, three `run-check.sh`
# processes at once), and it is what turned a correct commit's lazily routed SPA screen (a dynamic
# `import()`, see `frontend/src/vitest.setup.ts`) into a `findByRole` timeout. Affording a second
# slot would mean turning pytest-xdist's own concurrency down first (a `pyproject.toml` change, not
# a queueing one) — if the operator wants that trade on a bigger box, widen `LOCK_FILES` below
# instead of guessing a slot count from nothing.
#
# The shared Postgres container backing every check (`compose.yaml`'s fixed project name) already
# gives each `CHECK_NAME` its own database, so two checks were never going to collide on *data* —
# this queue is about CPU and RAM, not correctness.
#
# `flock`'s own `-w` timeout drives the wait so a queued agent gets feedback instead of a command
# that just sits there (which is exactly what looked like "hung" to an agent watching this
# tonight). **No stale-lock cleanup is needed anywhere in here**: flock releases the lock the
# instant the holding process's file descriptor closes, which the kernel does unconditionally when
# that process exits, crashes or is killed — there is nothing left to clean up after it.

# acquire_check_lock <lock-dir> [<max-wait-s>] [<poll-s>]
#
# Blocks until the lock is granted, printing "waiting for the check lock (N ahead)" every
# <poll-s> seconds while it waits (default 30), or returns 1 after <max-wait-s> (default 45 min)
# with a clear message on stderr. On success, holds an open file descriptor (9) for the rest of
# the caller's process lifetime — that open fd *is* the lock, so the caller must not close fd 9 or
# exec into a subshell that would close it early.
acquire_check_lock() {
  local lock_dir="$1"
  local max_wait_s="${2:-2700}"
  local poll_s="${3:-30}"
  mkdir -p "$lock_dir"
  local lockfile="$lock_dir/check.lock"
  local queue_dir="$lock_dir/queue"
  mkdir -p "$queue_dir"

  # A token that sorts in arrival order, for the "N ahead" feedback only — flock (not this token)
  # decides who actually runs next. A nanosecond Unix epoch is a fixed-width decimal for any date
  # in this decade, so plain string comparison is also arrival-order comparison.
  local queue_token queue_file
  queue_token="$(date -u +%s%N)-$$"
  queue_file="$queue_dir/$queue_token"
  : > "$queue_file"
  # Removed once this call — queued *or* holding the lock — is fully done: the trap fires on the
  # whole process's exit, by which point the caller's own work (run-check.sh's `make check`) is
  # finished too, so a still-waiting sibling's "N ahead" count reflects every turn still ahead of
  # it, not just the ones still waiting.
  # shellcheck disable=SC2064  # intentionally expanding queue_file now, not at trap time
  trap "rm -f '$queue_file'" EXIT

  exec 9>"$lockfile"
  if flock -n 9; then
    return 0
  fi

  echo "waiting for the check lock ($(_check_lock_ahead "$queue_dir" "$queue_token" "$max_wait_s" "$poll_s") ahead)…"
  local waited=0
  while ! flock -x -w "$poll_s" 9; do
    waited=$((waited + poll_s))
    if [ "$waited" -ge "$max_wait_s" ]; then
      echo "check lock: gave up after $((max_wait_s / 60)) min ($(_check_lock_ahead "$queue_dir" "$queue_token" "$max_wait_s" "$poll_s") still ahead) — the runner is busy; try again later, or see who holds it with 'pgrep -af run-check' (docs/runbooks/check-runner.md)" >&2
      return 1
    fi
    echo "waiting for the check lock ($(_check_lock_ahead "$queue_dir" "$queue_token" "$max_wait_s" "$poll_s") ahead, ${waited}s elapsed)…"
  done
}

# _check_lock_ahead <queue-dir> <my-token> <max-wait-s> <poll-s>: how many other queue files sort
# before mine and are not stale. A queue file only outlives its process if that process was killed
# before its own EXIT trap could run it (SIGKILL, a dropped SSH session); ignoring anything older
# than the wait limit itself keeps one leaked entry from making every later caller believe it is
# queued behind a ghost forever.
_check_lock_ahead() {
  local queue_dir="$1" my_token="$2" max_wait_s="$3" poll_s="$4"
  local now ahead f base age
  now="$(date -u +%s)"
  ahead=0
  for f in "$queue_dir"/*; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    [ "$base" = "$my_token" ] && continue
    age=$(( now - $(stat -c %Y "$f" 2>/dev/null || echo "$now") ))
    if [ "$age" -le $(( max_wait_s + poll_s )) ] && [[ "$base" < "$my_token" ]]; then
      ahead=$((ahead + 1))
    fi
  done
  echo "$ahead"
}
