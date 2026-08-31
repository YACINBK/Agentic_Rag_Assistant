"""M12 — per-user rate limiting on search (Redis fixed-window counter).

Key: ratelimit:{user_id}:{window_start} — INCR on every request, EXPIRE set on
the first hit of a window. The window is fixed (not sliding): a burst of N at
t=0 and N more at t=window-ε is allowed once the window flips — acceptable for
an internal assistant whose budget is "don't let one user hammer the LLM".

Stateless on our side: the counter lives entirely in Redis, so multiple uvicorn
workers share one budget per user.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis

RATE_LIMIT_PREFIX = "ratelimit:"


async def is_allowed(
    redis: Redis,
    *,
    user_id: str,
    max_requests: int,
    window_seconds: int,
) -> bool:
    window_start = int(time.time()) // window_seconds
    key = f"{RATE_LIMIT_PREFIX}{user_id}:{window_start}"

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return count <= max_requests
