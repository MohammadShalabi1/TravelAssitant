"""
FastAPI middleware:
  1. RequestTimingMiddleware  — records latency, logs every request
  2. SecurityHeadersMiddleware — adds browser security headers.
"""

from __future__ import annotations

import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.core.client_ip import get_client_ip
from backend.core.logger import get_logger
from backend.core.metrics import record_request

log = get_logger(__name__)

API_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'none'"
)
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
    "browsing-topics=()"
)
HSTS_HEADER = "max-age=31536000; includeSubDomains"


def _is_secure_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0]
    return (
        request.url.scheme == "https"
        or forwarded_proto.strip().lower() == "https"
        or os.getenv("APP_ENV", "local").strip().lower() in {"prod", "production"}
    )


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status, IP and duration."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.time()
        client_ip = get_client_ip(request)

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
        response.headers["Content-Security-Policy"] = API_CONTENT_SECURITY_POLICY
        response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if _is_secure_request(request):
            response.headers["Strict-Transport-Security"] = HSTS_HEADER
        if "X-XSS-Protection" in response.headers:
            del response.headers["X-XSS-Protection"]
        return response
