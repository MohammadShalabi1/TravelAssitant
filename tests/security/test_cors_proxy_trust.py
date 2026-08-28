from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

from backend.core import client_ip  # noqa: E402


def _request(
    client_host: str,
    forwarded_for: str | None = None,
    forwarded: str | None = None,
) -> Request:
    headers = []
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    if forwarded:
        headers.append((b"forwarded", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "headers": headers,
            "client": (client_host, 12345),
        }
    )


class CorsProxyTrustTests(unittest.TestCase):
    def test_direct_request_ignores_spoofed_x_forwarded_for(self) -> None:
        with patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "10.0.0.0/8"}, clear=False):
            request = _request("203.0.113.10", forwarded_for="198.51.100.7")

            self.assertEqual(client_ip.get_client_ip(request), "203.0.113.10")

    def test_trusted_proxy_uses_nearest_untrusted_forwarded_ip(self) -> None:
        with patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "10.0.0.0/8"}, clear=False):
            request = _request(
                "10.0.0.5",
                forwarded_for="198.51.100.99, 203.0.113.42",
            )

            self.assertEqual(client_ip.get_client_ip(request), "203.0.113.42")

    def test_trusted_proxy_uses_forwarded_header_before_x_forwarded_for(self) -> None:
        with patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "10.0.0.0/8"}, clear=False):
            request = _request(
                "10.0.0.5",
                forwarded_for="198.51.100.99",
                forwarded='for="203.0.113.42";proto=https',
            )

            self.assertEqual(client_ip.get_client_ip(request), "203.0.113.42")

    def test_malformed_forwarded_header_falls_back_to_peer(self) -> None:
        with patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "10.0.0.0/8"}, clear=False):
            request = _request("10.0.0.5", forwarded_for="203.0.113.42, not-an-ip")

            self.assertEqual(client_ip.get_client_ip(request), "10.0.0.5")

    def test_local_cors_defaults_are_explicit_and_restricted(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "local",
                "ALLOWED_ORIGINS": "",
                "FRONTEND_URL": "",
            },
            clear=False,
        ):
            config = client_ip.get_cors_config()

        self.assertEqual(
            config.allow_origins,
            ["http://localhost:5173", "http://localhost:3000"],
        )
        self.assertEqual(config.allow_methods, ["GET", "POST", "PATCH", "DELETE", "OPTIONS"])
        self.assertEqual(config.allow_headers, ["Authorization", "Content-Type", "X-CSRF-Token"])
        self.assertNotIn("*", config.allow_origins)
        self.assertNotIn("*", config.allow_methods)
        self.assertNotIn("*", config.allow_headers)

    def test_production_cors_requires_explicit_origin(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ALLOWED_ORIGINS": "",
                "FRONTEND_URL": "",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                client_ip.get_cors_config()

    def test_production_cors_rejects_wildcard_origin(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ALLOWED_ORIGINS": "*",
                "FRONTEND_URL": "",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                client_ip.get_cors_config()

    def test_production_cors_combines_allowed_origins_and_frontend_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ALLOWED_ORIGINS": "https://app.example.com, https://admin.example.com",
                "FRONTEND_URL": "https://app.example.com",
            },
            clear=False,
        ):
            config = client_ip.get_cors_config()

        self.assertEqual(
            config.allow_origins,
            ["https://app.example.com", "https://admin.example.com"],
        )


if __name__ == "__main__":
    unittest.main()
