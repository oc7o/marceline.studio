#!/usr/bin/env bash
# Exercises check-lock.sh's queue behaviour (issue #122) without the runner or a real `make
# check`: two background invocations of acquire_check_lock against a scratch lock directory, one
# holding the lock while the other queues behind it, and a third that times out because the first
# is still running. Not part of `make check` (it is a manual sanity check for the shell logic, not
# a pytest/vitest suite); run it by hand: `bash infra/check-runner/test-check-lock.sh`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
# shellcheck source=infra/check-runner/check-lock.sh
source infra/check-runner/check-lock.sh

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
LOCKS="$SCRATCH/locks"

fail() { echo "FAIL: $1" >&2; exit 1; }

echo "--- holder: acquires immediately, holds the lock for 4s ---"
HOLDER_LOG="$SCRATCH/holder.log"
(
  start=$(date +%s)
  acquire_check_lock "$LOCKS" 30 1 >"$HOLDER_LOG" 2>&1 || exit 1
  echo "holder: acquired at +$(( $(date +%s) - start ))s" >>"$HOLDER_LOG"
  sleep 4
  echo "holder: releasing at +$(( $(date +%s) - start ))s" >>"$HOLDER_LOG"
) &
holder_pid=$!

sleep 1 # let the holder grab the lock first

echo "--- impatient: queues behind the holder, gives up after 2s ---"
IMPATIENT_LOG="$SCRATCH/impatient.log"
impatient_start=$(date +%s)
if acquire_check_lock "$LOCKS" 2 1 >"$IMPATIENT_LOG" 2>&1; then
  fail "the impatient caller should not have gotten the lock while the holder is running"
fi
impatient_elapsed=$(( $(date +%s) - impatient_start ))
grep -q "waiting for the check lock" "$IMPATIENT_LOG" || fail "no waiting message: $(cat "$IMPATIENT_LOG")"
grep -q "1 ahead" "$IMPATIENT_LOG" || fail "expected exactly one ahead (the holder): $(cat "$IMPATIENT_LOG")"
grep -q "gave up" "$IMPATIENT_LOG" || fail "expected a 'gave up' message: $(cat "$IMPATIENT_LOG")"
[ "$impatient_elapsed" -ge 2 ] || fail "gave up too fast ($impatient_elapsed s), it should have waited out its 2s budget"
echo "ok: gave up after ${impatient_elapsed}s with $(grep -o '[0-9]* ahead' "$IMPATIENT_LOG" | tail -1)"

echo "--- patient: queues behind the holder, waits it out and then runs ---"
PATIENT_LOG="$SCRATCH/patient.log"
patient_start=$(date +%s)
acquire_check_lock "$LOCKS" 30 1 >"$PATIENT_LOG" 2>&1 || fail "the patient caller should have gotten the lock once the holder released it"
patient_elapsed=$(( $(date +%s) - patient_start ))
# Whole-second timestamps make an elapsed-time lower bound flaky by design in a few-second test
# window (the holder can release mid-second); the message is the real, non-racy proof that this
# call did not just walk straight past a free lock — `flock -n` alone would print nothing.
grep -q "waiting for the check lock" "$PATIENT_LOG" || fail "no waiting message, so this never actually queued: $(cat "$PATIENT_LOG")"
echo "ok: queued, then acquired after ~${patient_elapsed}s"

wait "$holder_pid"
echo "--- holder log ---"
cat "$HOLDER_LOG"

echo
echo "PASS"
