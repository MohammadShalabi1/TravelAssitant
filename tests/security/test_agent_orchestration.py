from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")


class _LoggerStub:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


logger_stub = types.ModuleType("backend.core.logger")
logger_stub.get_logger = lambda _name=None: _LoggerStub()
sys.modules.setdefault("backend.core.logger", logger_stub)
sys.modules.setdefault("core.logger", logger_stub)

from backend.agent import loop as agent_loop  # noqa: E402


SESSION = "session-a"
USER = "user-a"


class FakeCall:
    def __init__(self, name: str, args: dict):
        self.name = name
        self.args = args


class FakeResponse:
    def __init__(self, text: str = "", function_calls=None):
        self.text = text
        self.function_calls = function_calls or []


class FakeChat:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.messages: list[str] = []

    def send_message(self, message: str) -> FakeResponse:
        self.messages.append(message)
        return self.responses.pop(0)


class FakeChats:
    def __init__(self, chat: FakeChat):
        self.chat = chat
        self.created_with = None

    def create(self, **kwargs):
        self.created_with = kwargs
        return self.chat


class FakeClient:
    def __init__(self, chat: FakeChat):
        self.chats = FakeChats(chat)


class AgentOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_messages: list[tuple[str, str, str, str]] = []
        self.cache_sets = []
        self.patchers = [
            patch.object(agent_loop, "load_history", lambda _sid, _uid, limit=10: []),
            patch.object(agent_loop, "choose_model", lambda _message: "test-model"),
            patch.object(agent_loop, "classify_cache_request", self._cache_policy),
            patch.object(agent_loop, "build_cache_identity", self._cache_identity),
            patch.object(agent_loop, "hash_context", lambda _history: "context-hash"),
            patch.object(agent_loop, "get_cache", lambda _message, _identity: None),
            patch.object(agent_loop, "set_cache", self._set_cache),
            patch.object(agent_loop, "save_message", self._save_message),
            patch.object(agent_loop, "record_cache", lambda hit: None),
            patch.object(agent_loop, "record_tool_call", lambda _name, success=True: None),
            patch.object(agent_loop, "record_blocked_tool_call", lambda _name, _reason: None),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _cache_policy(self, _message: str, has_context: bool = False):
        return SimpleNamespace(
            kind=SimpleNamespace(value="non_cacheable"),
            action=agent_loop.CacheAction.BYPASS,
            ttl_seconds=0,
            reason="test_bypass",
        )

    def _cache_identity(self, *_args, **_kwargs):
        return SimpleNamespace(user_scope="global")

    def _save_message(self, session_id: str, user_id: str, role: str, content: str):
        self.saved_messages.append((session_id, user_id, role, content))
        return True

    def _set_cache(self, *args):
        self.cache_sets.append(args)

    def test_run_single_turn_saves_user_and_assistant_messages_in_order(self) -> None:
        chat = FakeChat([FakeResponse("Final answer")])

        result = agent_loop.run_single_turn(SESSION, USER, "hello", FakeClient(chat))

        self.assertEqual(result, {"text": "Final answer", "tools_used": [], "cached": False})
        self.assertEqual(
            [(role, content) for *_ids, role, content in self.saved_messages],
            [("user", "hello"), ("assistant", "Final answer")],
        )

    def test_cached_answer_bypasses_model_and_persists_assistant_only(self) -> None:
        def allow_policy(_message: str, has_context: bool = False):
            return SimpleNamespace(
                kind=SimpleNamespace(value="global_stateless"),
                action=agent_loop.CacheAction.ALLOW,
                ttl_seconds=600,
                reason="test_allow",
            )

        chat = FakeChat([FakeResponse("should not be used")])
        client = FakeClient(chat)
        with (
            patch.object(agent_loop, "classify_cache_request", allow_policy),
            patch.object(agent_loop, "get_cache", lambda _message, _identity: "Cached answer"),
        ):
            result = agent_loop.run_single_turn(SESSION, USER, "weather in paris", client)

        self.assertEqual(result, {"text": "Cached answer", "tools_used": [], "cached": True})
        self.assertIsNone(client.chats.created_with)
        self.assertEqual(
            [(role, content) for *_ids, role, content in self.saved_messages],
            [("assistant", "Cached answer")],
        )

    def test_model_selection_result_is_used_when_creating_chat(self) -> None:
        chat = FakeChat([FakeResponse("Final answer")])
        client = FakeClient(chat)

        with patch.object(agent_loop, "choose_model", lambda _message: "gemini-test-route"):
            agent_loop.run_single_turn(SESSION, USER, "plan rome", client)

        self.assertEqual(client.chats.created_with["model"], "gemini-test-route")
        self.assertEqual(len(client.chats.created_with["history"]), 0)

    def test_tool_calls_are_executed_and_tool_results_are_persisted(self) -> None:
        call = FakeCall("get_coordinates", {"location": "Paris"})
        chat = FakeChat(
            [
                FakeResponse(function_calls=[call]),
                FakeResponse("Paris is ready."),
            ]
        )
        tool_contexts = []

        def fake_tool(name, args, context):
            tool_contexts.append((name, args, context))
            return "UNTRUSTED_TOOL_DATA tool=get_coordinates: Lat: 48.85", True, False

        with patch.object(agent_loop, "_call_tool_with_retry", fake_tool):
            result = agent_loop.run_single_turn(
                SESSION,
                USER,
                "weather in Paris",
                FakeClient(chat),
                security_restricted=True,
            )

        self.assertEqual(result["tools_used"], ["get_coordinates"])
        self.assertEqual(tool_contexts[0][0], "get_coordinates")
        self.assertEqual(tool_contexts[0][1], {"location": "Paris"})
        self.assertTrue(tool_contexts[0][2].restricted)
        self.assertEqual(
            [(role, content) for *_ids, role, content in self.saved_messages],
            [
                ("user", "weather in Paris"),
                ("tool", "UNTRUSTED_TOOL_DATA tool=get_coordinates: Lat: 48.85"),
                ("assistant", "Paris is ready."),
            ],
        )

    def test_max_tool_loop_bound_is_preserved(self) -> None:
        call = FakeCall("get_coordinates", {"location": "Paris"})
        chat = FakeChat([FakeResponse(function_calls=[call]) for _ in range(4)])

        with (
            patch.object(agent_loop, "MAX_TOOL_LOOPS", 2),
            patch.object(
                agent_loop,
                "_call_tool_with_retry",
                lambda _name, _args, _context: ("tool result", True, False),
            ),
        ):
            result = agent_loop.run_single_turn(SESSION, USER, "weather in Paris", FakeClient(chat))

        self.assertEqual(result["tools_used"], ["get_coordinates", "get_coordinates"])
        self.assertEqual(
            [(role, content) for *_ids, role, content in self.saved_messages],
            [
                ("user", "weather in Paris"),
                ("tool", "tool result"),
                ("tool", "tool result"),
                ("assistant", ""),
            ],
        )

    def test_tool_failure_fallback_behavior_is_preserved(self) -> None:
        call = FakeCall("get_coordinates", {"location": "Paris"})
        chat = FakeChat(
            [
                FakeResponse(function_calls=[call]),
                FakeResponse(""),
            ]
        )

        with patch.object(
            agent_loop,
            "_call_tool_with_retry",
            lambda _name, _args, _context: ("Tool request rejected or unavailable.", False, False),
        ):
            result = agent_loop.run_single_turn(SESSION, USER, "weather in Paris", FakeClient(chat))

        self.assertIn("wasn't able to retrieve", result["text"])
        self.assertEqual(result["tools_used"], ["get_coordinates"])
        self.assertEqual(self.saved_messages[-1][2], "assistant")
        self.assertIn("wasn't able to retrieve", self.saved_messages[-1][3])


if __name__ == "__main__":
    unittest.main()
