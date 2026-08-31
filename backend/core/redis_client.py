"""Redis client helpers with safe local fallback semantics."""

from __future__ import annotations

import os
from functools import lru_cache

import redis

from backend.core.logger import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_redis():
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.5)
        client.ping()
        return client
    except Exception as exc:
        log.warning(f"Redis unavailable, using local fallbacks where allowed: {exc}")
        return None


def redis_ready() -> bool:
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:
        return False
