import time
_last_message_time = {}

RATE_LIMIT_SECONDS = 5 

def check_rate_limit(session_id) -> bool:
    now = time.time()
    last_time = _last_message_time.get(session_id,0)
    if now - last_time <RATE_LIMIT_SECONDS:
        return False
    _last_message_time[session_id] = now
    return  True
def time_remaining(session_id: str) -> int:
    """
    Returns seconds remaining until the user can send the next message.
    """
    now = time.time()
    last_time = _last_message_time.get(session_id, 0)
    remaining = RATE_LIMIT_SECONDS - (now - last_time)
    return max(int(remaining), 0)