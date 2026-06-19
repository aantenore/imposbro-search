"""Fixed-window request limiting for data-plane API protection."""
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RateLimitConfigError(ValueError):
    """Raised when rate-limit settings are invalid."""


class RateLimitBackendError(RuntimeError):
    """Raised when the selected rate-limit backend cannot be used."""


@dataclass(frozen=True)
class RateLimitCheck:
    """Result of one fixed-window rate-limit decision."""

    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class FixedWindowRateLimiter:
    """
    Fixed-window limiter with Redis for distributed deployments.

    The in-memory backend is intentionally available for tests and single-process
    local runs only. Multi-replica deployments should use Redis so all Query API
    instances share the same counters.
    """

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis_client: Optional[redis.Redis] = None
        self._memory_windows: Dict[str, Tuple[int, int]] = {}

    def close(self) -> None:
        """Close the Redis client if it was opened."""
        if self._redis_client is not None:
            try:
                self._redis_client.close()
            except Exception as exc:
                logger.debug("Error closing rate-limit Redis client: %s", exc)
            self._redis_client = None

    def check(
        self,
        *,
        identity: str,
        action: str,
        collection: str,
        limit: int,
        window_seconds: int,
        backend: str,
        key_prefix: str,
    ) -> RateLimitCheck:
        """Increment the caller bucket and return whether the request is allowed."""
        if limit < 1:
            raise RateLimitConfigError("Rate-limit request limits must be >= 1")
        if window_seconds < 1:
            raise RateLimitConfigError("RATE_LIMIT_WINDOW_SECONDS must be >= 1")

        now = int(time.time())
        window_id = now // window_seconds
        reset_seconds = max(1, ((window_id + 1) * window_seconds) - now)
        bucket = self._bucket_key(
            key_prefix=key_prefix,
            identity=identity,
            action=action,
            collection=collection,
            window_id=window_id,
        )

        normalized_backend = backend.strip().lower()
        if normalized_backend == "redis":
            count = self._check_redis(bucket, window_seconds)
        elif normalized_backend == "memory":
            count = self._check_memory(bucket, window_id)
        else:
            raise RateLimitConfigError(
                "RATE_LIMIT_BACKEND must be either 'redis' or 'memory'"
            )

        allowed = count <= limit
        return RateLimitCheck(
            allowed=allowed,
            limit=limit,
            remaining=max(0, limit - count),
            reset_seconds=reset_seconds,
        )

    def _redis(self) -> redis.Redis:
        if self._redis_client is None:
            self._redis_client = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis_client

    def _check_redis(self, bucket: str, window_seconds: int) -> int:
        try:
            client = self._redis()
            pipe = client.pipeline()
            pipe.incr(bucket)
            pipe.expire(bucket, window_seconds * 2)
            count, _ = pipe.execute()
            return int(count)
        except RedisError as exc:
            raise RateLimitBackendError("Rate-limit Redis backend unavailable") from exc

    def _check_memory(self, bucket: str, window_id: int) -> int:
        if len(self._memory_windows) > 10000:
            stale_windows = [
                key
                for key, (stored_window_id, _count) in self._memory_windows.items()
                if stored_window_id < window_id - 1
            ]
            for key in stale_windows:
                self._memory_windows.pop(key, None)

        stored_window_id, count = self._memory_windows.get(bucket, (window_id, 0))
        if stored_window_id != window_id:
            count = 0
            stored_window_id = window_id
        count += 1
        self._memory_windows[bucket] = (stored_window_id, count)
        return count

    @staticmethod
    def _bucket_key(
        *,
        key_prefix: str,
        identity: str,
        action: str,
        collection: str,
        window_id: int,
    ) -> str:
        fingerprint = hashlib.sha256(
            f"{identity}\0{action}\0{collection}".encode("utf-8")
        ).hexdigest()
        prefix = key_prefix.strip() or "imposbro:rate_limit"
        return f"{prefix}:{action}:{window_id}:{fingerprint}"
