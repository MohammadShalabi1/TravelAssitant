from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendAuthStaticTests(unittest.TestCase):
    def test_frontend_does_not_persist_access_token_in_local_storage(self):
        auth_context = (ROOT / "frontend" / "src" / "lib" / "AuthContext.jsx").read_text(
            encoding="utf-8"
        )
        api_client = (ROOT / "frontend" / "src" / "lib" / "api.js").read_text(
            encoding="utf-8"
        )

        combined = auth_context + api_client

        self.assertNotIn('localStorage.setItem("token"', combined)
        self.assertNotIn('localStorage.getItem("token"', combined)
        self.assertIn("let accessToken = null", api_client)
        self.assertIn("refreshAuth", api_client)
        self.assertIn("credentials: \"include\"", api_client)
        self.assertIn("X-CSRF-Token", api_client)


if __name__ == "__main__":
    unittest.main()
