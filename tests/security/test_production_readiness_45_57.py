from __future__ import annotations

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

from backend import api  # noqa: E402
from backend.core import concurrency, rate_limit  # noqa: E402
from backend.core.auth import CurrentUser  # noqa: E402
from backend.integrations import http as resilient_http  # noqa: E402
from evals.runner import evaluate, heuristic_runner, load_dataset  # noqa: E402


USER = CurrentUser(user_id="user-a", email="a@example.com")
SESSION = "session-a"


class ProductionReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides[api.get_current_user] = lambda: USER
        self.client = TestClient(api.app)
        self.patchers = [
            patch.object(api, "check_ip_rate_limit", lambda _ip: True),
            patch.object(api, "check_ai_user_rate_limit", lambda _uid: rate_limit.RateLimitResult(True, 10, 0)),
            patch.object(api, "check_rate_limit", lambda _session_id: True),
            patch.object(api, "check_spam", lambda _ip, _message: False),
            patch.object(api, "validate_input", lambda _message: (True, "")),
            patch.object(api, "get_owned_conversation_id", lambda _sid, _uid: 123),
            patch.object(api, "begin_request", lambda *_args, **_kwargs: None),
            patch.object(api, "complete_request", lambda *_args, **_kwargs: None),
            patch.object(api, "fail_request", lambda *_args, **_kwargs: None),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        api.app.dependency_overrides.clear()

    def test_eval_dataset_and_thresholds_are_executable(self) -> None:
        dataset = load_dataset()
        self.assertGreaterEqual(len(dataset), 50)
        categories = {case["category"] for case in dataset}
        self.assertIn("weather", categories)
        self.assertIn("nearby_places", categories)
        self.assertIn("prompt_injection", categories)

        report = evaluate(heuristic_runner, dataset)

        self.assertEqual(report["case_count"], len(dataset))
        self.assertIn("prompt_version", report)
        self.assertIn("p95_latency_ms", report["metrics"])
        self.assertIn("estimated_cost_per_request", report["metrics"])
        self.assertEqual(report["threshold_failures"], [])

    def test_v1_and_legacy_chat_routes_work_with_deprecation_header(self) -> None:
        with patch.object(
            api,
            "run_single_turn",
            lambda session_id, user_id, message, _client, _restricted=False: {
                "text": f"{user_id}:{session_id}:{message}",
                "tools_used": [],
                "cached": False,
            },
        ):
            v1 = self.client.post("/api/v1/chat", json={"session_id": SESSION, "message": "hello"})
            legacy = self.client.post("/api/chat", json={"session_id": SESSION, "message": "hello"})

        self.assertEqual(v1.status_code, 200)
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.headers["Deprecation"], "true")

    def test_openapi_has_error_schema_examples_and_v1_paths(self) -> None:
        schema = self.client.get("/openapi.json").json()

        self.assertIn("/api/v1/chat", schema["paths"])
        self.assertIn("ErrorResponse", schema["components"]["schemas"])
        self.assertIn("IdempotencyConflict", schema["components"]["examples"])

    def test_streaming_chat_emits_structured_events_without_private_data(self) -> None:
        with patch.object(
            api,
            "run_single_turn",
            lambda *_args, **_kwargs: {"text": "Paris answer", "tools_used": ["get_coordinates"], "cached": False},
        ):
            response = self.client.post(
                "/api/v1/chat/stream",
                json={"session_id": SESSION, "message": "plan Paris"},
            )

        body = response.text
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: turn_started", body)
        self.assertIn("event: message_delta", body)
        self.assertIn("event: turn_completed", body)
        self.assertNotIn("system prompt", body.lower())

    def test_session_lock_rejects_overlap_and_releases(self) -> None:
        with patch.object(concurrency, "get_redis", lambda: None):
            lock = concurrency.acquire_session_lock("same-session")
            with self.assertRaises(concurrency.SessionBusyError):
                concurrency.acquire_session_lock("same-session")
            concurrency.release_session_lock(lock)
            second = concurrency.acquire_session_lock("same-session")
            concurrency.release_session_lock(second)

    def test_migration_contains_idempotency_and_session_name_schema(self) -> None:
        migration = (ROOT / "migrations" / "versions" / "20260831_0001_initial.py").read_text()
        self.assertIn("idempotency_keys", migration)
        self.assertIn('"name"', migration)
        self.assertIn("conversation_summaries", migration)

    def test_resilient_http_opens_circuit_after_repeated_provider_failures(self) -> None:
        class TimeoutSession:
            def request(self, *_args, **_kwargs):
                raise resilient_http.requests.Timeout()

        with patch.object(resilient_http, "_session", TimeoutSession()):
            for _ in range(3):
                with self.assertRaises(resilient_http.ProviderUnavailableError):
                    resilient_http.request_json("test-provider", "GET", "https://example.invalid", retries=0)

            with self.assertRaises(resilient_http.ProviderUnavailableError):
                resilient_http.request_json("test-provider", "GET", "https://example.invalid", retries=0)


if __name__ == "__main__":
    unittest.main()
