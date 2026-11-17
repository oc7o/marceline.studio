"""Database-backed host slots: politeness shared by every thread and process (ADR 0030 § 2).

`DbHostGate` is the `core.http.HostGate` of the crawl runtime. One `core.HostSlot` row per host
key (`core.http.host_key`: `www.` removed) holds when the host may be asked next, a lease for the
request in flight, a host-wide penalty and a marker for lane alternation. Every attempt — page,
robots.txt, retry, redirect hop, media — goes through it, so a site's own server sees one request
per delay whichever lane or process sends it, and two sites sharing a CDN host are limited once.

Per attempt (see `core.http.PoliteClient._wait_turn`):

1. `acquire`: one conditional `UPDATE` takes the lease when the host is free — the previous start
   is at least `max(delay, min_delay_s)` ago and at least the previous request's own delay ago
   (the stricter of two callers wins), no lease is live, no penalty runs, and the other lane is not
   waiting for its turn after this lane had the last one. Otherwise the lane marks itself waiting,
   sleeps until the earliest moment the host could be free (in steps of at most `poll_max_s`) and
   tries again. After `max_wait_s` — or at once, when a penalty or delay ends later than that —
   `HostBusyError` names when to try again.
2. The caller's checks run (safety deny, robots, `public_only`, budget) while the lease is held.
3. `start` stamps the start on the database clock; `cancel` (a check refused) releases the lease
   and leaves the slot's times untouched, so a refused attempt costs the host nothing.
4. `done` releases the lease once the response is read or failed.

All times come from `clock_timestamp()`: `now()` is the transaction start and container clocks
may drift. Statements are short and run in autocommit (a savepoint inside a caller's transaction),
so no row lock is held while a thread sleeps or a request runs; the lease does that job and
expires by itself (`lease_s`, longer than any request's deadline) if its holder dies.

A thread that already holds a host's lease and asks for the same host again (a media host's
robots.txt fetched from inside that host's `before_request` check when the cached rules expired
in between) gets a nested turn that neither waits nor touches the row — the request goes out
under the outer lease, as it did with the in-memory gate.
"""

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection
from django.utils import timezone

from core.http import HostBusyError, HostTurn, host_key

logger = logging.getLogger(__name__)

DEFAULT_LEASE_S = 240.0
DEFAULT_MAX_WAIT_S = 150.0
POLL_MIN_S = 0.05
POLL_MAX_S = 1.0
#: A lease held by someone else ends when their request ends, which the row cannot predict.
LEASE_POLL_S = 0.2
PENALTY_MAX_S = 6 * 3600.0
REASON_MAX_LENGTH = 255

_TABLE = "core_hostslot"
Params = dict[str, str | float]

_INSERT = f"""
INSERT INTO {_TABLE} (key, next_allowed_at, last_delay_s, min_delay_s, lease_owner, last_lane,
                      waiting_lane, penalty_reason)
VALUES (%(key)s, clock_timestamp(), 0, 0, '', '', '', '')
ON CONFLICT (key) DO NOTHING
"""

_ACQUIRE = f"""
UPDATE {_TABLE}
   SET leased_until = clock_timestamp() + make_interval(secs => %(lease)s),
       lease_owner = %(owner)s,
       last_lane = %(lane)s,
       waiting_until = CASE WHEN waiting_lane = %(lane)s THEN NULL ELSE waiting_until END,
       waiting_lane = CASE WHEN waiting_lane = %(lane)s THEN '' ELSE waiting_lane END
 WHERE key = %(key)s
   AND next_allowed_at <= clock_timestamp()
   AND (last_started_at IS NULL
        OR last_started_at + make_interval(secs => greatest(%(delay)s, min_delay_s))
           <= clock_timestamp())
   AND (leased_until IS NULL OR leased_until < clock_timestamp())
   AND (penalty_until IS NULL OR penalty_until <= clock_timestamp())
   AND NOT (last_lane = %(lane)s AND waiting_lane <> '' AND waiting_lane <> %(lane)s
            AND waiting_until > clock_timestamp())
RETURNING key
"""

# Mark this lane as waiting, unless another lane's mark is still live.
_WAITING = f"""
UPDATE {_TABLE}
   SET waiting_lane = %(lane)s,
       waiting_until = clock_timestamp() + make_interval(secs => %(hold)s)
 WHERE key = %(key)s
   AND (waiting_lane = '' OR waiting_lane = %(lane)s OR waiting_until IS NULL
        OR waiting_until <= clock_timestamp())
"""

# Seconds until the delay and the penalty allow a request, whether someone else's lease is live,
# and the database time (for `HostBusyError.retry_at`).
_STATE = f"""
SELECT extract(epoch FROM greatest(
           next_allowed_at,
           coalesce(last_started_at + make_interval(secs => greatest(%(delay)s, min_delay_s)),
                    next_allowed_at),
           coalesce(penalty_until, next_allowed_at)
       ) - clock_timestamp()),
       leased_until IS NOT NULL AND leased_until >= clock_timestamp(),
       clock_timestamp()
  FROM {_TABLE}
 WHERE key = %(key)s
"""

_START = f"""
UPDATE {_TABLE}
   SET last_started_at = clock_timestamp(),
       next_allowed_at = clock_timestamp()
                         + make_interval(secs => greatest(%(delay)s, min_delay_s)),
       last_delay_s = %(delay)s
 WHERE key = %(key)s AND lease_owner = %(owner)s
"""

