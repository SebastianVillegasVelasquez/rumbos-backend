"""A small fixed-window rate limiter for `/auth/launch`.

State lives in this process's memory: it is lost on restart and is NOT shared
between workers, so with `WORKERS=n` the effective limit is up to n times the
configured one. That is acceptable for what it guards (the cost of verifying a
signature and minting a token); a shared limit belongs in the reverse proxy.
"""

import ipaddress
import math
import time
from collections.abc import Callable

WINDOW_SECONDS = 60
# Bounds memory against a spray of distinct keys; beyond it new keys are refused.
MAX_KEYS = 50_000


def client_key(host: str | None) -> str:
    """The limiter key of a client address. An IPv6 client can pick any address
    in its /64, so the whole /64 shares one key."""
    if not host:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False).network_address)
    return str(address)


class FixedWindowLimiter:
    def __init__(
        self,
        limit: int,
        *,
        window_seconds: int = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._counts: dict[str, tuple[int, int]] = {}  # key -> (window, hits)

    def hit(self, key: str) -> int:
        """Counts one request. Returns 0 if it is allowed, otherwise the seconds
        until the window it was refused in ends."""
        now = self._clock()
        window = int(now // self._window)
        current = self._counts.get(key)
        if current is None or current[0] != window:
            if current is None and len(self._counts) >= self._max_keys:
                self._prune(window)
                if len(self._counts) >= self._max_keys:
                    return self._window
            self._counts[key] = (window, 1)
            return 0
        if current[1] >= self._limit:
            return max(1, math.ceil((window + 1) * self._window - now))
        self._counts[key] = (window, current[1] + 1)
        return 0

    def _prune(self, window: int) -> None:
        self._counts = {k: v for k, v in self._counts.items() if v[0] == window}
