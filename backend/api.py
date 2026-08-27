"""
Travel Agent API — production-ready FastAPI application.

New vs original:
  ✅ Secrets from env vars only (no hardcoding)
  ✅ IP-based rate limiting + spam protection
  ✅ Input length validation
  ✅ JWT authentication (register / login)
  ✅ Request timing + security-headers middleware
  ✅ Structured logging (loguru)
  ✅ Deep /health endpoint (DB + Gemini probe)
  ✅ Prometheus /metrics endpoint
  ✅ JSON /api/metrics endpoint
  ✅ Session rename + soft-delete
  ✅ Paginated chat history
  ✅ Export chat as JSON
  ✅ Switched to PostgreSQL (Neon / Supabase compatible)
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from google import genai
from pydantic import BaseModel, Field

from agent.loop import run_single_turn
from agent.memory import (
    cleanup_old_sessions,
    create_session,
    delete_session,
    get_all_sessions,
    get_owned_conversation_id,
    init_db,
    load_history,
    rename_session,
)
from core.auth import (
    AuthResponse,
    CurrentUser,
    LoginRequest,
    RegisterRequest,
    get_current_user,
    init_auth_db,
    login_user,
    register_user,
)
from core.logger import get_logger
from core.metrics import get_metrics, prometheus_export
from core.rate_limit import (
    MAX_INPUT_LENGTH,
    check_ip_rate_limit,
    check_rate_limit,
    check_spam,
    ip_requests_remaining,
    time_remaining,
    validate_input,
)
from middleware.timing import RequestTimingMiddleware, SecurityHeadersMiddleware

load_dotenv()
log = get_logger(__name__)

executor = ThreadPoolExecutor(max_workers=10)
gemini_client: genai.Client = None   # type: ignore
_start_time = time.time()


def _raise_session_not_found() -> None:
    raise HTTPException(status_code=404, detail="Session not found")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gemini_client
    required = ["DATABASE_URL", "GEMINI_API_KEY", "JWT_SECRET"]
    missing  = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {missing}")
    init_db()
    init_auth_db()
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    log.info("✅ Database initialised")
    log.info("✅ Gemini client ready")
    yield
    executor.shutdown(wait=False)
    log.info("🛑 Server shutting down")


app = FastAPI(title="Travel Agent API", version="2.0.0", lifespan=lifespan)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestTimingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception(f"Unhandled error on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "server_error", "message": "An unexpected error occurred."},
    )


# ── Pydantic models ───────────────────────────────────────────────────────────

class NewSessionResponse(BaseModel):
    session_id: str

class SessionItem(BaseModel):
    session_id: str
    created_at: str

class SessionsListResponse(BaseModel):
    sessions: list[SessionItem]

class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(..., max_length=MAX_INPUT_LENGTH)

class ChatResponse(BaseModel):
    text:       str
    tools_used: list[str]
    cached:     bool
    session_id: str

class HistoryMessage(BaseModel):
    role:    str
    content: str

class HistoryResponse(BaseModel):
    session_id: str
    messages:   list[HistoryMessage]
    total:      int

class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)

class HealthStatus(BaseModel):
    status:   str
    db:       str
    gemini:   str
    uptime_s: float


# ── Auth (public) ─────────────────────────────────────────────────────────────

@app.post("/api/auth/register", response_model=AuthResponse, status_code=201, tags=["auth"])
def api_register(req: RegisterRequest):
    return register_user(req)

@app.post("/api/auth/login", response_model=AuthResponse, tags=["auth"])
def api_login(req: LoginRequest):
    return login_user(req)


# ── Sessions (protected) ──────────────────────────────────────────────────────

@app.post("/api/sessions", response_model=NewSessionResponse, status_code=201, tags=["sessions"])
def new_session(current_user: CurrentUser = Depends(get_current_user)):
    try:
        session_id = create_session(user_id=current_user.user_id)
        return NewSessionResponse(session_id=session_id)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")

@app.get("/api/sessions", response_model=SessionsListResponse, tags=["sessions"])
def list_sessions(current_user: CurrentUser = Depends(get_current_user)):
    try:
        rows = get_all_sessions(user_id=current_user.user_id)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")

    return SessionsListResponse(sessions=[
        SessionItem(session_id=str(r["session_id"]), created_at=str(r["created_at"])) for r in rows
    ])

@app.patch("/api/sessions/{session_id}/rename", status_code=204, tags=["sessions"])
def api_rename(session_id: str, req: RenameRequest,
               current_user: CurrentUser = Depends(get_current_user)):
    try:
        renamed = rename_session(session_id, current_user.user_id, req.name)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    if not renamed:
        _raise_session_not_found()

@app.delete("/api/sessions/{session_id}", status_code=204, tags=["sessions"])
def api_delete(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    try:
        deleted = delete_session(session_id, current_user.user_id)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    if not deleted:
        _raise_session_not_found()


# ── Chat (protected) ──────────────────────────────────────────────────────────

@app.post("/api/chat", response_model=ChatResponse, tags=["chat"])
async def chat(
    req: Request,
    body: ChatRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    client_ip = (
        req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (req.client.host if req.client else "unknown")
    )

    if not check_ip_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail={"error": "ip_rate_limited",
                    "requests_remaining": ip_requests_remaining(client_ip)},
        )
    if check_spam(client_ip, body.message):
        raise HTTPException(
            status_code=429,
            detail={"error": "spam_detected", "message": "Too many identical messages."},
        )
    ok, reason = validate_input(body.message)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)

    try:
        if get_owned_conversation_id(body.session_id, current_user.user_id) is None:
            _raise_session_not_found()
        if not check_rate_limit(body.session_id):
            raise HTTPException(
                status_code=429,
                detail={"error": "session_rate_limited",
                        "retry_after_seconds": time_remaining(body.session_id)},
            )
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            executor,
            run_single_turn,
            body.session_id,
            current_user.user_id,
            body.message,
            gemini_client,
        )
        return ChatResponse(**result, session_id=body.session_id)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Gemini request timed out.")
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"Chat error session={body.session_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── History (protected, paginated) ────────────────────────────────────────────



@app.get(
    "/api/sessions/{session_id}/history",
    response_model=HistoryResponse,
    tags=["sessions"]
)
def get_history(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        if get_owned_conversation_id(session_id, current_user.user_id) is None:
            _raise_session_not_found()
        rows = load_history(
            session_id,
            current_user.user_id,
            limit=limit,
            offset=offset,
        )

    except psycopg2.Error as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database error: {e}"
        )

    # IMPORTANT: handle None OR empty result safely
    if not rows:
        return HistoryResponse(
            session_id=session_id,
            messages=[],
            total=0
        )

    messages = []
    for row in rows:
        role = row[0]
        content = row[1]

        # skip tool messages
        if role == "tool":
            continue

        messages.append(
            HistoryMessage(role=role, content=content)
        )

    return HistoryResponse(
        session_id=session_id,
        messages=messages,
        total=len(messages)
    )


# ── Export (protected) ────────────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/export", tags=["sessions"])
def export_session(session_id: str,
                   current_user: CurrentUser = Depends(get_current_user)):
    """Export full conversation as downloadable JSON."""
    try:
        if get_owned_conversation_id(session_id, current_user.user_id) is None:
            _raise_session_not_found()
        rows = load_history(session_id, current_user.user_id, limit=1000)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    messages = [{"role": r, "content": c} for r, c in rows if r != "tool"]
    return JSONResponse(
        content={"session_id": session_id, "messages": messages},
        headers={"Content-Disposition": f'attachment; filename="chat-{session_id}.json"'},
    )


# ── Health + Observability (public) ──────────────────────────────────────────

@app.get("/health", response_model=HealthStatus, tags=["ops"])
def health():
    db_ok, gemini_ok = "ok", "ok"
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.close()
    except Exception as e:
        db_ok = f"error: {e}"
        log.error(f"Health DB probe failed: {e}")
    if gemini_client is None:
        gemini_ok = "not initialised"
    status = "ok" if db_ok == "ok" and gemini_ok == "ok" else "degraded"
    return HealthStatus(status=status, db=db_ok, gemini=gemini_ok,
                        uptime_s=round(time.time() - _start_time, 1))

@app.get("/api/metrics", tags=["ops"])
def api_metrics():
    return get_metrics()

@app.get("/metrics", response_class=PlainTextResponse, tags=["ops"])
def prom_metrics():
    return prometheus_export()
