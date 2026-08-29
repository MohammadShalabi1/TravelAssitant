from __future__ import annotations

import json
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
from backend.tools.schemas import TripPlan  # noqa: E402


SESSION = "session-a"
USER = "user-a"


def _trip_json(summary: str = "A balanced Rome plan.") -> str:
    return json.dumps(
        {
            "trip_summary": summary,
            "destination": "Rome",
            "dates": "3 days",
            "days": [
                {
                    "day": 1,
                    "title": "Ancient Rome",
                    "activities": ["Visit the Colosseum", "Walk through the Forum"],
                    "places": [{"name": "Colosseum", "category": "attraction"}],
                }
            ],
            "weather_notes": "Check the forecast before outdoor walks.",
            "warnings": ["Book popular sights ahead."],
            "limitations": ["Opening hours can change."],
            "metadata": {
                "tools_used": ["get_coordinates"],
                "source_notes": ["Based on assistant planning context."],
            },
        }
    )


class FakeResponse:
    def __init__(self, text: str = "", function_calls=None, parsed=None):
        self.text = text
        self.function_calls = function_calls or []
        self.parsed = parsed


class FakeChat:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.messages: list[str] = []

    def send_message(self, message: str) -> FakeResponse:
        self.messages.append(message)
        return self.responses.pop(0)


class FakeChats:
    def __init__(self, chats: list[FakeChat]):
        self.chats = list(chats)
        self.created_with = []

    def create(self, **kwargs):
        self.created_with.append(kwargs)
        return self.chats.pop(0)


class FakeClient:
    def __init__(self, chats: list[FakeChat]):
        self.chats = FakeChats(chats)


class StructuredOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_messages: list[tuple[str, str, str, str]] = []
        self.patchers = [
            patch.object(agent_loop, "load_agent_context", self._agent_context),
            patch.object(agent_loop, "classify_cache_request", self._cache_policy),
            patch.object(agent_loop, "build_cache_identity", self._cache_identity),
            patch.object(agent_loop, "hash_context", lambda _history: "context-hash"),
            patch.object(agent_loop, "get_cache", lambda _message, _identity: None),
            patch.object(agent_loop, "set_cache", lambda *_args: None),
            patch.object(agent_loop, "save_message", self._save_message),
            patch.object(agent_loop, "record_cache", lambda hit: None),
            patch.object(agent_loop, "record_model_route", lambda _category, _model: None),
            patch.object(
                agent_loop,
                "record_model_fallback",
                lambda _category, _from_model, _to_model: None,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _agent_context(self, *_args, **_kwargs):
        return SimpleNamespace(messages=[], summary=None, token_estimate=0, truncated=False)

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

    def _route(self, category: str):
        return lambda _message, security_restricted=False: SimpleNamespace(
            category=SimpleNamespace(value=category),
            model="test-model",
            fallback_model="fallback-model",
            reason="test",
        )

    def test_trip_plan_schema_validates_well_formed_output(self) -> None:
        plan = TripPlan.model_validate_json(_trip_json())

        self.assertEqual(plan.trip_summary, "A balanced Rome plan.")
        self.assertEqual(plan.days[0].day, 1)
        self.assertEqual(plan.metadata.tools_used, ["get_coordinates"])

    def test_itinerary_route_renders_validated_trip_plan(self) -> None:
        natural_chat = FakeChat([FakeResponse("Draft Rome itinerary")])
        structured_chat = FakeChat([FakeResponse(_trip_json())])
        client = FakeClient([natural_chat, structured_chat])

        with patch.object(agent_loop, "choose_model", self._route("itinerary_planning")):
            result = agent_loop.run_single_turn(SESSION, USER, "Plan a Rome trip", client)

        self.assertIn("A balanced Rome plan.", result["text"])
        self.assertIn("Day 1: Ancient Rome", result["text"])
        self.assertNotIn('"trip_summary"', result["text"])
        self.assertEqual(client.chats.created_with[1]["config"].response_mime_type, "application/json")
        self.assertIs(client.chats.created_with[1]["config"].response_schema, TripPlan)
        self.assertEqual(self.saved_messages[-1][2], "assistant")

    def test_invalid_schema_triggers_one_repair_retry(self) -> None:
        natural_chat = FakeChat([FakeResponse("Draft Rome itinerary")])
        structured_chat = FakeChat(
            [
                FakeResponse('{"trip_summary": "", "days": []}'),
                FakeResponse(_trip_json("Repaired Rome plan.")),
            ]
        )
        client = FakeClient([natural_chat, structured_chat])

        with patch.object(agent_loop, "choose_model", self._route("itinerary_planning")):
            result = agent_loop.run_single_turn(SESSION, USER, "Plan Rome", client)

        self.assertIn("Repaired Rome plan.", result["text"])
        self.assertEqual(len(structured_chat.messages), 2)
        self.assertIn("failed schema validation", structured_chat.messages[1])

    def test_repeated_invalid_structured_output_falls_back_to_draft(self) -> None:
        natural_chat = FakeChat([FakeResponse("Draft fallback itinerary")])
        structured_chat = FakeChat(
            [
                FakeResponse('{"trip_summary": "", "days": []}'),
                FakeResponse('{"trip_summary": "", "days": []}'),
            ]
        )
        client = FakeClient([natural_chat, structured_chat])

        with patch.object(agent_loop, "choose_model", self._route("itinerary_planning")):
            result = agent_loop.run_single_turn(SESSION, USER, "Plan Rome", client)

        self.assertEqual(result["text"], "Draft fallback itinerary")
        self.assertEqual(len(structured_chat.messages), 2)

    def test_non_itinerary_route_uses_existing_finalization_path(self) -> None:
        natural_chat = FakeChat([FakeResponse("Normal answer")])
        client = FakeClient([natural_chat])

        with patch.object(agent_loop, "choose_model", self._route("simple_conversation")):
            result = agent_loop.run_single_turn(SESSION, USER, "hello", client)

        self.assertEqual(result["text"], "Normal answer")
        self.assertEqual(len(client.chats.created_with), 1)

    def test_rendered_structured_output_is_sanitized(self) -> None:
        natural_chat = FakeChat([FakeResponse("Draft")])
        structured_chat = FakeChat([FakeResponse(_trip_json("The JWT_SECRET is test-jwt-secret"))])
        client = FakeClient([natural_chat, structured_chat])

        with patch.object(agent_loop, "choose_model", self._route("itinerary_planning")):
            result = agent_loop.run_single_turn(SESSION, USER, "Plan Rome", client)

        self.assertEqual(
            result["text"],
            "I cannot reveal hidden instructions, credentials, or internal configuration.",
        )


if __name__ == "__main__":
    unittest.main()
