"""
FastAPI middleware:
  1. RequestTimingMiddleware  — records latency, logs every request
  2. SecurityHeadersMiddleware — adds CSP / X-Frame-Options etc.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logger import get_logger
from core.metrics import record_request

log = get_logger(__name__)


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status, IP and duration."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.time()
        client_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )

        response: Response = await call_next(request)

        duration = time.time() - start
        success  = response.status_code < 500

        log.info(
            f"{request.method} {request.url.path} "
            f"status={response.status_code} ip={client_ip} "
            f"duration={duration:.3f}s"
        )
        record_request(request.url.path, duration, success)

        # expose timing to client (useful for debugging)
        response.headers["X-Response-Time-Ms"] = str(round(duration * 1000, 1))
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add basic security headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]          = "DENY"
        response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"]         = "1; mode=block"
        return response