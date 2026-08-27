"""
Authentication — JWT-based (email/password).

Endpoints added:
    POST /api/auth/register
    POST /api/auth/login

All /api/sessions and /api/chat endpoints are protected by
get_current_user() dependency (Bearer token required).

Passwords are hashed with bcrypt.
JWT secret is loaded from environment variable JWT_SECRET.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import bcrypt
import psycopg2
import psycopg2.extras
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr

from backend.core.logger import get_logger

log = get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
JWT_SECRET: str  = os.environ["JWT_SECRET"]          # fail fast if missing
JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_SECS  = 60 * 60 * 24 * 7                  # 7 days

DATABASE_URL: str = os.environ["DATABASE_URL"]

bearer_scheme = HTTPBearer(auto_error=False)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_auth_db():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            BIGSERIAL PRIMARY KEY,
                    email         TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
            """)
        conn.commit()
    log.info("Auth schema initialised")


# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _create_token(user_id: int, email: str) -> str:
    payload = {
        "sub":   str(user_id),
        "email": email,
        "iat":   int(time.time()),
        "exp":   int(time.time()) + JWT_EXPIRE_SECS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


# ── FastAPI dependency ────────────────────────────────────────────────────────

class CurrentUser(BaseModel):
    user_id: str
    email: str


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> CurrentUser:
    """FastAPI dependency — inject into any protected endpoint."""
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


# ── Request / response models ─────────────────────────────────────────────────

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


# ── Route handlers (imported into api.py) ────────────────────────────────────

def register_user(req: RegisterRequest) -> AuthResponse:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (req.email,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Email already registered")
            hashed = _hash_password(req.password)
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (req.email, hashed),
            )
            user_id = cur.fetchone()["id"]
        conn.commit()

    token = _create_token(user_id, req.email)
    log.info(f"New user registered: {req.email}")
    return AuthResponse(access_token=token, user_id=str(user_id), email=req.email)


def login_user(req: LoginRequest) -> AuthResponse:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM users WHERE email = %s", (req.email,)
            )
            row = cur.fetchone()

    if not row or not _verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = _create_token(row["id"], req.email)
    log.info(f"User logged in: {req.email}")
    return AuthResponse(access_token=token, user_id=str(row["id"]), email=req.email)