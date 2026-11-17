from collections.abc import Iterator

import pytest
from django.db import connection, connections
from django.db.backends.base.base import BaseDatabaseWrapper

from core.locks import CRAWL_WORKER_LOCKS, MEDIA_WORKER_LOCKS, advisory_locks


@pytest.fixture
def other_session(db: None) -> Iterator[BaseDatabaseWrapper]:
    """A second database session, like a second worker process."""
    other = connections.create_connection("default")
    try:
        yield other
    finally:
        other.close()


def held_elsewhere(session: BaseDatabaseWrapper, space: int, key: int) -> bool:
    with session.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [space, key])
        row = cursor.fetchone()
    acquired = bool(row and row[0])
    if acquired:
        with session.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [space, key])
    return not acquired


def test_locks_are_held_for_the_block_and_released_after(
    other_session: BaseDatabaseWrapper,
) -> None:
    with advisory_locks(CRAWL_WORKER_LOCKS, [7, 8]) as busy:
        assert busy == []
        assert held_elsewhere(other_session, CRAWL_WORKER_LOCKS, 7)
        assert held_elsewhere(other_session, CRAWL_WORKER_LOCKS, 8)
        # A media worker for the same site is a different lock.
        assert not held_elsewhere(other_session, MEDIA_WORKER_LOCKS, 7)
    assert not held_elsewhere(other_session, CRAWL_WORKER_LOCKS, 7)


def test_a_taken_lock_is_reported_and_nothing_stays_locked(
    other_session: BaseDatabaseWrapper,
) -> None:
    with other_session.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [CRAWL_WORKER_LOCKS, 8])

    with advisory_locks(CRAWL_WORKER_LOCKS, [7, 8]) as busy:
        assert busy == [8]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = "
                "pg_backend_pid() AND classid = %s",
                [CRAWL_WORKER_LOCKS],
            )
            row = cursor.fetchone()
        assert row is not None and row[0] == 0  # the lock on 7 was given back at once
