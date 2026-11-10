"""Small in-process rate limiter for sign-in and link endpoints (one server process in phase 1)."""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque

MAX_KEYS = 50_000


def client_key(ip: str) -> str:
    """IPv6 addresses are grouped by /64 (one home or phone network hands out a whole /64)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def allow(self, key: str, limit: int, per_seconds: float, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            if t - self._last_sweep > 300 or len(self._hits) > MAX_KEYS:
                self._sweep(t)
            q = self._hits[key]
            while q and q[0] <= t - per_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            return True

    def _sweep(self, t: float) -> None:
        # windows are at most 10 minutes; anything older is gone
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= t - 600]:
            del self._hits[k]
        self._last_sweep = t

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
