"""Database connection management for PostgreSQL."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PGConnection
from psycopg2.pool import ThreadedConnectionPool

from backend.core.logger import get_logger

log = get_logger(__name__)

_pool: ThreadedConnectionPool | None = None


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return url


def init_pool(minconn: int | None = None, maxconn: int | None = None) -> None:
    """Initialise the process-wide PostgreSQL pool once."""
    global _pool
    if _pool is not None:
        return
    min_size = minconn or int(os.getenv("DB_POOL_MIN", "1"))
    max_size = maxconn or int(os.getenv("DB_POOL_MAX", "10"))
    _pool = ThreadedConnectionPool(
        min_size,
        max_size,
        database_url(),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    log.info(f"PostgreSQL pool initialised min={min_size} max={max_size}")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_connection() -> Iterator[PGConnection]:
    """Yield a pooled connection, falling back to a direct connection in tests."""
    if _pool is None:
        conn = psycopg2.connect(
            database_url(),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        try:
            yield conn
        finally:
            conn.close()
        return

    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


@contextmanager
def transaction() -> Iterator[PGConnection]:
    """Commit on success and rollback on any exception."""
    with get_connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def readiness_check() -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception as exc:
        log.error(f"DB readiness check failed: {exc}")
        return False
