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
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PGConnection

from core.logger import get_logger

log = get_logger(__name__)

DATABASE_URL: str = os.environ["DATABASE_URL"]   # must be set; fail fast if not


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
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    log.info("Database schema initialised")


# ── Session management ────────────────────────────────────────────────────────

def create_session(user_id: Optional[str] = None) -> str:
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


def get_conversation_id(session_id: str) -> Optional[int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM conversations WHERE session_id = %s AND deleted_at IS NULL",
                (session_id,),
            )
            row = cur.fetchone()
    return row["id"] if row else None


def get_all_sessions(user_id: Optional[str] = None) -> list[tuple]:
    """Return (session_id, created_at) ordered newest first."""
    with _connect() as conn:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(
                    """SELECT session_id, created_at FROM conversations
                       WHERE user_id = %s AND deleted_at IS NULL
                       ORDER BY created_at DESC""",
                    (user_id,),
                )
            else:
                cur.execute(
                    """SELECT session_id, created_at FROM conversations
                       WHERE deleted_at IS NULL
                       ORDER BY created_at DESC""",
                )
            return cur.fetchall()


def delete_session(session_id: str):
    """Soft-delete a session."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET deleted_at = NOW() WHERE session_id = %s",
                (session_id,),
            )
        conn.commit()
    log.info(f"Session soft-deleted: {session_id}")


def rename_session(session_id: str, name: str):
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
                "UPDATE conversations SET name = %s WHERE session_id = %s",
                (name, session_id),
            )
        conn.commit()


# ── Message persistence ───────────────────────────────────────────────────────

def save_message(session_id: str, role: str, content: str):
    conv_id = get_conversation_id(session_id)
    if conv_id is None:
        log.error(f"save_message: session not found: {session_id}")
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
                (conv_id, role, content),
            )
        conn.commit()


def load_history(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple]:
    """
    Paginated history.  Returns list of (role, content) tuples, oldest first.
    Default: last 50 messages.
    """
    conv_id = get_conversation_id(session_id)
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