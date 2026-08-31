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
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
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
    logout_user,
    refresh_user_session,
    register_user,
)
from backend.core.client_ip import get_client_ip, get_cors_config
from backend.core.concurrency import SessionBusyError, acquire_session_lock, release_session_lock
from backend.core.db import close_pool, init_pool, readiness_check
from backend.core.idempotency import (
    IdempotencyConflictError,
    begin_request,
    complete_request,
    fail_request,
)
from core.logger import get_logger
from backend.core.redis_client import redis_ready
from backend.core.metrics import get_metrics, prometheus_export, record_prompt_guard
from core.rate_limit import (
    MAX_INPUT_LENGTH,
    check_ai_user_rate_limit,
    check_ip_rate_limit,
    check_register_rate_limit,
    check_rate_limit,
    check_spam,
    ip_requests_remaining,
    time_remaining,
    validate_input,
)
from backend.security.prompt_guard import analyze_prompt, safe_error_message
from middleware.timing import RequestTimingMiddleware, SecurityHeadersMiddleware

load_dotenv()
log = get_logger(__name__)

executor = ThreadPoolExecutor(max_workers=10)
gemini_client: genai.Client = None   # type: ignore
_start_time = time.time()


def _raise_session_not_found() -> None:
    raise HTTPException(status_code=404, detail="Session not found")


api_v1 = APIRouter(prefix="/api/v1")
legacy_api = APIRouter(prefix="/api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gemini_client
    required = ["DATABASE_URL", "GEMINI_API_KEY", "JWT_SECRET"]
    missing  = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {missing}")
    init_pool()
    init_db()
    init_auth_db()
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    log.info("✅ Database initialised")
    log.info("✅ Gemini client ready")
    yield
    executor.shutdown(wait=False)
    close_pool()
    log.info("🛑 Server shutting down")


app = FastAPI(title="Travel Agent API", version="2.0.0", lifespan=lifespan)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestTimingMiddleware)
cors_config = get_cors_config()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_config.allow_origins,
    allow_credentials=True,
    allow_methods=cors_config.allow_methods,
    allow_headers=cors_config.allow_headers,
    expose_headers=cors_config.expose_headers,
)


@app.middleware("http")
async def legacy_api_deprecation_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/v1/"):
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = '</api/v1>; rel="successor-version"'
    return response


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


class ErrorResponse(BaseModel):
    error: str
    message: str


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=(
            "Rihla public API. Canonical application routes live under /api/v1; "
            "legacy /api aliases are deprecated for one release."
        ),
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("schemas", {})["ErrorResponse"] = ErrorResponse.model_json_schema()
    schema["components"]["examples"] = {
        "RateLimited": {"value": {"error": "rate_limited", "message": "Retry after the provided number of seconds."}},
        "IdempotencyConflict": {"value": {"error": "idempotency_conflict", "message": "Idempotency-Key was reused with a different request body."}},
        "SessionBusy": {"value": {"error": "session_busy", "message": "Another message is already running for this session."}},
    }
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# ── Auth (public) ─────────────────────────────────────────────────────────────

@api_v1.post("/auth/register", response_model=AuthResponse, status_code=201, tags=["auth"])
@legacy_api.post("/auth/register", response_model=AuthResponse, status_code=201, tags=["auth"], include_in_schema=False)
def api_register(req: RegisterRequest, request: Request, response: Response):
    client_ip = get_client_ip(request)
    register_limit = check_register_rate_limit(f"{req.email}:{client_ip}")
    if not register_limit.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many registration attempts",
            headers={"Retry-After": str(register_limit.retry_after_seconds)},
        )
    return register_user(req, response)

@api_v1.post("/auth/login", response_model=AuthResponse, tags=["auth"])
@legacy_api.post("/auth/login", response_model=AuthResponse, tags=["auth"], include_in_schema=False)
def api_login(req: LoginRequest, request: Request, response: Response):
    return login_user(req, request, response)

@api_v1.post("/auth/refresh", response_model=AuthResponse, tags=["auth"])
@legacy_api.post("/auth/refresh", response_model=AuthResponse, tags=["auth"], include_in_schema=False)
def api_refresh(request: Request, response: Response):
    return refresh_user_session(request, response)

