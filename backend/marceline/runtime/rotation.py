"""Fair round robin over the sites a lane serves (pure, thread-safe).

A thread `take`s the next site after the one handed out last that is neither busy (another thread
works on it: one task per site per lane at a time, so a site's own slot never has two threads of a
lane queueing for it) nor resting (its last claim found nothing, or failed). It `give_back`s the
site when done, with a rest when there was no work. A site with 20,000 queued rows therefore gets
one turn per round like a site with four, and cannot starve anyone.
"""

import threading
import time
from collections.abc import Callable, Iterable


class SiteRotation:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._next = 0
        self._busy: set[str] = set()
        self._rest_until: dict[str, float] = {}

    def set_sites(self, sites: Iterable[str]) -> None:
        """Serve exactly `sites` from now on (sorted); a removed busy site is just not handed out
        again. A newly added site is ready at once."""
        wanted = sorted(set(sites))
        with self._lock:
            current = self._order[self._next] if self._next < len(self._order) else None
            self._order = wanted
            self._next = wanted.index(current) if current in wanted else 0
            for site in list(self._rest_until):
                if site not in wanted:
                    del self._rest_until[site]

    def sites(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def take(self) -> str | None:
        """The next ready site, marked busy; None when every site is busy or resting."""
        now = self._clock()
        with self._lock:
            count = len(self._order)
            for offset in range(count):
                index = (self._next + offset) % count
                site = self._order[index]
                if site in self._busy or self._rest_until.get(site, 0.0) > now:
                    continue
                self._busy.add(site)
                self._next = (index + 1) % count
                return site
            return None

    def give_back(self, site: str, *, rest_s: float = 0.0) -> None:
        with self._lock:
            self._busy.discard(site)
            if rest_s > 0:
                self._rest_until[site] = self._clock() + rest_s
            else:
                self._rest_until.pop(site, None)

    def is_busy(self, site: str) -> bool:
        with self._lock:
            return site in self._busy

    def busy_count(self) -> int:
        with self._lock:
            return len(self._busy)

    def ready_in(self) -> float:
        """Seconds until some served, non-busy site stops resting (0 when one is ready now)."""
        now = self._clock()
        with self._lock:
            waits = [
                max(self._rest_until.get(site, 0.0) - now, 0.0)
                for site in self._order
                if site not in self._busy
            ]
        return min(waits) if waits else 1.0

    def all_resting(self) -> bool:
        """True when no site is busy and every site rests (a `--once` run is finished)."""
        now = self._clock()
        with self._lock:
            return not self._busy and all(
                self._rest_until.get(site, 0.0) > now for site in self._order
            )
