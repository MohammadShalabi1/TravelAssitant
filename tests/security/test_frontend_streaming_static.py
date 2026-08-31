import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendStreamingStaticTests(unittest.TestCase):
    def test_frontend_uses_v1_and_streaming_chat(self):
        api_js = (ROOT / "frontend" / "src" / "lib" / "api.js").read_text()
        use_chat = (ROOT / "frontend" / "src" / "hooks" / "useChat.js").read_text()

        self.assertIn('const API_PREFIX = "/api/v1"', api_js)
        self.assertIn("/chat/stream", api_js)
        self.assertIn("sendMessageStream", api_js)
        self.assertIn("sendMessageStream", use_chat)
        self.assertIn("onDelta", use_chat)


if __name__ == "__main__":
    unittest.main()
