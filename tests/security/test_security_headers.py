from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))


class _LoggerStub:
    def info(self, *_args, **_kwargs):
        pass


logger_stub = types.ModuleType("backend.core.logger")
logger_stub.get_logger = lambda _name=None: _LoggerStub()
sys.modules.setdefault("backend.core.logger", logger_stub)

from backend.middleware.timing import (  # noqa: E402
    API_CONTENT_SECURITY_POLICY,
    HSTS_HEADER,
    PERMISSIONS_POLICY,
    SecurityHeadersMiddleware,
)


def _request(scheme: str = "http", forwarded_proto: str | None = None) -> Request:
    headers = []
    if forwarded_proto:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "method": "GET",
            "path": "/health",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


async def _call_middleware(request: Request, response: Response | None = None) -> Response:
    middleware = SecurityHeadersMiddleware(app=lambda _scope, _receive, _send: None)

    async def call_next(_request: Request) -> Response:
        return response or Response("ok")

    return await middleware.dispatch(request, call_next)


class SecurityHeadersTests(unittest.TestCase):
    def test_api_security_headers_are_added_without_obsolete_xss_header(self) -> None:
        result = asyncio.run(_call_middleware(_request()))

        self.assertEqual(result.headers["Content-Security-Policy"], API_CONTENT_SECURITY_POLICY)
        self.assertEqual(result.headers["Permissions-Policy"], PERMISSIONS_POLICY)
        self.assertEqual(result.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(result.headers["X-Frame-Options"], "DENY")
        self.assertEqual(result.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertNotIn("X-XSS-Protection", result.headers)

    def test_api_hsts_is_added_for_https_edge_requests(self) -> None:
        result = asyncio.run(_call_middleware(_request(forwarded_proto="https")))

        self.assertEqual(result.headers["Strict-Transport-Security"], HSTS_HEADER)

    def test_api_hsts_is_added_in_production(self) -> None:
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            result = asyncio.run(_call_middleware(_request()))

        self.assertEqual(result.headers["Strict-Transport-Security"], HSTS_HEADER)

    def test_api_removes_existing_obsolete_xss_header(self) -> None:
        response = Response("ok", headers={"X-XSS-Protection": "1; mode=block"})
        result = asyncio.run(_call_middleware(_request(), response=response))

        self.assertNotIn("X-XSS-Protection", result.headers)

    def test_frontend_vercel_headers_include_csp_hsts_and_permissions_policy(self) -> None:
        vercel_config = json.loads((ROOT / "frontend" / "vercel.json").read_text())
        headers = vercel_config["headers"][0]["headers"]
        header_map = {header["key"]: header["value"] for header in headers}

        csp = header_map["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self' 'unsafe-inline' https://fonts.googleapis.com", csp)
        self.assertIn("font-src 'self' https://fonts.gstatic.com data:", csp)
        self.assertIn("connect-src 'self' https:", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("upgrade-insecure-requests", csp)
        self.assertEqual(header_map["Strict-Transport-Security"], HSTS_HEADER)
        self.assertEqual(header_map["Permissions-Policy"], PERMISSIONS_POLICY)
        self.assertNotIn("X-XSS-Protection", header_map)


if __name__ == "__main__":
    unittest.main()
