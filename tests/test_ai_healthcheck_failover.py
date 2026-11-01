import importlib.util
import sys
import types
import unittest
from unittest.mock import patch

if importlib.util.find_spec("aiosqlite") is None:
    # This test exercises the AI check only; database behavior is outside its scope.
    sys.modules["aiosqlite"] = types.ModuleType("aiosqlite")

from backend.services.health_check_service import HealthCheckService


class FakeProvider:
    provider_type = "openai_compatible"
    model = "qwen3.8-flash"
    last_used_fallback = True
    fallback_provider = type("Fallback", (), {"model": "qwen3.8-gufo"})()

    def __init__(self):
        self.generated = False
        self.closed = False

    async def generate(self, system_prompt, user_prompt, max_tokens=16000, temperature=0.7, request_timeout=None):
        self.generated = True
        self.request_timeout = request_timeout
        return "OK"

    async def close(self):
        self.closed = True


class AIHealthCheckFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_check_accepts_successful_fallback_generation(self):
        provider = FakeProvider()
        with patch("backend.services.ai_providers.get_ai_provider", return_value=provider):
            result = await HealthCheckService()._check_openai_compatible_provider()

        self.assertEqual(result["status"], "success")
        self.assertIn("fallback", result["message"].lower())
        self.assertIn("qwen3.8-gufo", result["message"])
        self.assertTrue(provider.generated)
        self.assertEqual(provider.request_timeout, 15.0)
        self.assertTrue(provider.closed)


if __name__ == "__main__":
    unittest.main()
