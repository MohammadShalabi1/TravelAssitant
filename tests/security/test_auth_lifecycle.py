from __future__ import annotations

import os
import sys
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Request, Response
from jose import jwt


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")


class _LoggerStub:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


logger_stub = types.ModuleType("backend.core.logger")
logger_stub.get_logger = lambda _name=None: _LoggerStub()
sys.modules.setdefault("backend.core.logger", logger_stub)

from backend.core import auth  # noqa: E402


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetchone(self):
        return self.result

    def execute(self, query, params=()):
        compact = " ".join(query.lower().split())
        if compact.startswith("select id from users where email"):
            email = params[0]
            user = self.db["users_by_email"].get(email)
            self.result = {"id": user["id"]} if user else None
        elif compact.startswith("insert into users"):
            email, password_hash = params
            user_id = self.db["next_user_id"]
            self.db["next_user_id"] += 1
            user = {"id": user_id, "email": email, "password_hash": password_hash}
            self.db["users_by_email"][email] = user
            self.db["users_by_id"][user_id] = user
            self.result = {"id": user_id}
        elif compact.startswith("select id, password_hash from users where email"):
            email = params[0]
            user = self.db["users_by_email"].get(email)
            self.result = {"id": user["id"], "password_hash": user["password_hash"]} if user else None
        elif compact.startswith("insert into auth_sessions"):
            user_id, refresh_hash, csrf_hash, expires_at = params
            session_id = self.db["next_session_id"]
            self.db["next_session_id"] += 1
            self.db["sessions"][session_id] = {
                "id": session_id,
                "user_id": user_id,
                "refresh_token_hash": refresh_hash,
                "csrf_token_hash": csrf_hash,
                "expires_at": expires_at,
                "revoked_at": None,
                "replaced_by_session_id": None,
            }
            self.result = {"id": session_id}
        elif "from auth_sessions s join users u" in compact:
            refresh_hash = params[0]
            session = next(
                (row for row in self.db["sessions"].values() if row["refresh_token_hash"] == refresh_hash),
                None,
            )
            if not session:
                self.result = None
            else:
                user = self.db["users_by_id"][session["user_id"]]
                self.result = {**session, "email": user["email"]}
        elif compact.startswith("update auth_sessions set revoked_at = now(), replaced_by_session_id"):
            new_session_id, old_session_id = params
            self.db["sessions"][old_session_id]["revoked_at"] = auth._utcnow()
            self.db["sessions"][old_session_id]["replaced_by_session_id"] = new_session_id
        elif compact.startswith("update auth_sessions set revoked_at = now() where id"):
            session_id = params[0]
            self.db["sessions"][session_id]["revoked_at"] = auth._utcnow()
        elif compact.startswith("update auth_sessions set revoked_at = now() where user_id"):
            user_id = params[0]
            for session in self.db["sessions"].values():
                if session["user_id"] == user_id and session["revoked_at"] is None:
                    session["revoked_at"] = auth._utcnow()
        else:
            self.result = None


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db["commits"] += 1


