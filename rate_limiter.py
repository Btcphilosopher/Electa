"""
Electa Systems — Rate Limiting
Sliding-window per-IP rate limiter. Two tiers:
  - POST /votes: 60 req/min (prevents ballot automation)
  - All other routes: 300 req/min
Returns HTTP 429 with Retry-After and X-RateLimit-* headers on breach.
"""

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int = 120, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window = window_seconds
        self._buckets: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def is_allowed(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window
        async with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            count = len(bucket)
            if count >= self.max_requests:
                return False, 0
            bucket.append(now)
            return True, self.max_requests - count - 1

    async def cleanup(self):
        now = time.monotonic()
        cutoff = now - self.window
        async with self._lock:
            dead = [k for k, v in self._buckets.items()
                    if not v or v[-1] < cutoff]
            for k in dead:
                del self._buckets[k]


vote_limiter    = SlidingWindowRateLimiter(max_requests=60,  window_seconds=60.0)
general_limiter = SlidingWindowRateLimiter(max_requests=300, window_seconds=60.0)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        ip = self._get_client_ip(request)
        is_vote_post = (request.url.path.startswith("/votes")
                        and request.method == "POST")
        limiter = vote_limiter if is_vote_post else general_limiter
        allowed, remaining = await limiter.is_allowed(ip)

        if not allowed:
            return Response(
                content='{"error":"rate_limit_exceeded",'
                        '"message":"Too many requests. Please slow down."}',
                status_code=429,
                headers={
                    "Content-Type": "application/json",
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(limiter.max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Window": "60",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"]     = str(limiter.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"]    = "60"
        return response

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        for header in ("X-Forwarded-For", "X-Real-IP"):
            value = request.headers.get(header)
            if value:
                return value.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
