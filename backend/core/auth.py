"""Authentication lifecycle: short-lived JWT access tokens and rotating refresh cookies."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import psycopg2
import psycopg2.extras
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr

from backend.core.client_ip import get_client_ip
from backend.core.logger import get_logger

log = get_logger(__name__)

JWT_SECRET: str = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_SECS = 15 * 60
REFRESH_TOKEN_EXPIRE_DAYS = 30
REFRESH_COOKIE_NAME = "refresh_token"
CSRF_COOKIE_NAME = "csrf_token"
AUTH_COOKIE_PATH = "/api/auth"
AUTH_FAILURE_DETAIL = "Invalid email or password"
PASSWORD_POLICY_DETAIL = (
    "Password must be at least 8 characters and include at least one letter and one number."
)

DATABASE_URL: str = os.environ["DATABASE_URL"]

bearer_scheme = HTTPBearer(auto_error=False)

_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_FAILURES = 5
_login_failures: dict[str, deque[float]] = defaultdict(deque)


def _connect():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_auth_db():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            BIGSERIAL PRIMARY KEY,
                    email         TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id                     BIGSERIAL PRIMARY KEY,
                    user_id                BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    refresh_token_hash     TEXT NOT NULL UNIQUE,
                    csrf_token_hash        TEXT NOT NULL,
                    expires_at             TIMESTAMPTZ NOT NULL,
                    revoked_at             TIMESTAMPTZ,
                    replaced_by_session_id BIGINT REFERENCES auth_sessions(id),
                    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions (user_id);
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_refresh_hash
                    ON auth_sessions (refresh_token_hash);
                """
            )
        conn.commit()
    log.info("Auth schema initialised")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password_strength(password: str) -> None:
    has_letter = any(ch.isalpha() for ch in password)
    has_number = any(ch.isdigit() for ch in password)
    if len(password) < 8 or not has_letter or not has_number:
        raise HTTPException(status_code=422, detail=PASSWORD_POLICY_DETAIL)


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _hash_secret(secret: str) -> str:
    return hmac.new(JWT_SECRET.encode(), secret.encode(), hashlib.sha256).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(48)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _create_token(user_id: int | str, email: str) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + ACCESS_TOKEN_EXPIRE_SECS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def _cookie_secure() -> bool:
    configured = os.getenv("AUTH_COOKIE_SECURE")
    if configured is not None:
        return configured.lower() in {"1", "true", "yes"}
    return os.getenv("APP_ENV", "local").lower() in {"prod", "production"}


def _set_auth_cookies(response: Response, refresh_token: str, csrf_token: str) -> None:
    max_age = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    secure = _cookie_secure()
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=AUTH_COOKIE_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path=AUTH_COOKIE_PATH,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE_NAME, path=AUTH_COOKIE_PATH)
    response.delete_cookie(CSRF_COOKIE_NAME, path=AUTH_COOKIE_PATH)


def _client_ip(request: Request) -> str:
    return get_client_ip(request)


def _login_rate_key(email: str, request: Request) -> str:
    return f"{normalize_email(email)}:{_client_ip(request)}"


def _login_rate_limited(key: str) -> bool:
    now = time.time()
    window = _login_failures[key]
    while window and window[0] < now - _LOGIN_WINDOW_SECONDS:
        window.popleft()
    return len(window) >= _LOGIN_MAX_FAILURES


def _record_login_failure(key: str) -> None:
    _login_failures[key].append(time.time())


def _clear_login_failures(key: str) -> None:
    _login_failures.pop(key, None)