def _request(cookies=None, csrf=None, client_host="testclient"):
    headers = []
    if csrf:
        headers.append((b"x-csrf-token", csrf.encode()))
    cookie_header = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    if cookie_header:
        headers.append((b"cookie", cookie_header.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/test",
        "headers": headers,
        "client": (client_host, 12345),
    }
    return Request(scope)


class AuthLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = {
            "users_by_email": {},
            "users_by_id": {},
            "sessions": {},
            "next_user_id": 1,
            "next_session_id": 1,
            "commits": 0,
        }
        auth._login_failures.clear()
        self.patcher = patch.object(auth, "_connect", lambda: FakeConnection(self.db))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _register(self, email="USER@Example.COM", password="abc12345"):
        response = Response()
        body = auth.RegisterRequest(email=email, password=password)
        result = auth.register_user(body, response)
        cookie_headers = [
            value.decode()
            for key, value in response.raw_headers
            if key.lower() == b"set-cookie"
        ]
        refresh = next(header for header in cookie_headers if header.startswith("refresh_token="))
        csrf = next(header for header in cookie_headers if header.startswith("csrf_token="))
        refresh_value = refresh.split("refresh_token=", 1)[1].split(";", 1)[0]
        csrf_value = csrf.split("csrf_token=", 1)[1].split(";", 1)[0]
        return result, response, refresh_value, csrf_value

    def test_register_sets_refresh_and_csrf_cookies_and_short_access_token(self):
        result, response, refresh_value, csrf_value = self._register()

        self.assertEqual(result.email, "user@example.com")
        self.assertEqual(result.expires_in, 900)
        self.assertEqual(len(self.db["sessions"]), 1)
        headers = [value.decode().lower() for key, value in response.raw_headers if key == b"set-cookie"]
        self.assertTrue(any("refresh_token=" in header and "httponly" in header for header in headers))
        self.assertTrue(any("csrf_token=" in header and "httponly" not in header for header in headers))
        payload = jwt.get_unverified_claims(result.access_token)
        self.assertLessEqual(payload["exp"] - payload["iat"], auth.ACCESS_TOKEN_EXPIRE_SECS)
        self.assertTrue(refresh_value)
        self.assertTrue(csrf_value)

    def test_weak_password_fails_registration(self):
        with self.assertRaises(HTTPException) as ctx:
            auth.register_user(auth.RegisterRequest(email="a@example.com", password="short"), Response())

        self.assertEqual(ctx.exception.status_code, 422)

    def test_login_normalizes_email_and_uses_generic_failure(self):
        self._register(email="User@Example.com", password="abc12345")
        response = Response()
        result = auth.login_user(
            auth.LoginRequest(email=" user@example.COM ", password="abc12345"),
            _request(),
            response,
        )

        self.assertEqual(result.email, "user@example.com")
        with self.assertRaises(HTTPException) as ctx:
            auth.login_user(
                auth.LoginRequest(email="missing@example.com", password="wrong123"),
                _request(),
                Response(),
            )
        self.assertEqual(ctx.exception.detail, auth.AUTH_FAILURE_DETAIL)

    def test_login_rate_limiting_blocks_repeated_failures(self):
        for _ in range(auth._LOGIN_MAX_FAILURES):
            with self.assertRaises(HTTPException):
                auth.login_user(
                    auth.LoginRequest(email="missing@example.com", password="wrong123"),
                    _request(client_host="1.2.3.4"),
                    Response(),
                )

        with self.assertRaises(HTTPException) as ctx:
            auth.login_user(
                auth.LoginRequest(email="missing@example.com", password="wrong123"),
                _request(client_host="1.2.3.4"),
                Response(),
            )

        self.assertEqual(ctx.exception.status_code, 429)

    def test_refresh_requires_csrf_and_rotates_session(self):
        _result, _response, refresh_value, csrf_value = self._register()
        with self.assertRaises(HTTPException) as missing_csrf:
            auth.refresh_user_session(_request(cookies={auth.REFRESH_COOKIE_NAME: refresh_value}), Response())
        self.assertEqual(missing_csrf.exception.status_code, 403)

        response = Response()
        refreshed = auth.refresh_user_session(
            _request(cookies={auth.REFRESH_COOKIE_NAME: refresh_value}, csrf=csrf_value),
            response,
        )

        self.assertEqual(refreshed.email, "user@example.com")
        self.assertEqual(len(self.db["sessions"]), 2)
        self.assertIsNotNone(self.db["sessions"][1]["revoked_at"])
        self.assertEqual(self.db["sessions"][1]["replaced_by_session_id"], 2)

    def test_reusing_old_refresh_token_is_rejected_and_revokes_user_sessions(self):
        _result, _response, refresh_value, csrf_value = self._register()
        auth.refresh_user_session(
            _request(cookies={auth.REFRESH_COOKIE_NAME: refresh_value}, csrf=csrf_value),
            Response(),
        )

        with self.assertRaises(HTTPException) as ctx:
            auth.refresh_user_session(
                _request(cookies={auth.REFRESH_COOKIE_NAME: refresh_value}, csrf=csrf_value),
                Response(),
            )

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertTrue(all(session["revoked_at"] is not None for session in self.db["sessions"].values()))

    def test_logout_revokes_session_and_clears_cookies(self):
        _result, _response, refresh_value, csrf_value = self._register()
        response = Response()
        result = auth.logout_user(
            _request(cookies={auth.REFRESH_COOKIE_NAME: refresh_value}, csrf=csrf_value),
            response,
        )

        self.assertEqual(result, {"status": "ok"})
        self.assertIsNotNone(self.db["sessions"][1]["revoked_at"])
        headers = [value.decode().lower() for key, value in response.raw_headers if key == b"set-cookie"]
        self.assertTrue(any("refresh_token=" in header and "max-age=0" in header for header in headers))

    def test_expired_refresh_session_is_rejected(self):
        _result, _response, refresh_value, csrf_value = self._register()
        self.db["sessions"][1]["expires_at"] = auth._utcnow() - timedelta(seconds=1)

        with self.assertRaises(HTTPException) as ctx:
            auth.refresh_user_session(
                _request(cookies={auth.REFRESH_COOKIE_NAME: refresh_value}, csrf=csrf_value),
                Response(),
            )

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIsNotNone(self.db["sessions"][1]["revoked_at"])


if __name__ == "__main__":
    unittest.main()
