from __future__ import annotations

import inspect
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

cache_stub = types.ModuleType("backend.core.cache")
cache_stub.CacheAction = types.SimpleNamespace(ALLOW="allow", BYPASS="bypass")
cache_stub.get_cache = lambda _key: None
cache_stub.set_cache = lambda _key, _value, _ttl: None
cache_stub.get_tool_cache = lambda _tool_name, _args: None
cache_stub.set_tool_cache = lambda _tool_name, _args, _result: None
cache_stub.get_ttl = lambda _query: 60
cache_stub.build_cache_identity = lambda *args, **kwargs: types.SimpleNamespace(
    user_scope="global"
)
cache_stub.classify_cache_request = lambda *args, **kwargs: types.SimpleNamespace(
    action="bypass",
    kind=types.SimpleNamespace(value="non_cacheable"),
    reason="test",
)
cache_stub.hash_context = lambda _messages: "empty"
sys.modules.setdefault("backend.core.cache", cache_stub)

class _LoggerStub:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def exception(self, *_args, **_kwargs):
        pass


logger_stub = types.ModuleType("backend.core.logger")
logger_stub.get_logger = lambda _name=None: _LoggerStub()
sys.modules.setdefault("backend.core.logger", logger_stub)
sys.modules.setdefault("core.logger", logger_stub)

import agent.loop as agent_loop  # noqa: E402
import agent.memory as agent_memory  # noqa: E402
from backend import api  # noqa: E402
from backend.core.auth import CurrentUser  # noqa: E402


USER_A = CurrentUser(user_id="user-a", email="a@example.com")
USER_B = CurrentUser(user_id="user-b", email="b@example.com")
SESSION_A = "session-owned-by-a"


class SessionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides[api.get_current_user] = lambda: USER_B
        self.client = TestClient(api.app)
        self.patchers = [
            patch.object(api, "check_ip_rate_limit", lambda _ip: True),
            patch.object(api, "check_rate_limit", lambda _session_id: True),
            patch.object(api, "check_spam", lambda _ip, _message: False),
            patch.object(api, "validate_input", lambda _message: (True, None)),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        api.app.dependency_overrides.clear()

    def _patch_owned_session_for_user_a_only(self):
        def fake_get_owned_conversation_id(session_id: str, user_id: str) -> int | None:
            if session_id == SESSION_A and user_id == USER_A.user_id:
                return 123
            return None

        patcher = patch.object(
            api,
            "get_owned_conversation_id",
            fake_get_owned_conversation_id,
        )
        self.patchers.append(patcher)
        return patcher.start()

    def test_cross_user_history_returns_404(self) -> None:
        self._patch_owned_session_for_user_a_only()
        load_called = False

        def fake_load_history(*_args, **_kwargs):
            nonlocal load_called
            load_called = True
            return [("user", "private message")]

        patcher = patch.object(api, "load_history", fake_load_history)
        self.patchers.append(patcher)
        patcher.start()

        response = self.client.get(f"/api/sessions/{SESSION_A}/history")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(load_called)

    def test_cross_user_export_returns_404(self) -> None:
        self._patch_owned_session_for_user_a_only()
        load_called = False

        def fake_load_history(*_args, **_kwargs):
            nonlocal load_called
            load_called = True
            return [("assistant", "private answer")]

        patcher = patch.object(api, "load_history", fake_load_history)
        self.patchers.append(patcher)
        patcher.start()

        response = self.client.get(f"/api/sessions/{SESSION_A}/export")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(load_called)

    def test_cross_user_rename_returns_404(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def fake_rename_session(session_id: str, user_id: str, name: str) -> bool:
            calls.append((session_id, user_id, name))
            return False

        patcher = patch.object(api, "rename_session", fake_rename_session)
        self.patchers.append(patcher)
        patcher.start()

        response = self.client.patch(
            f"/api/sessions/{SESSION_A}/rename",
            json={"name": "stolen name"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(calls, [(SESSION_A, USER_B.user_id, "stolen name")])

    def test_cross_user_delete_returns_404(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_delete_session(session_id: str, user_id: str) -> bool:
            calls.append((session_id, user_id))
            return False

        patcher = patch.object(api, "delete_session", fake_delete_session)
        self.patchers.append(patcher)
        patcher.start()

        response = self.client.delete(f"/api/sessions/{SESSION_A}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(calls, [(SESSION_A, USER_B.user_id)])

    def test_cross_user_chat_returns_404_without_running_agent(self) -> None:
        self._patch_owned_session_for_user_a_only()
        run_called = False

        def fake_run_single_turn(*_args, **_kwargs):
            nonlocal run_called
            run_called = True
            return {"text": "should not run", "tools_used": [], "cached": False}

        patcher = patch.object(api, "run_single_turn", fake_run_single_turn)
        self.patchers.append(patcher)
        patcher.start()

        response = self.client.post(
            "/api/chat",
            json={"session_id": SESSION_A, "message": "hello"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(run_called)

    def test_owned_session_routes_continue_to_work(self) -> None:
        api.app.dependency_overrides[api.get_current_user] = lambda: USER_A
        patch_specs = [
            patch.object(api, "get_owned_conversation_id", lambda _sid, _uid: 123),
            patch.object(
                api,
                "load_history",
                lambda session_id, user_id, limit=50, offset=0: [
                    ("user", f"history for {user_id}"),
                    ("tool", "internal trace"),
                    ("assistant", f"answer in {session_id}"),
                ],
            ),
            patch.object(api, "rename_session", lambda _sid, _uid, _name: True),
            patch.object(api, "delete_session", lambda _sid, _uid: True),
            patch.object(
                api,
                "run_single_turn",
                lambda session_id, user_id, message, _client: {
                    "text": f"{user_id}:{session_id}:{message}",
                    "tools_used": [],
                    "cached": False,
                },
            ),
        ]
        self.patchers.extend(patch_specs)
        for patcher in patch_specs:
            patcher.start()

        history_response = self.client.get(f"/api/sessions/{SESSION_A}/history")
        export_response = self.client.get(f"/api/sessions/{SESSION_A}/export")
        rename_response = self.client.patch(
            f"/api/sessions/{SESSION_A}/rename",
            json={"name": "Paris trip"},
        )
        delete_response = self.client.delete(f"/api/sessions/{SESSION_A}")
        chat_response = self.client.post(
            "/api/chat",
            json={"session_id": SESSION_A, "message": "hello"},
        )

        self.assertEqual(history_response.status_code, 200)
        self.assertEqual(
            history_response.json()["messages"],
            [
                {"role": "user", "content": "history for user-a"},
                {"role": "assistant", "content": f"answer in {SESSION_A}"},
            ],
        )
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response.json()["messages"],
            [
                {"role": "user", "content": "history for user-a"},
                {"role": "assistant", "content": f"answer in {SESSION_A}"},
            ],
        )
        self.assertEqual(rename_response.status_code, 204)
        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(chat_response.status_code, 200)
        self.assertEqual(chat_response.json()["text"], f"user-a:{SESSION_A}:hello")

    def test_private_repository_and_agent_functions_require_user_scope(self) -> None:
        required_user_scoped_functions = [
            agent_memory.get_owned_conversation_id,
            agent_memory.load_history,
            agent_memory.save_message,
            agent_memory.rename_session,
            agent_memory.delete_session,
            agent_loop.run_single_turn,
        ]

        for fn in required_user_scoped_functions:
            self.assertIn("user_id", inspect.signature(fn).parameters)

        self.assertFalse(hasattr(agent_memory, "get_conversation_id"))


if __name__ == "__main__":
    unittest.main()
