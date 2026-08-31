"""Idempotency storage for retry-safe chat requests."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.core.db import transaction

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


class IdempotencyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredIdempotentResult:
    response: dict[str, Any] | None
    in_progress: bool


def request_hash(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def begin_request(user_id: str, key: str | None, payload: dict[str, Any]) -> StoredIdempotentResult | None:
    if not key:
        return None
    h = request_hash(payload)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_hash, response_json, status
                FROM idempotency_keys
                WHERE user_id = %s AND idempotency_key = %s AND expires_at > NOW()
                FOR UPDATE
                """,
                (user_id, key),
            )
            row = cur.fetchone()
            if row:
                if row["request_hash"] != h:
                    raise IdempotencyConflictError("Idempotency-Key was reused with a different request body.")
                if row["status"] == "completed":
                    return StoredIdempotentResult(response=row["response_json"], in_progress=False)
                return StoredIdempotentResult(response=None, in_progress=True)
            cur.execute(
                """
                INSERT INTO idempotency_keys (
                    user_id, idempotency_key, request_hash, status, expires_at
                )
                VALUES (%s, %s, %s, 'in_progress', %s)
                """,
                (user_id, key, h, expires_at),
            )
    return None


def complete_request(user_id: str, key: str | None, response: dict[str, Any]) -> None:
    if not key:
        return
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE idempotency_keys
                SET status = 'completed', response_json = %s, completed_at = NOW()
                WHERE user_id = %s AND idempotency_key = %s
                """,
                (json.dumps(response), user_id, key),
            )


def fail_request(user_id: str, key: str | None) -> None:
    if not key:
        return
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE idempotency_keys
                SET status = 'failed', completed_at = NOW()
                WHERE user_id = %s AND idempotency_key = %s AND status = 'in_progress'
                """,
                (user_id, key),
            )