@api_v1.post("/auth/logout", tags=["auth"])
@legacy_api.post("/auth/logout", tags=["auth"], include_in_schema=False)
def api_logout(request: Request, response: Response):
    return logout_user(request, response)


# ── Sessions (protected) ──────────────────────────────────────────────────────

@api_v1.post("/sessions", response_model=NewSessionResponse, status_code=201, tags=["sessions"])
@legacy_api.post("/sessions", response_model=NewSessionResponse, status_code=201, tags=["sessions"], include_in_schema=False)
def new_session(current_user: CurrentUser = Depends(get_current_user)):
    try:
        session_id = create_session(user_id=current_user.user_id)
        return NewSessionResponse(session_id=session_id)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")

@api_v1.get("/sessions", response_model=SessionsListResponse, tags=["sessions"])
@legacy_api.get("/sessions", response_model=SessionsListResponse, tags=["sessions"], include_in_schema=False)
def list_sessions(current_user: CurrentUser = Depends(get_current_user)):
    try:
        rows = get_all_sessions(user_id=current_user.user_id)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")

    return SessionsListResponse(sessions=[
        SessionItem(session_id=str(r["session_id"]), created_at=str(r["created_at"])) for r in rows
    ])

@api_v1.patch("/sessions/{session_id}/rename", status_code=204, tags=["sessions"])
@legacy_api.patch("/sessions/{session_id}/rename", status_code=204, tags=["sessions"], include_in_schema=False)
def api_rename(session_id: str, req: RenameRequest,
               current_user: CurrentUser = Depends(get_current_user)):
    try:
        renamed = rename_session(session_id, current_user.user_id, req.name)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    if not renamed:
        _raise_session_not_found()

@api_v1.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
@legacy_api.delete("/sessions/{session_id}", status_code=204, tags=["sessions"], include_in_schema=False)
def api_delete(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    try:
        deleted = delete_session(session_id, current_user.user_id)
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    if not deleted:
        _raise_session_not_found()


# ── Chat (protected) ──────────────────────────────────────────────────────────

@api_v1.post("/chat", response_model=ChatResponse, tags=["chat"])
@legacy_api.post("/chat", response_model=ChatResponse, tags=["chat"], include_in_schema=False)
async def chat(
    req: Request,
    body: ChatRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: CurrentUser = Depends(get_current_user),
):
    client_ip = get_client_ip(req)

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
    ai_limit = check_ai_user_rate_limit(current_user.user_id)
    if not ai_limit.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": "user_ai_rate_limited", "retry_after_seconds": ai_limit.retry_after_seconds},
            headers={"Retry-After": str(ai_limit.retry_after_seconds)},
        )
    ok, reason = validate_input(body.message)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)

    prompt_risk = analyze_prompt(body.message)
    record_prompt_guard(
        prompt_risk.action,
        prompt_risk.risk_level,
        prompt_risk.latency_ms,
    )
    log.info(
        f"prompt_guard session={body.session_id} user={current_user.user_id} "
        f"action={prompt_risk.action} level={prompt_risk.risk_level} "
        f"score={prompt_risk.risk_score} signals={prompt_risk.signals}"
    )
    if prompt_risk.action == "block":
        raise HTTPException(
            status_code=403,
            detail="This request looks like an attempt to access hidden instructions or internal configuration.",
        )

    try:
        if get_owned_conversation_id(body.session_id, current_user.user_id) is None:
            _raise_session_not_found()
        if not check_rate_limit(body.session_id):
            retry_after = time_remaining(body.session_id)
            raise HTTPException(
                status_code=429,
                detail={"error": "session_rate_limited",
                        "retry_after_seconds": retry_after},
                headers={"Retry-After": str(retry_after)},
            )
        stored = begin_request(
            current_user.user_id,
            idempotency_key,
            body.model_dump(),
        )
        if stored and stored.response:
            return ChatResponse(**stored.response)
        if stored and stored.in_progress:
            raise HTTPException(status_code=409, detail="Identical request is already in progress.")
        lock = acquire_session_lock(body.session_id)
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                executor,
                run_single_turn,
                body.session_id,
                current_user.user_id,
                body.message,
                gemini_client,
                prompt_risk.action == "allow_with_restrictions",
            )
        finally:
            release_session_lock(lock)
        response_payload = {**result, "session_id": body.session_id}
        complete_request(current_user.user_id, idempotency_key, response_payload)
        return ChatResponse(**response_payload)
    except IdempotencyConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SessionBusyError as e:
        raise HTTPException(status_code=409, detail=str(e), headers={"Retry-After": "5"})
    except psycopg2.Error as e:
        fail_request(current_user.user_id, idempotency_key)
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    except TimeoutError:
        fail_request(current_user.user_id, idempotency_key)
        raise HTTPException(status_code=504, detail="Gemini request timed out.")
    except HTTPException:
        raise
    except Exception as e:
        fail_request(current_user.user_id, idempotency_key)
        log.exception(f"Chat error session={body.session_id}")
        raise HTTPException(status_code=500, detail=safe_error_message())


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _stream_chat_events(
    request: Request,
    body: ChatRequest,
    current_user: CurrentUser,
) -> AsyncIterator[str]:
    started = time.time()
    yield _sse("turn_started", {"session_id": body.session_id})
    try:
        response = await chat(request, body, None, current_user)
        payload = response.model_dump() if isinstance(response, BaseModel) else dict(response)
        for tool_name in payload.get("tools_used", []):
            yield _sse("tool_started", {"tool": tool_name})
            yield _sse("tool_completed", {"tool": tool_name})
        text = payload.get("text", "")
        first_token_ms = None
        for index in range(0, len(text), 80):
            if await request.is_disconnected():
                return
            if first_token_ms is None:
                first_token_ms = round((time.time() - started) * 1000, 1)
            yield _sse("message_delta", {"text": text[index:index + 80]})
            await asyncio.sleep(0)
        yield _sse(
            "turn_completed",
            {
                "session_id": body.session_id,
                "cached": payload.get("cached", False),
                "tools_used": payload.get("tools_used", []),
                "time_to_first_token_ms": first_token_ms,
                "latency_ms": round((time.time() - started) * 1000, 1),
            },
        )
    except HTTPException as exc:
        yield _sse("error", {"status_code": exc.status_code, "message": safe_error_message()})
    except Exception:
        log.exception(f"Streaming chat error session={body.session_id}")
        yield _sse("error", {"status_code": 500, "message": safe_error_message()})


