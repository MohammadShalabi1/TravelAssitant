"""
Rate limiting:
  - Per-session cooldown (original, kept)
  - Per-IP sliding-window limiter (new)
  - Max input length guard
  - Repeated-spam detection (same message 3× in 60 s → blocked)

All state is in-memory; swap the dicts for Redis in production
if you need multi-process safety.
"""

import time
import hashlib
from collections import defaultdict, deque

from backend.core.logger import get_logger

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
SESSION_COOLDOWN_SECONDS = 5        # per-session min gap between messages
IP_MAX_REQUESTS = 30                # per IP per window
IP_WINDOW_SECONDS = 60              # sliding window length
MAX_INPUT_LENGTH = 1_000            # chars
SPAM_WINDOW_SECONDS = 60            # window for identical-message detection
SPAM_MAX_IDENTICAL = 3              # block after this many identical messages

# ── State ─────────────────────────────────────────────────────────────────────
_last_session_time: dict[str, float] = {} #stores the last time a message was sent in a session
_ip_requests: dict[str, deque] = defaultdict(deque)   # ip → deque of timestamps
_ip_message_hashes: dict[str, deque] = defaultdict(deque)  # ip → deque of (ts, hash)


# ── Session cooldown (original) ───────────────────────────────────────────────

def check_rate_limit(session_id: str) -> bool:
    now = time.time()
    last = _last_session_time.get(session_id, 0)
    if now - last < SESSION_COOLDOWN_SECONDS:
        return False
    _last_session_time[session_id] = now
    return True


def time_remaining(session_id: str) -> int:
    now = time.time()
    last = _last_session_time.get(session_id, 0)
    return max(int(SESSION_COOLDOWN_SECONDS - (now - last)), 0)


# ── IP sliding-window limiter (new) ──────────────────────────────────────────

def check_ip_rate_limit(ip: str) -> bool:
    """
    Returns True if the request is allowed, False if the IP is over limit.
    Uses a sliding-window of timestamps.
    """
    now = time.time()
    window = _ip_requests[ip]

    # drop timestamps older than the window
    while window and window[0] < now - IP_WINDOW_SECONDS:
        window.popleft()

    if len(window) >= IP_MAX_REQUESTS:
        log.warning(f"IP rate-limited: {ip} ({len(window)} reqs in {IP_WINDOW_SECONDS}s)")
        return False

    window.append(now)
    return True


def ip_requests_remaining(ip: str) -> int:
    now = time.time()
    window = _ip_requests[ip]
    while window and window[0] < now - IP_WINDOW_SECONDS:
        window.popleft()
    return max(IP_MAX_REQUESTS - len(window), 0)


# ── Input validation ──────────────────────────────────────────────────────────

def validate_input(message: str) -> tuple[bool, str]:
    """
    Returns (ok, reason).  ok=True means the message passed all checks.
    """
    if not message or not message.strip():
        return False, "Message cannot be empty."

    if len(message) > MAX_INPUT_LENGTH:
        return False, f"Message too long ({len(message)} chars). Max is {MAX_INPUT_LENGTH}."

    return True, ""


# ── Spam / abuse detection (new) ─────────────────────────────────────────────

def check_spam(ip: str, message: str) -> bool:
    """
    Returns True if this looks like spam (same message sent ≥ SPAM_MAX_IDENTICAL
    times within SPAM_WINDOW_SECONDS from the same IP).
    """
    now = time.time()
    msg_hash = hashlib.md5(message.strip().lower().encode()).hexdigest()

    history = _ip_message_hashes[ip]

    # evict old entries
    while history and history[0][0] < now - SPAM_WINDOW_SECONDS:
        history.popleft()

    identical = sum(1 for _, h in history if h == msg_hash)

    if identical >= SPAM_MAX_IDENTICAL:
        log.warning(f"Spam detected from IP {ip}: '{message[:60]}...'")
        return True

    history.append((now, msg_hash))
    return False