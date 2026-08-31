"""Distributed rate limiting and input-abuse checks."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from backend.core.logger import get_logger
from backend.core.redis_client import get_redis

log = get_logger(__name__)

SESSION_COOLDOWN_SECONDS = 5
IP_MAX_REQUESTS = 30
IP_WINDOW_SECONDS = 60
USER_AI_MAX_REQUESTS = 20
LOGIN_MAX_REQUESTS = 8
REGISTER_MAX_REQUESTS = 5
AUTH_WINDOW_SECONDS = 60
MAX_INPUT_LENGTH = 1_000
SPAM_WINDOW_SECONDS = 60
SPAM_MAX_IDENTICAL = 3

_local_windows: dict[str, deque] = defaultdict(deque)
_ip_message_hashes: dict[str, deque] = defaultdict(deque)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


def check_named_rate_limit(name: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Sliding-window limiter backed by Redis, with process-local fallback."""
    client = get_redis()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - window_seconds * 1000
    key = f"rate:{name}"

    if client is not None:
        member = f"{now_ms}:{hashlib.sha1(name.encode()).hexdigest()[:8]}"
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, start_ms)
        pipe.zcard(key)
        pipe.zadd(key, {member: now_ms})
        pipe.expire(key, window_seconds)
        _, count_before, _, _ = pipe.execute()
        if count_before >= limit:
            client.zrem(key, member)
            oldest = client.zrange(key, 0, 0, withscores=True)
            retry_after = window_seconds
            if oldest:
                retry_after = max(1, int((oldest[0][1] + window_seconds * 1000 - now_ms) / 1000))
            return RateLimitResult(False, 0, retry_after)
        return RateLimitResult(True, max(limit - count_before - 1, 0), 0)

    now = time.time()
    window = _local_windows[key]
    while window and window[0] < now - window_seconds:
        window.popleft()
    if len(window) >= limit:
        retry_after = max(1, int(window_seconds - (now - window[0])))
        return RateLimitResult(False, 0, retry_after)
    window.append(now)
    return RateLimitResult(True, max(limit - len(window), 0), 0)


def check_rate_limit(session_id: str) -> bool:
    return check_named_rate_limit(f"session:{session_id}", 1, SESSION_COOLDOWN_SECONDS).allowed


def time_remaining(session_id: str) -> int:
    key = f"rate:session:{session_id}"
    client = get_redis()
    now = time.time()
    if client is not None:
        oldest = client.zrange(key, 0, 0, withscores=True)
        if not oldest:
            return 0
        return max(0, int((oldest[0][1] / 1000 + SESSION_COOLDOWN_SECONDS) - now))
    window = _local_windows[key]
    if not window:
        return 0
    return max(0, int(SESSION_COOLDOWN_SECONDS - (now - window[0])))


def check_ip_rate_limit(ip: str) -> bool:
    result = check_named_rate_limit(f"ip:{ip}", IP_MAX_REQUESTS, IP_WINDOW_SECONDS)
    if not result.allowed:
        log.warning(f"IP rate-limited: {ip}")
    return result.allowed


def ip_requests_remaining(ip: str) -> int:
    return check_named_rate_limit(f"ip-status:{ip}", IP_MAX_REQUESTS, IP_WINDOW_SECONDS).remaining


def check_ai_user_rate_limit(user_id: str) -> RateLimitResult:
    return check_named_rate_limit(f"ai-user:{user_id}", USER_AI_MAX_REQUESTS, IP_WINDOW_SECONDS)


def check_login_rate_limit(identity: str) -> RateLimitResult:
    return check_named_rate_limit(f"login:{identity}", LOGIN_MAX_REQUESTS, AUTH_WINDOW_SECONDS)


def check_register_rate_limit(identity: str) -> RateLimitResult:
    return check_named_rate_limit(f"register:{identity}", REGISTER_MAX_REQUESTS, AUTH_WINDOW_SECONDS)


def validate_input(message: str) -> tuple[bool, str]:
    if not message or not message.strip():
        return False, "Message cannot be empty."
    if len(message) > MAX_INPUT_LENGTH:
        return False, f"Message too long ({len(message)} chars). Max is {MAX_INPUT_LENGTH}."
    return True, ""


def check_spam(ip: str, message: str) -> bool:
    now = time.time()
    msg_hash = hashlib.md5(message.strip().lower().encode()).hexdigest()
    client = get_redis()
    if client is not None:
        key = f"spam:{ip}:{msg_hash}"
        count = client.incr(key)
        client.expire(key, SPAM_WINDOW_SECONDS)
        if count > SPAM_MAX_IDENTICAL:
            log.warning(f"Spam detected from IP {ip}: '{message[:60]}...'")
            return True
        return False

    history = _ip_message_hashes[ip]
    while history and history[0][0] < now - SPAM_WINDOW_SECONDS:
        history.popleft()
    identical = sum(1 for _, h in history if h == msg_hash)
    if identical >= SPAM_MAX_IDENTICAL:
        log.warning(f"Spam detected from IP {ip}: '{message[:60]}...'")
        return True
    history.append((now, msg_hash))
    return False
