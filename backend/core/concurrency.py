"""Per-session distributed concurrency control."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from backend.core.redis_client import get_redis

SESSION_LOCK_TTL_SECONDS = 120
_local_locks: dict[str, tuple[str, float]] = {}
_local_guard = threading.Lock()


class SessionBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionLock:
    key: str
    token: str
    redis_backed: bool


def acquire_session_lock(session_id: str, ttl_seconds: int = SESSION_LOCK_TTL_SECONDS) -> SessionLock:
    token = secrets.token_urlsafe(24)
    key = f"lock:chat-session:{session_id}"
    client = get_redis()
    if client is not None:
        acquired = client.set(key, token, nx=True, ex=ttl_seconds)
        if not acquired:
            raise SessionBusyError("Another message is already running for this session.")
        return SessionLock(key=key, token=token, redis_backed=True)

    now = time.time()
    with _local_guard:
        existing = _local_locks.get(key)
        if existing and existing[1] > now:
            raise SessionBusyError("Another message is already running for this session.")
        _local_locks[key] = (token, now + ttl_seconds)
    return SessionLock(key=key, token=token, redis_backed=False)


def release_session_lock(lock: SessionLock) -> None:
    if lock.redis_backed:
        client = get_redis()
        if client is None:
            return
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        end
        return 0
        """
        client.eval(script, 1, lock.key, lock.token)
        return

    with _local_guard:
        existing = _local_locks.get(lock.key)
        if existing and existing[0] == lock.token:
            _local_locks.pop(lock.key, None)
