"""Per-user request budgets, because authentication is not a quota.

`POST /chat` was authenticated from the day it existed, with the docstring reasoning that
an open endpoint "is an open invitation to drive someone else's GPU". That is right and
insufficient: a signed-in user can still hold an `agentic=true` tool loop open
back-to-back, and every upload costs a parse and an embedding pass per chunk. Auth
answers *who*, never *how much*.

A fixed-window counter, deliberately, rather than a token bucket: the window boundary is
visible in the `Retry-After` the caller receives, so "wait 34 seconds" is a true statement
rather than an estimate. Bursts at a window edge are the known cost and are acceptable at
these limits.

**Honest limitation.** This is in-process memory. Two Uvicorn workers mean two independent
budgets, and a restart clears every counter. That is fine for a workbench on one laptop
and is NOT fine behind a load balancer — the swap is a shared counter in Redis or
Postgres, with `hit()` keeping its signature. Stated here rather than discovered later.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, status


@dataclass(frozen=True)
class Limit:
    """`calls` requests per `window` seconds."""

    calls: int
    window: int

    def __str__(self) -> str:
        return f"{self.calls} per {self.window}s"


# The numbers, and why each is where it is:
#
#   chat        an analyst asking hard is ~1 question per 10s; 20/min is far above real
#               use and far below what it takes to saturate a local GPU.
#   agentic     the model-driven tool loop runs 1-2 minutes and is the expensive path,
#               so it gets its own, tighter budget on top of the chat one.
#   upload      parsing and embedding a 25 MB PDF is the heaviest write in the app.
#   review      approve/reject embeds on approval, so it is rate-limited as a write too.
LIMITS: dict[str, Limit] = {
    "chat": Limit(calls=20, window=60),
    "chat_agentic": Limit(calls=5, window=300),
    "upload": Limit(calls=10, window=600),
    "review": Limit(calls=60, window=600),
}

_lock = threading.Lock()
_counters: dict[tuple[str, str], tuple[int, float]] = {}     # (bucket, who) -> (n, start)


def hit(bucket: str, who: str) -> None:
    """Count one call; raise 429 with a truthful `Retry-After` when over budget.

    Locked because Uvicorn serves requests from a thread pool for sync endpoints, and a
    read-modify-write on a shared dict across threads is exactly the race that makes a
    limiter leak under the load it exists to handle.
    """
    limit = LIMITS[bucket]
    now = time.monotonic()
    key = (bucket, who)
    with _lock:
        count, start = _counters.get(key, (0, now))
        if now - start >= limit.window:                  # window rolled over
            count, start = 0, now
        count += 1
        _counters[key] = (count, start)
        over = count > limit.calls
        retry = max(1, math.ceil(limit.window - (now - start)))
    if over:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit reached for {bucket} ({limit}) — retry in {retry}s",
            headers={"Retry-After": str(retry)})


def reset() -> None:
    """Drop every counter. For tests, so one case cannot exhaust the next one's budget."""
    with _lock:
        _counters.clear()


def snapshot(who: str) -> dict[str, dict[str, int]]:
    """What this user has left in each bucket — surfaced so the UI can warn before a 429
    rather than after one."""
    now = time.monotonic()
    out: dict[str, dict[str, int]] = {}
    with _lock:
        for bucket, limit in LIMITS.items():
            count, start = _counters.get((bucket, who), (0, now))
            if now - start >= limit.window:
                count = 0
            out[bucket] = {"used": count, "limit": limit.calls,
                           "remaining": max(0, limit.calls - count),
                           "window_seconds": limit.window}
    return out
