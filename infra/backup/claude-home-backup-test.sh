#!/usr/bin/env bash
# Self-check for claude-home-backup.sh (issue: survivable agent memory). Builds a synthetic
# ~/.claude fixture — including a fake credentials file and a fake session-history file, each
# holding a marker string that must never appear anywhere in the script's output or in the
# archive it builds — and exercises the allowlist, the dry-run, the archive build, the
# transcripts opt-in, and the two "fail loudly" paths.
#
# No docker, no network, no real restic repository, and it never touches the real ~/.claude or
# the real backup directory: everything runs against temporary directories this script creates
# and removes.
#
#   infra/backup/claude-home-backup-test.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/claude-home-backup.sh"
SECRET_MARKER="SHOULD-NEVER-LEAVE-THIS-FIXTURE-$$"

tmp_root="$(mktemp -d)"
FIX="$tmp_root/claude-home"
BK="$tmp_root/backups"
cleanup() { rm -rf -- "$tmp_root"; }
trap cleanup EXIT

failures=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; failures=$((failures + 1)); }

# assert_contains OUTPUT NEEDLE LABEL
assert_contains() {
  if grep -qF -- "$2" <<<"$1"; then pass "$3"; else fail "$3 (expected to find: $2)"; fi
}
assert_not_contains() {
  if grep -qF -- "$2" <<<"$1"; then fail "$3 (must NOT contain: $2)"; else pass "$3"; fi
}

build_fixture() {
  rm -rf "$FIX" "$BK"
  mkdir -p "$FIX" "$BK"
  echo '{"theme":"dark"}' >"$FIX/settings.json"
  printf '{"accessToken":"%s"}\n' "$SECRET_MARKER" >"$FIX/.credentials.json"
  chmod 600 "$FIX/.credentials.json"
  printf '{"pastedContents":"%s"}\n' "$SECRET_MARKER" >"$FIX/history.jsonl"
  mkdir -p "$FIX/agents" "$FIX/commands" "$FIX/skills"
  echo "agent body" >"$FIX/agents/foo.md"
  echo "cmd body" >"$FIX/commands/bar.md"
  echo "skill body" >"$FIX/skills/baz.md"
  mkdir -p "$FIX/projects/-home-operator-marceline/memory"
  echo "# MEMORY" >"$FIX/projects/-home-operator-marceline/memory/MEMORY.md"
  echo "note body" >"$FIX/projects/-home-operator-marceline/memory/note1.md"
  printf '{"transcript":"line","marker":"%s"}\n' "$SECRET_MARKER" \
    >"$FIX/projects/-home-operator-marceline/sess123.jsonl"
  mkdir -p "$FIX/sessions" "$FIX/cache"
  printf '%s\n' "$SECRET_MARKER" >"$FIX/sessions/runtime.key"
  echo "cache" >"$FIX/cache/x.json"
}

run_dry() { CLAUDE_HOME_DIR="$FIX" BACKUP_DIR="$BK" "$TARGET" --dry-run 2>&1; }
run_real() { CLAUDE_HOME_DIR="$FIX" BACKUP_DIR="$BK" "$TARGET" 2>&1; }

echo "=== 1. dry-run lists exactly the allowlisted files ==="
build_fixture
out="$(run_dry)"; rc=$?
[[ $rc -eq 0 ]] && pass "dry-run against a healthy fixture exits 0" ||
  fail "dry-run against a healthy fixture exited $rc"
