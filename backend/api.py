import os
import asyncio
import pyodbc
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv

from agent.loop import run_single_turn
from agent.memory import init_db, create_session, load_history, get_all_sessions
from core.rate_limit import check_rate_limit, time_remaining

load_dotenv()

executor = ThreadPoolExecutor(max_workers=10)
gemini_client: genai.Client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gemini_client
    init_db()
    gemini_client = genai.Client(api_key="////////")
    print("✅ DB initialised")
    print("✅ Gemini client ready")
    yield
    executor.shutdown(wait=False)


app = FastAPI(title="Travel Agent API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"error": "server_error", "message": str(exc)}
    )


# ── Models ───────────────────────────────────────────────────────────────────

class NewSessionResponse(BaseModel):
    session_id: str

class SessionItem(BaseModel):
    session_id: str
    created_at: str

class SessionsListResponse(BaseModel):
    sessions: list[SessionItem]

class ChatRequest(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    text: str
    tools_used: list[str]
    cached: bool
    session_id: str

class HistoryMessage(BaseModel):
    role: str
    content: str

class HistoryResponse(BaseModel):
    session_id: str
    messages: list[HistoryMessage]


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/api/sessions", response_model=NewSessionResponse, status_code=201)
def new_session():
    """Create a new conversation session."""
    try:
        session_id = create_session()
        return NewSessionResponse(session_id=session_id)
    except pyodbc.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {str(e)}")


@app.get("/api/sessions", response_model=SessionsListResponse)
def list_sessions():
    """
    Return all sessions ordered by most recent first.
    Used by the React sidebar to list past conversations.
    """
    try:
        rows = get_all_sessions()
    except pyodbc.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {str(e)}")

    sessions = [
        SessionItem(
            session_id=row[0],
            created_at=str(row[1])
        )
        for row in rows
    ]
    return SessionsListResponse(sessions=sessions)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):

    if not check_rate_limit(req.session_id):
        wait = time_remaining(req.session_id)
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "retry_after_seconds": wait}
        )

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            executor,
            run_single_turn,
            req.session_id,
            req.message,
            gemini_client,
        )
        return ChatResponse(**result, session_id=req.session_id)

    except pyodbc.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {str(e)}")

    except TimeoutError:
        raise HTTPException(status_code=504, detail="Gemini request timed out, please try again")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions/{session_id}/history", response_model=HistoryResponse)
def get_history(session_id: str):

    try:
        rows = load_history(session_id)
    except pyodbc.Error as e:
        raise HTTPException(status_code=503, detail=f"Database error: {str(e)}")

    if rows is None:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = [
        HistoryMessage(role=role, content=content)
        for role, content in rows
        if role != "tool"
    ]
    return HistoryResponse(session_id=session_id, messages=messages)


@app.get("/health")
def health():
    return {"status": "ok"}