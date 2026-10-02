"""Per-client rate limiting for the search endpoint.

Every search can spend shared, metered resources - an embedding call, the
LLM providers' free-tier quotas, Tavily credits on the web fallback - so
one caller in a loop could use them up for everyone. This caps how often a
single client may search.

In-process and in-memory on purpose: there is no shared store to put it in,
and the cost of that is bounded. Each Cloud Run instance counts on its own,
so a client spread across N instances gets up to N times the limit - still
a cap, which is the point - and counts reset when an instance is recycled.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from app.config import get_settings

logger = logging.getLogger("app.rate_limit")

# How often (in recorded requests) to drop clients that have gone quiet,
# so the table doesn't grow with every address that ever searched once.
_SWEEP_EVERY = 1000


class RateLimiter:
    """Sliding-window limits: at most `limit` requests per `window` seconds,
    for each (limit, window) pair, per client key.
    """

    def __init__(self, windows: list[tuple[int, float]]):
        self.windows = [(limit, window) for limit, window in windows if limit > 0]
        self._longest = max((window for _, window in self.windows), default=0.0)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._since_sweep = 0

    def check(self, key: str, now: float | None = None) -> float | None:
        """Record a request from `key`, or refuse it.

        Returns None if allowed, else how many seconds until it would be.
        A refused request is not recorded, so a client that keeps retrying
        through a refusal isn't pushed further out by its own retries.
        """
        if not self.windows:
            return None
        now = time.monotonic() if now is None else now

        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self._longest:
                hits.popleft()

            retry_after = 0.0
            for limit, window in self.windows:
                in_window = [t for t in hits if t > now - window]
                if len(in_window) >= limit:
                    # Allowed again once the oldest request that still
                    # counts against this window falls out of it.
                    retry_after = max(retry_after, in_window[-limit] + window - now)
            if retry_after > 0:
                return retry_after

            hits.append(now)
            self._since_sweep += 1
            if self._since_sweep >= _SWEEP_EVERY:
                self._sweep(now)
            return None

    def _sweep(self, now: float) -> None:
        self._since_sweep = 0
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= now - self._longest]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._since_sweep = 0


def client_key(request: Request) -> str:
    """Who is asking, as far as we can tell.

    In production every request reaches Cloud Run through Firebase Hosting
    and Google's front end, so the TCP peer is a proxy, never the user; the
    original client is the first X-Forwarded-For entry. That entry is
    whatever the client sent, though, so a caller set on evading the limit
    can rotate it - this stops the accidental loop and the casual abuser,
    not a determined one.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


_settings = get_settings()
search_limiter = RateLimiter(
    [
        (_settings.search_rate_limit_per_minute, 60.0),
        (_settings.search_rate_limit_per_hour, 3600.0),
    ]
)


def limit_search(request: Request) -> None:
    """FastAPI dependency: refuse a search over the caller's limit with a 429."""
    retry_after = search_limiter.check(client_key(request))
    if retry_after is None:
        return
    seconds = max(1, math.ceil(retry_after))
    logger.warning("rate_limited", extra={"path": request.url.path, "retry_after_s": seconds})
    raise HTTPException(
        status_code=429,
        detail=f"Too many searches. Please wait {seconds} seconds and try again.",
        headers={"Retry-After": str(seconds)},
    )