def _create_refresh_session(cur, user_id: int | str) -> tuple[int, str, str]:
    refresh_token = _new_secret()
    csrf_token = _new_secret()
    expires_at = _utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    cur.execute(
        """
        INSERT INTO auth_sessions (
            user_id, refresh_token_hash, csrf_token_hash, expires_at
        )
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (user_id, _hash_secret(refresh_token), _hash_secret(csrf_token), expires_at),
    )
    return int(cur.fetchone()["id"]), refresh_token, csrf_token


def _get_refresh_session(cur, refresh_token: str) -> Optional[dict]:
    cur.execute(
        """
        SELECT s.*, u.email
        FROM auth_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.refresh_token_hash = %s
        """,
        (_hash_secret(refresh_token),),
    )
    return cur.fetchone()


def _revoke_session(cur, session_id: int) -> None:
    cur.execute(
        "UPDATE auth_sessions SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL",
        (session_id,),
    )


def _revoke_all_user_sessions(cur, user_id: int | str) -> None:
    cur.execute(
        "UPDATE auth_sessions SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )


def _is_expired(expires_at) -> bool:
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= _utcnow()
    return False


def _require_csrf(request: Request, session: dict) -> None:
    csrf_header = request.headers.get("X-CSRF-Token")
    if not csrf_header or not hmac.compare_digest(
        _hash_secret(csrf_header), session["csrf_token_hash"]
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _require_refresh_cookie(request: Request) -> str:
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return refresh_token


def _issue_auth_response(response: Response, user_id: int | str, email: str, cur) -> "AuthResponse":
    _session_id, refresh_token, csrf_token = _create_refresh_session(cur, user_id)
    _set_auth_cookies(response, refresh_token, csrf_token)
    return AuthResponse(
        access_token=_create_token(user_id, email),
        user_id=str(user_id),
        email=email,
        expires_in=ACCESS_TOKEN_EXPIRE_SECS,
    )


class CurrentUser(BaseModel):
    user_id: str
    email: str


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = _decode_token(credentials.credentials)
        return CurrentUser(user_id=payload["sub"], email=payload["email"])
    except JWTError as exc:
        log.warning(f"Invalid JWT: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    expires_in: int


def register_user(req: RegisterRequest, response: Response) -> AuthResponse:
    email = normalize_email(req.email)
    validate_password_strength(req.password)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Unable to register with those credentials")
            hashed = _hash_password(req.password)
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (email, hashed),
            )
            user_id = cur.fetchone()["id"]
            auth_response = _issue_auth_response(response, user_id, email, cur)
        conn.commit()

    log.info(f"New user registered: {email}")
    return auth_response


def login_user(req: LoginRequest, request: Request, response: Response) -> AuthResponse:
    email = normalize_email(req.email)
    rate_key = _login_rate_key(email, request)
    if _login_rate_limited(rate_key):
        raise HTTPException(status_code=429, detail="Too many login attempts")

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, password_hash FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if not row or not _verify_password(req.password, row["password_hash"]):
                _record_login_failure(rate_key)
                raise HTTPException(status_code=401, detail=AUTH_FAILURE_DETAIL)
            _clear_login_failures(rate_key)
            auth_response = _issue_auth_response(response, row["id"], email, cur)
        conn.commit()

    log.info(f"User logged in: {email}")
    return auth_response


def refresh_user_session(request: Request, response: Response) -> AuthResponse:
    refresh_token = _require_refresh_cookie(request)
    with _connect() as conn:
        with conn.cursor() as cur:
            session = _get_refresh_session(cur, refresh_token)
            if not session:
                _clear_auth_cookies(response)
                raise HTTPException(status_code=401, detail="Invalid refresh session")
            if session["revoked_at"] is not None or session["replaced_by_session_id"] is not None:
                _revoke_all_user_sessions(cur, session["user_id"])
                conn.commit()
                _clear_auth_cookies(response)
                raise HTTPException(status_code=401, detail="Invalid refresh session")
            if _is_expired(session["expires_at"]):
                _revoke_session(cur, session["id"])
                conn.commit()
                _clear_auth_cookies(response)
                raise HTTPException(status_code=401, detail="Refresh session expired")

            _require_csrf(request, session)
            new_session_id, new_refresh_token, new_csrf_token = _create_refresh_session(
                cur, session["user_id"]
            )
            cur.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = NOW(), replaced_by_session_id = %s
                WHERE id = %s
                """,
                (new_session_id, session["id"]),
            )
            auth_response = AuthResponse(
                access_token=_create_token(session["user_id"], session["email"]),
                user_id=str(session["user_id"]),
                email=session["email"],
                expires_in=ACCESS_TOKEN_EXPIRE_SECS,
            )
        conn.commit()

    _set_auth_cookies(response, new_refresh_token, new_csrf_token)
    return auth_response


def logout_user(request: Request, response: Response) -> dict:
    refresh_token = _require_refresh_cookie(request)
    with _connect() as conn:
        with conn.cursor() as cur:
            session = _get_refresh_session(cur, refresh_token)
            if session and session["revoked_at"] is None:
                _require_csrf(request, session)
                _revoke_session(cur, session["id"])
        conn.commit()
    _clear_auth_cookies(response)
    return {"status": "ok"}
