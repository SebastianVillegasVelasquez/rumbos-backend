"""Process-local counters of what the exam engine did: attempts started,
submitted and expired, and closed attempts by band. They are logged with the
events and readable here; per-worker, like the rate limiter. The admin CLI's
`stats` reads the database for totals across workers. Preview is counted
apart."""

from collections import Counter
from threading import Lock


class Metrics:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def count(self, event: str, purpose: str, *, preview: bool) -> None:
        with self._lock:
            self._counts[f"{event}.{purpose}.{'preview' if preview else 'real'}"] += 1

    def count_band(self, band: str, *, preview: bool) -> None:
        with self._lock:
            self._counts[f"band.{band}.{'preview' if preview else 'real'}"] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


METRICS = Metrics()