_RELEASE = f"""
UPDATE {_TABLE}
   SET leased_until = NULL, lease_owner = ''
 WHERE key = %(key)s AND lease_owner = %(owner)s
"""

_PENALIZE = f"""
UPDATE {_TABLE}
   SET penalty_until = greatest(coalesce(penalty_until, clock_timestamp()),
                                clock_timestamp() + make_interval(secs => %(seconds)s)),
       penalty_reason = %(reason)s
 WHERE key = %(key)s
"""


@dataclass(frozen=True)
class SlotWait:
    """One granted acquisition: how long it waited for `key`, for the runtime's metrics."""

    key: str
    lane: str
    waited_s: float


class _DbTurn:
    def __init__(self, gate: "DbHostGate", key: str) -> None:
        self._gate = gate
        self._key = key
        self._open = True

    def start(self, delay_s: float) -> None:
        self._gate._execute(
            _START, {"key": self._key, "owner": self._gate.owner, "delay": float(delay_s)}
        )

    def cancel(self) -> None:
        self._release()

    def done(self) -> None:
        self._release()

    def _release(self) -> None:
        if not self._open:
            return
        self._open = False
        self._gate._held().discard(self._key)
        self._gate._execute(_RELEASE, {"key": self._key, "owner": self._gate.owner})


class _NestedTurn:
    """A second turn at a host this thread already holds: no wait, no row change."""

    def start(self, delay_s: float) -> None:
        return None

    def cancel(self) -> None:
        return None

    def done(self) -> None:
        return None


class DbHostGate:
    """`core.http.HostGate` on `core.HostSlot` rows. One per `PoliteClient` (per thread).

    `lane` names the caller (`pages`, `media`, ...) for lane alternation; `lease_s` must exceed the
    longest request the client can make (its deadline plus the read timeout); `on_wait` receives a
    `SlotWait` for every granted turn; a wait ends with `HostBusyError` as soon as `cancelled()`
    says so (the runtime shutting down).
    """

    def __init__(
        self,
        lane: str,
        *,
        lease_s: float = DEFAULT_LEASE_S,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
        poll_max_s: float = POLL_MAX_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_wait: Callable[[SlotWait], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.lane = lane[:16]
        self.owner = (
            f"{self.lane}:{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )[:128]
        self._lease_s = lease_s
        self._max_wait_s = max_wait_s
        self._poll_max_s = poll_max_s
        self._sleep = sleep
        self._clock = clock
        self._on_wait = on_wait
        self._cancelled = cancelled
        self._local = threading.local()

    def acquire(self, host: str, delay_s: float) -> HostTurn:
        key = host_key(host)
        if not key:
            return _NestedTurn()
        held = self._held()
        if key in held:
            return _NestedTurn()
        delay_s = max(float(delay_s), 0.0)
        params: Params = {
            "key": key,
            "lane": self.lane,
            "owner": self.owner,
            "delay": delay_s,
            "lease": self._lease_s,
            "hold": max(2 * delay_s, 2.0),
        }
        started = self._clock()
        while True:
            if self._fetch(_ACQUIRE, params) is not None:
                held.add(key)
                if self._on_wait is not None:
                    self._on_wait(SlotWait(key, self.lane, self._clock() - started))
                return _DbTurn(self, key)
            state = self._fetch(_STATE, params)
            if state is None:
                self._execute(_INSERT, params)
                continue
            free_in, leased, db_now = float(str(state[0])), bool(state[1]), state[2]
            self._execute(_WAITING, params)
            waited = self._clock() - started
            if waited + max(free_in, 0.0) > self._max_wait_s or (
                leased and waited >= self._max_wait_s
            ):
                raise HostBusyError(
                    f"host slot busy: {key} not free within {self._max_wait_s:g} s",
                    host=key,
                    retry_at=_as_aware(db_now) + timedelta(seconds=max(free_in, POLL_MAX_S)),
                )
            if leased and free_in <= 0:
                pause = LEASE_POLL_S
            else:
                pause = min(max(free_in, POLL_MIN_S), self._poll_max_s)
            if self._cancelled is not None and self._cancelled():
                raise HostBusyError(
                    f"host slot wait for {key} cancelled: shutting down",
                    host=key,
                    retry_at=_as_aware(db_now),
                )
            self._sleep(pause)

    def penalize(self, host: str, seconds: float, reason: str) -> None:
        key = host_key(host)
        if not key or seconds <= 0:
            return
        params: Params = {
            "key": key,
            "seconds": min(float(seconds), PENALTY_MAX_S),
            "reason": reason[:REASON_MAX_LENGTH],
        }
        self._execute(_INSERT, params)
        self._execute(_PENALIZE, params)
        logger.info("host %s penalised for %.1f s: %s", key, seconds, reason)

    def _held(self) -> set[str]:
        held: set[str] | None = getattr(self._local, "held", None)
        if held is None:
            held = set()
            self._local.held = held
        return held

    def _fetch(self, sql: str, params: Params) -> tuple[object, ...] | None:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            row: tuple[object, ...] | None = cursor.fetchone()
        return row

    def _execute(self, sql: str, params: Params) -> None:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def _as_aware(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.now()
