"""Postgres advisory locks that keep long-running workers single-instance.

Session-level locks: they are held by the database connection and released when the process
exits or its connection closes, so a crashed worker never leaves a stale lock behind.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from django.db import connection

# Two-key advisory lock spaces (`pg_try_advisory_lock(space, id)`), one per kind of worker, so a
# crawler and a media worker for the same site do not exclude each other. The scheduler's
# single-key lock (73428200) lives in a different key space altogether.
CRAWL_WORKER_LOCKS = 73428300
MEDIA_WORKER_LOCKS = 73428301
# One crawl runtime per lane (ADR 0030): key = the lane's number (`RUNTIME_LANE_KEYS`).
RUNTIME_LOCKS = 73428302
RUNTIME_LANE_KEYS = {"pages": 1, "media": 2}
# One upload fingerprinting worker at a time (issue #59, ADR 0044): the decode is CPU-bound and
# the box has one core to spare, so a second container exits instead of fighting for it.
UPLOAD_WORKER_LOCKS = 73428303


def try_advisory_lock(space: int, key: int) -> bool:
    """Take one session-level lock on this thread's connection; False when someone else holds it.

    Locks are re-entrant per session: a key this connection already holds is taken again (and must
    be unlocked as often), so callers track what they hold.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [space, key])
        row = cursor.fetchone()
    return bool(row and row[0])


def advisory_unlock(space: int, key: int) -> None:
    _unlock(space, [key])


def held_advisory_locks(space: int) -> set[int]:
    """The keys in `space` this thread's connection holds right now (read from `pg_locks`).

    A long-running holder checks this: a connection that was reset lost its locks silently.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT objid FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid = pg_backend_pid() AND classid = %s AND objsubid = 2",
            [space],
        )
        return {int(row[0]) for row in cursor.fetchall()}


@contextmanager
def advisory_locks(space: int, ids: Iterable[int]) -> Iterator[list[int]]:
    """Try to lock every id in `space` for the whole block; yields the ids someone else holds.

    All or nothing: when any id is taken, the locks acquired so far are released again before the
    block runs, and the caller is expected to stop. An empty list means every lock is held here.
    """
    acquired: list[int] = []
    busy: list[int] = []
    try:
        with connection.cursor() as cursor:
            for key in sorted(set(ids)):
                cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [space, key])
                row = cursor.fetchone()
                (acquired if row and row[0] else busy).append(key)
        if busy:
            _unlock(space, acquired)
            acquired = []
        yield busy
    finally:
        _unlock(space, acquired)


def _unlock(space: int, ids: Iterable[int]) -> None:
    ids = list(ids)
    if not ids:
        return
    with connection.cursor() as cursor:
        for key in ids:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [space, key])
