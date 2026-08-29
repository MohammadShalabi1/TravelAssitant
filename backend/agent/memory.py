"""
Persistent memory layer — PostgreSQL (Neon / Supabase / Railway).

Changes from original (SQL Server):
  - Driver: asyncpg-compatible psycopg2 / psycopg  → use psycopg2 for sync
  - Indexes on session_id + created_at
  - messages.created_at column added
  - Soft-delete on conversations (deleted_at)
  - Paginated history fetch
  - Cleanup helper (purge sessions older than N days)
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PGConnection
from dotenv import load_dotenv
from backend.core.logger import get_logger

log = get_logger(__name__)



load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
AGENT_MEMORY_VERSION = "agent-memory-v1"


@dataclass(frozen=True)
class ConversationSummary:
    summary: str
    version: str
    updated_at: datetime | str | None


@dataclass(frozen=True)
class AgentContext:
    messages: list[tuple[str, str]]
    summary: ConversationSummary | None
    token_estimate: int
    truncated: bool

# ── Connection helper ─────────────────────────────────────────────────────────

def _connect() -> PGConnection:
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Schema bootstrap ──────────────────────────────────────────────────────────

def init_db():
    """Create tables + indexes if they don't exist yet."""
    sql = """
    CREATE TABLE IF NOT EXISTS conversations (
        id          BIGSERIAL PRIMARY KEY,
        session_id  TEXT        NOT NULL UNIQUE,
        user_id     TEXT,                           -- NULL until auth is added
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted_at  TIMESTAMPTZ                     -- soft-delete
    );
    CREATE INDEX IF NOT EXISTS idx_conversations_session_id
        ON conversations (session_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_created_at
        ON conversations (created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_conversations_user_id
        ON conversations (user_id)
        WHERE user_id IS NOT NULL;

    CREATE TABLE IF NOT EXISTS messages (
        id                BIGSERIAL PRIMARY KEY,
        conversation_id   BIGINT      NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role              TEXT        NOT NULL,
        content           TEXT        NOT NULL,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
        ON messages (conversation_id, id ASC);
    CREATE INDEX IF NOT EXISTS idx_messages_created_at
        ON messages (created_at DESC);

    CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id BIGINT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        summary         TEXT NOT NULL,
        version         TEXT NOT NULL,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    log.info("Database schema initialised")


# ── Session management ────────────────────────────────────────────────────────

def create_session(user_id: str) -> str:
    session_id = str(uuid.uuid4())
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (session_id, user_id) VALUES (%s, %s)",
                (session_id, user_id),
            )
        conn.commit()
    log.info(f"Session created: {session_id}")
    return session_id


def get_owned_conversation_id(session_id: str, user_id: str) -> int | None:
    """Return the conversation id only when the session belongs to this user."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM conversations
                WHERE session_id = %s
                  AND user_id = %s
                  AND deleted_at IS NULL
                """,
                (session_id, user_id),
            )
            row = cur.fetchone()
    return row["id"] if row else None


def get_all_sessions(user_id: str) -> list[tuple]:
    """Return this user's sessions ordered newest first."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT session_id, created_at FROM conversations
                   WHERE user_id = %s AND deleted_at IS NULL
                   ORDER BY created_at DESC""",
                (user_id,),
            )
            return cur.fetchall()


def delete_session(session_id: str, user_id: str) -> bool:
    """Soft-delete a session only if it belongs to the user."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE conversations
                SET deleted_at = NOW()
                WHERE session_id = %s
                  AND user_id = %s
                  AND deleted_at IS NULL
                """,
                (session_id, user_id),
            )
            updated = cur.rowcount
        conn.commit()
    if updated:
        log.info(f"Session soft-deleted: {session_id}")
    return updated > 0


def rename_session(session_id: str, user_id: str, name: str) -> bool:
    """
    Store a human-readable name for a session.
    Requires an ALTER TABLE to add a `name` column — included in init_db
    as a safe migration guard.
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            # idempotent column add
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='conversations' AND column_name='name'
                    ) THEN
                        ALTER TABLE conversations ADD COLUMN name TEXT;
                    END IF;
                END$$;
            """)
            cur.execute(
                """
                UPDATE conversations
                SET name = %s
                WHERE session_id = %s
                  AND user_id = %s
                  AND deleted_at IS NULL
                """,
                (name, session_id, user_id),
            )
            updated = cur.rowcount
        conn.commit()
    return updated > 0


# ── Message persistence ───────────────────────────────────────────────────────

def save_message(session_id: str, user_id: str, role: str, content: str) -> bool:
    conv_id = get_owned_conversation_id(session_id, user_id)
    if conv_id is None:
        log.error(f"save_message: session not found: {session_id}")
        return False
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
                (conv_id, role, content),
            )
        conn.commit()
    return True


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _summarize_messages(rows: list[dict]) -> str:
    if not rows:
        return ""

    extracts = []
    for row in rows[-12:]:
        content = " ".join(str(row["content"]).split())
        if len(content) > 220:
            content = f"{content[:217]}..."
        extracts.append(f"{row['role']}: {content}")

    return "Earlier conversation summary:\n" + "\n".join(extracts)


def _load_summary(cur, conv_id: int) -> ConversationSummary | None:
    cur.execute(
        """
        SELECT summary, version, updated_at
        FROM conversation_summaries
        WHERE conversation_id = %s
        """,
        (conv_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return ConversationSummary(
        summary=row["summary"],
        version=row["version"],
        updated_at=row["updated_at"],
    )


def _refresh_conversation_summary(cur, conv_id: int, message_limit: int) -> ConversationSummary | None:
    cur.execute(
        """
        SELECT id, role, content
        FROM messages
        WHERE conversation_id = %s
          AND role <> 'tool'
        ORDER BY id ASC
        """,
        (conv_id,),
    )
    rows = cur.fetchall()
    if len(rows) <= message_limit:
        return _load_summary(cur, conv_id)

    older_rows = rows[: -message_limit]
    summary_text = _summarize_messages(older_rows)
    cur.execute(
        """
        INSERT INTO conversation_summaries (conversation_id, summary, version, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (conversation_id)
        DO UPDATE SET
            summary = EXCLUDED.summary,
            version = EXCLUDED.version,
            updated_at = NOW()
        RETURNING summary, version, updated_at
        """,
        (conv_id, summary_text, AGENT_MEMORY_VERSION),
    )
    row = cur.fetchone()
    return ConversationSummary(
        summary=row["summary"],
        version=row["version"],
        updated_at=row["updated_at"],
    )


def _fit_messages_to_budget(
    messages: list[tuple[str, str]],
    summary: ConversationSummary | None,
    token_budget: int,
) -> AgentContext:
    budget = max(1, token_budget)
    selected_reversed: list[tuple[str, str]] = []
    token_estimate = 0
    truncated = False

    for role, content in reversed(messages):
        message_tokens = estimate_tokens(content)
        if token_estimate + message_tokens > budget:
            truncated = True
            break
        selected_reversed.append((role, content))
        token_estimate += message_tokens

    selected = list(reversed(selected_reversed))
    included_summary = None
    if summary:
        summary_tokens = estimate_tokens(summary.summary)
        if token_estimate + summary_tokens <= budget:
            included_summary = summary
            token_estimate += summary_tokens
        else:
            truncated = True

    if len(selected) < len(messages):
        truncated = True

    return AgentContext(
        messages=selected,
        summary=included_summary,
        token_estimate=token_estimate,
        truncated=truncated,
    )


def load_agent_context(
    session_id: str,
    user_id: str,
    message_limit: int = 30,
    token_budget: int = 3000,
) -> AgentContext:
    """Return recent, non-tool, user-owned context for the model."""
    conv_id = get_owned_conversation_id(session_id, user_id)
    if conv_id is None:
        return AgentContext(messages=[], summary=None, token_estimate=0, truncated=False)

    with _connect() as conn:
        with conn.cursor() as cur:
            summary = _refresh_conversation_summary(cur, conv_id, message_limit)
            cur.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id = %s
                  AND role <> 'tool'
                ORDER BY id DESC
                LIMIT %s
                """,
                (conv_id, message_limit),
            )
            rows = list(reversed(cur.fetchall()))
        conn.commit()

    messages = [(r["role"], r["content"]) for r in rows]
    return _fit_messages_to_budget(messages, summary, token_budget)

#load the history for a specific session (when clicking on a session in the history bar we need the fetch the messages in that session this what this fct do)
# limit is the amx messages to return (prevent loading huge nb of messages at once )
#-> list[tuple] tells you what it returns: a list of tuples like [("user", "hello"), ("assistant", "hi!")]
def load_history(
    session_id: str,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple]:
    """
    Paginated history.  Returns list of (role, content) tuples, oldest first.
    Default: last 50 messages.
    """
    conv_id = get_owned_conversation_id(session_id, user_id)
    if conv_id is None:
        return []

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id = %s
                ORDER BY id ASC
                LIMIT %s OFFSET %s
                """,
                (conv_id, limit, offset),
            )
            rows = cur.fetchall()

    return [(r["role"], r["content"]) for r in rows]


# ── Maintenance ───────────────────────────────────────────────────────────────

def cleanup_old_sessions(older_than_days: int = 90):
    """Hard-delete sessions (and cascaded messages) soft-deleted > N days ago."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM conversations
                WHERE deleted_at < NOW() - INTERVAL '%s days'
                """,
                (older_than_days,),
            )
            deleted = cur.rowcount
        conn.commit()
    log.info(f"Cleanup: removed {deleted} old sessions (>{older_than_days} days)")
    return deleted
