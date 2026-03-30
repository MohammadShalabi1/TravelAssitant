"""
Structured logging with loguru.
Outputs JSON lines to stdout (for Railway / Render log drain)
and to a rotating file for local debugging.
"""

import sys
from loguru import logger

# ── Remove default handler ────────────────────────────────────────────────────
logger.remove()

# ── stdout – JSON lines (production-friendly) ─────────────────────────────────
logger.add(
    sys.stdout,
    level="INFO",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
        "{name}:{function}:{line} | {message}"
    ),
    serialize=False,   # set True to get pure JSON lines
    backtrace=True,
    diagnose=False,    # set False in prod (hides variable values from tracebacks)
)

# ── Rotating file (keep last 7 days) ─────────────────────────────────────────
logger.add(
    "logs/agent.log",
    level="DEBUG",
    rotation="1 day",
    retention="7 days",
    compression="zip",
    backtrace=True,
    diagnose=True,
    enqueue=True,       # non-blocking from async context
)


def get_logger(name: str):
    """Return a contextualised child logger."""
    return logger.bind(module=name)