assert_contains "$out" "agents/foo.md" "dry-run lists agents/"
assert_contains "$out" "commands/bar.md" "dry-run lists commands/"
assert_contains "$out" "skills/baz.md" "dry-run lists skills/"
assert_contains "$out" "settings.json" "dry-run lists settings.json"
assert_contains "$out" "memory/MEMORY.md" "dry-run lists the memory index"
assert_contains "$out" "memory/note1.md" "dry-run lists a memory note"
assert_not_contains "$out" ".credentials" "dry-run excludes .credentials.json"
assert_not_contains "$out" "history.jsonl" "dry-run excludes history.jsonl"
assert_not_contains "$out" "sess123.jsonl" "dry-run excludes transcripts by default"
assert_not_contains "$out" "sessions/" "dry-run excludes sessions/"
assert_not_contains "$out" "cache/" "dry-run excludes cache/"
assert_not_contains "$out" "$SECRET_MARKER" "dry-run never prints the secret marker"
[[ ! -e "$BK" || -z "$(find "$BK" -maxdepth 1 -name 'claude-home-*.tar.gz')" ]] &&
  pass "dry-run writes no archive" || fail "dry-run wrote an archive"

echo
echo "=== 2. real build produces a clean archive ==="
build_fixture
out="$(run_real)"; rc=$?
[[ $rc -eq 0 ]] && pass "real build against a healthy fixture exits 0" ||
  fail "real build against a healthy fixture exited $rc: $out"
tarball="$(find "$BK" -maxdepth 1 -name 'claude-home-*.tar.gz' | head -n 1)"
if [[ -n "$tarball" ]]; then
  pass "an archive was written"
  toc="$(tar -tzf "$tarball" 2>&1)"
  assert_contains "$toc" "memory/MEMORY.md" "archive contains the memory index"
  assert_not_contains "$toc" "credentials" "archive contains no credentials path"
  assert_not_contains "$toc" "history.jsonl" "archive contains no history.jsonl"
  assert_not_contains "$toc" "sess123.jsonl" "archive contains no transcript by default"
  raw="$(tar -xzOf "$tarball" 2>/dev/null)"
  assert_not_contains "$raw" "$SECRET_MARKER" "archive bytes never contain the secret marker"
else
  fail "no archive was written by the real build"
fi

echo
echo "=== 3. CLAUDE_HOME_INCLUDE_TRANSCRIPTS=1 opts in, explicitly ==="
build_fixture
out="$(CLAUDE_HOME_DIR="$FIX" BACKUP_DIR="$BK" CLAUDE_HOME_INCLUDE_TRANSCRIPTS=1 "$TARGET" 2>&1)"
tarball="$(find "$BK" -maxdepth 1 -name 'claude-home-*.tar.gz' | head -n 1)"
if [[ -n "$tarball" ]]; then
  toc="$(tar -tzf "$tarball" 2>&1)"
  assert_contains "$toc" "sess123.jsonl" "opt-in run includes the transcript"
else
  fail "opt-in run did not produce an archive"
fi

echo
echo "=== 4. fails loudly when the memory group is empty ==="
build_fixture
rm -rf "$FIX/projects/-home-operator-marceline/memory"
out="$(run_dry)"; rc=$?
[[ $rc -ne 0 ]] && pass "dry-run refuses when memory is missing (exit $rc)" ||
  fail "dry-run exited 0 with no memory files"
assert_contains "$out" "memory group" "the failure names the memory group"
out="$(run_real)"; rc=$?
[[ $rc -ne 0 ]] && pass "real build refuses when memory is missing (exit $rc)" ||
  fail "real build exited 0 with no memory files"
[[ -z "$(find "$BK" -maxdepth 1 -newer "$FIX/settings.json" -name 'claude-home-*.tar.gz' 2>/dev/null)" ]] ||
  fail "real build wrote an archive despite the missing memory group"

echo
echo "=== 5. fails loudly on a nonexistent CLAUDE_HOME_DIR ==="
out="$(CLAUDE_HOME_DIR="$tmp_root/does-not-exist" BACKUP_DIR="$BK" "$TARGET" --dry-run 2>&1)"
rc=$?
[[ $rc -ne 0 ]] && pass "dry-run refuses a nonexistent CLAUDE_HOME_DIR (exit $rc)" ||
  fail "dry-run exited 0 for a nonexistent CLAUDE_HOME_DIR"

echo
if ((failures == 0)); then
  echo "OK claude-home-backup-test: all checks passed"
  exit 0
fi
echo "FAIL claude-home-backup-test: $failures check(s) failed"
exit 1
