from __future__ import annotations

import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


class _LoggerStub:
    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


logger_stub = types.ModuleType("backend.core.logger")
logger_stub.get_logger = lambda _name=None: _LoggerStub()
sys.modules.setdefault("backend.core.logger", logger_stub)

from backend.agent import memory  # noqa: E402


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

    def fetchall(self):
        return self.result or []

    def execute(self, query, params=()):
        compact = " ".join(query.lower().split())
        if compact.startswith("select id from conversations"):
            session_id, user_id = params
            row = self.db["conversations"].get((session_id, user_id))
            self.result = {"id": row["id"]} if row and row.get("deleted_at") is None else None
        elif (
            compact.startswith("select role, content from messages")
            and "role <> 'tool'" in compact
            and "order by id desc" in compact
        ):
            conv_id, limit = params
            rows = [
                {"role": row["role"], "content": row["content"]}
                for row in sorted(self.db["messages"], key=lambda item: item["id"], reverse=True)
                if row["conversation_id"] == conv_id and row["role"] != "tool"
            ]
            self.result = rows[:limit]
        elif (
            compact.startswith("select id, role, content from messages")
            and "role <> 'tool'" in compact
        ):
            conv_id = params[0]
            self.result = [
                {"id": row["id"], "role": row["role"], "content": row["content"]}
                for row in sorted(self.db["messages"], key=lambda item: item["id"])
                if row["conversation_id"] == conv_id and row["role"] != "tool"
            ]
        elif compact.startswith("select summary, version, updated_at"):
            conv_id = params[0]
            self.result = self.db["summaries"].get(conv_id)
        elif compact.startswith("insert into conversation_summaries"):
            conv_id, summary, version = params
            row = {
                "summary": summary,
                "version": version,
                "updated_at": datetime.now(timezone.utc),
            }
            self.db["summaries"][conv_id] = row
            self.result = row
        elif compact.startswith("select role, content from messages"):
            conv_id, limit, offset = params
            rows = [
                {"role": row["role"], "content": row["content"]}
                for row in sorted(self.db["messages"], key=lambda item: item["id"])
                if row["conversation_id"] == conv_id
            ]
            self.result = rows[offset : offset + limit]
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


class ContextMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = {
            "conversations": {
                ("session-a", "user-a"): {"id": 1, "deleted_at": None},
                ("session-a", "user-b"): None,
            },
            "messages": [],
            "summaries": {},
            "commits": 0,
        }
        self.patcher = patch.object(memory, "_connect", lambda: FakeConnection(self.db))
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()

    def _add_message(self, message_id: int, role: str, content: str, conversation_id: int = 1):
        self.db["messages"].append(
            {
                "id": message_id,
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
            }
        )

    def test_agent_context_uses_most_recent_messages_oldest_first(self) -> None:
        for idx in range(1, 6):
            self._add_message(idx, "user", f"message-{idx}")

        context = memory.load_agent_context("session-a", "user-a", message_limit=3)

        self.assertEqual(
            context.messages,
            [("user", "message-3"), ("user", "message-4"), ("user", "message-5")],
        )
        self.assertFalse(context.truncated)

    def test_public_load_history_keeps_existing_pagination_and_tool_rows(self) -> None:
        self._add_message(1, "user", "first")
        self._add_message(2, "tool", "internal trace")
        self._add_message(3, "assistant", "third")

        rows = memory.load_history("session-a", "user-a", limit=2, offset=0)

        self.assertEqual(rows, [("user", "first"), ("tool", "internal trace")])

    def test_agent_context_excludes_tool_traces(self) -> None:
        self._add_message(1, "user", "hello")
        self._add_message(2, "tool", "internal trace")
        self._add_message(3, "assistant", "answer")

        context = memory.load_agent_context("session-a", "user-a", message_limit=10)

        self.assertEqual(context.messages, [("user", "hello"), ("assistant", "answer")])

    def test_token_budget_preserves_newest_messages(self) -> None:
        self._add_message(1, "user", "older fits")
        self._add_message(2, "assistant", "x" * 40)
        self._add_message(3, "user", "newest")

        context = memory.load_agent_context(
            "session-a",
            "user-a",
            message_limit=10,
            token_budget=2,
        )

        self.assertEqual(context.messages, [("user", "newest")])
        self.assertTrue(context.truncated)
        self.assertLessEqual(context.token_estimate, 2)

    def test_rolling_summary_is_persisted_for_long_sessions(self) -> None:
        for idx in range(1, 33):
            self._add_message(idx, "user", f"message-{idx}")

        context = memory.load_agent_context("session-a", "user-a", message_limit=30)

        self.assertIsNotNone(context.summary)
        self.assertEqual(context.summary.version, memory.AGENT_MEMORY_VERSION)
        self.assertIn("message-1", self.db["summaries"][1]["summary"])
        self.assertIn("message-2", self.db["summaries"][1]["summary"])
        self.assertEqual(context.messages[0], ("user", "message-3"))

    def test_cross_user_context_returns_empty_without_summary(self) -> None:
        self._add_message(1, "user", "private")

        context = memory.load_agent_context("session-a", "user-b", message_limit=30)

        self.assertEqual(context.messages, [])
        self.assertIsNone(context.summary)
        self.assertEqual(self.db["summaries"], {})


if __name__ == "__main__":
    unittest.main()
