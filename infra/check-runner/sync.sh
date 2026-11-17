#!/usr/bin/env bash
# Push a local checkout to the check runner (docs/runbooks/check-runner.md). Prefers rsync
# (incremental, deletes files removed locally); falls back to a tar-over-ssh pipe when rsync isn't
# installed on the caller (it is not a listed prerequisite everywhere `make remote-check` might run
# from, and the runner must never be reached by `git clone`). Both paths read the same exclude list
# so there is exactly one place that decides what never leaves the caller's machine.
#
# **Both paths delete.** rsync does it with `--delete`; the tar path sends a manifest of what it
# extracted and removes every other file, skipping the excluded directory names so `node_modules`,
# the venv and the build caches survive between runs. It has to delete: a file that lingers after
# being renamed locally makes the check test something the tree no longer contains — on 2026-09-17
# the customer app's `middleware.ts` became `proxy.ts`, the old file stayed on the runner, and
# Next.js refused to build because both existed.
#
# Usage: sync.sh <local-dir> <ssh-host> <remote-dir>
set -euo pipefail

SRC="${1:?usage: sync.sh <local-dir> <ssh-host> <remote-dir>}"
HOST="${2:?}"
DEST="${3:?}"
EXCLUDES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/excludes.txt"

ssh "$HOST" "mkdir -p '$DEST'"

if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete --exclude-from="$EXCLUDES" "$SRC"/ "$HOST:$DEST"/
  exit 0
fi

echo "sync.sh: no local rsync, falling back to tar over ssh" >&2

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ARCHIVE="$WORK/tree.tar"
MANIFEST="$WORK/sent"

# Other agents write in this checkout while a sync runs, so tar exits 1 on "file changed as we
# read it" for files this check does not care about. Tolerate that one class, then insist the
# archive actually exists and is not empty, so a real tar failure still stops the sync.
set +e
tar -C "$SRC" --exclude-from="$EXCLUDES" --warning=no-file-changed -cf "$ARCHIVE" .
tar_status=$?
set -e
if [ ! -s "$ARCHIVE" ]; then
  echo "sync.sh: tar produced no archive (exit $tar_status)" >&2
  exit 1
fi
# Files only: a directory entry is not something to compare against `find -type f`.
tar -tf "$ARCHIVE" | grep -v '/$' >"$MANIFEST"
ssh "$HOST" "tar -xf - -C '$DEST'" <"$ARCHIVE"

# **Everything the exclude list names must be protected from the prune**, in all three forms it
# uses, because tar never sent those files and they would otherwise look stale. Getting this wrong
# deletes real state: an earlier version only handled the bare names and wiped `backend/.venv`,
# whose entry has a slash in it, so the next `make check` could not find `ruff`.
#
#   `node_modules`     a bare name, at any depth
#   `backend/.venv`    a path relative to the tree root
#   `*.pyc`            a filename glob
#
# Patterns are single-quoted so the remote shell hands them to `find` instead of globbing them.
KEEP_ARGS=""
while read -r name; do
  case "$name" in
    "" | \#*) continue ;;
    *\**) KEEP_ARGS="$KEEP_ARGS ! -name '$name'" ;;
    */*) KEEP_ARGS="$KEEP_ARGS ! -path './${name%/}/*'" ;;
    *) KEEP_ARGS="$KEEP_ARGS ! -path './$name/*' ! -path '*/$name/*'" ;;
  esac
done <"$EXCLUDES"

# Delete every file the runner has that this sync did not send — rsync `--delete` semantics, with
# the kept directories pruned out of the comparison.
# shellcheck disable=SC2029  # $DEST and $KEEP_ARGS are meant to expand locally
# One locale for the whole remote command: `sort` and `comm` must agree on collation or `comm`
# reports "input is not in sorted order" and its answer — the list of files to delete — cannot be
# trusted.
ssh "$HOST" "export LC_ALL=C; cd '$DEST' && sort >.sync-sent \
  && find . -type f ! -name '.sync-*' $KEEP_ARGS -print | sort >.sync-here \
  && comm -23 .sync-here .sync-sent | tr '\n' '\0' | xargs -0 -r rm -f \
  && rm -f .sync-sent .sync-here" <"$MANIFEST"