@api_v1.post("/chat/stream", tags=["chat"])
@legacy_api.post("/chat/stream", tags=["chat"], include_in_schema=False)
async def chat_stream(
    req: Request,
    body: ChatRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    return StreamingResponse(
        _stream_chat_events(req, body, current_user),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── History (protected, paginated) ────────────────────────────────────────────



@api_v1.get(
    "/sessions/{session_id}/history",
    response_model=HistoryResponse,
    tags=["sessions"]
)
@legacy_api.get(
    "/sessions/{session_id}/history",
    response_model=HistoryResponse,
    tags=["sessions"],
    include_in_schema=False,
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

@api_v1.get("/sessions/{session_id}/export", tags=["sessions"])
@legacy_api.get("/sessions/{session_id}/export", tags=["sessions"], include_in_schema=False)
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

@app.get("/health/live", tags=["ops"])
def live():
    return {"status": "ok", "uptime_s": round(time.time() - _start_time, 1)}


@app.get("/health/ready", response_model=HealthStatus, tags=["ops"])
@app.get("/health", response_model=HealthStatus, tags=["ops"], include_in_schema=False)
def health():
    db_ok, gemini_ok = "ok", "ok"
    if not readiness_check():
        db_ok = "error"
    if gemini_client is None:
        gemini_ok = "not initialised"
    status = "ok" if db_ok == "ok" and gemini_ok == "ok" else "degraded"
    return HealthStatus(status=status, db=db_ok, gemini=gemini_ok,
                        uptime_s=round(time.time() - _start_time, 1))

@api_v1.get("/metrics", tags=["ops"])
@legacy_api.get("/metrics", tags=["ops"], include_in_schema=False)
def api_metrics():
    return get_metrics()

@app.get("/metrics", response_class=PlainTextResponse, tags=["ops"])
def prom_metrics():
    return prometheus_export()


app.include_router(api_v1)
app.include_router(legacy_api)
