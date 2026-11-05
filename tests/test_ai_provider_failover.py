import os
import traceback
import unittest
from unittest.mock import patch

import httpx

from backend.services.ai_providers import AIProvider, get_ai_provider


class AIProviderFailoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AI_PROVIDER": "openai_compatible",
            "AI_API_KEY": "primary-test-key",
            "AI_MODEL": "qwen3.8-flash",
            "AI_BASE_URL": "http://primary.test/v1/chat/completions",
            "AI_FALLBACK_MODEL": "qwen3.8-gufo",
            "AI_FALLBACK_BASE_URL": "http://fallback.test/v1/chat/completions",
            "AI_FALLBACK_API_KEY": "fallback-test-key",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def completion(content):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    async def test_unreachable_primary_uses_configured_fallback(self):
        primary = get_ai_provider()
        fallback = primary.fallback_provider
        requests = []

        def primary_transport(request):
            requests.append(("primary", request.url.path, request.headers.get("Authorization")))
            raise httpx.ConnectError("primary offline", request=request)

        def fallback_transport(request):
            requests.append(("fallback", request.url.path, request.headers.get("Authorization")))
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "qwen3.8-gufo"}]})
            return self.completion("used fallback")

        primary.client = httpx.AsyncClient(transport=httpx.MockTransport(primary_transport))
        fallback.client = httpx.AsyncClient(transport=httpx.MockTransport(fallback_transport))
        try:
            result = await primary.generate("system", "user")
        finally:
            await primary.close()
            await fallback.close()

        self.assertEqual(result, "used fallback")
        self.assertEqual(requests, [
            ("primary", "/v1/models", "Bearer primary-test-key"),
            ("fallback", "/v1/chat/completions", "Bearer fallback-test-key"),
        ])
        self.assertTrue(primary.last_used_fallback)

    async def test_primary_http_503_uses_fallback(self):
        primary = get_ai_provider()
        fallback = primary.fallback_provider
        primary.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (
            httpx.Response(200, json={"data": []})
            if request.url.path.endswith("/models")
            else httpx.Response(503, json={"error": "unavailable"})
        )))
        fallback.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.completion("fallback after 503")))
        try:
            result = await primary.generate("system", "user")
        finally:
            await primary.close()
            await fallback.close()
        self.assertEqual(result, "fallback after 503")
        self.assertTrue(primary.last_used_fallback)

    async def test_invalid_request_does_not_mask_primary_http_400(self):
        primary = get_ai_provider()
        fallback = primary.fallback_provider
        requests = []
        def primary_transport(request):
            requests.append("primary")
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": []})
            return httpx.Response(400, json={"error": "bad request"})
        def fallback_transport(request):
            requests.append("fallback")
            return self.completion("must not be used")
        primary.client = httpx.AsyncClient(transport=httpx.MockTransport(primary_transport))
        fallback.client = httpx.AsyncClient(transport=httpx.MockTransport(fallback_transport))
        try:
            with self.assertRaises(httpx.HTTPStatusError):
                await primary.generate("system", "user")
        finally:
            await primary.close()
            await fallback.close()
        self.assertEqual(requests, ["primary", "primary"])
        self.assertFalse(primary.last_used_fallback)

    async def test_missing_model_reported_as_http_400_uses_fallback(self):
        primary = get_ai_provider()
        fallback = primary.fallback_provider
        primary.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (
            httpx.Response(200, json={"data": []})
            if request.url.path.endswith("/models")
            else httpx.Response(400, json={"error": {"message": "model qwen3.8-flash not found"}})
        )))
        fallback.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.completion("fallback for missing model")))
        try:
            result = await primary.generate("system", "user")
        finally:
            await primary.close()
            await fallback.close()
        self.assertEqual(result, "fallback for missing model")
        self.assertTrue(primary.last_used_fallback)

    async def test_google_http_error_does_not_expose_api_key(self):
        api_key = "google-test-sentinel-key"
        provider = AIProvider(
            provider_type="google",
            api_key=api_key,
            model="gemini-test",
            base_url="https://google.test/v1beta",
        )
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429, json={"error": "rate limited"}, request=request
            )
        ))
        try:
            with self.assertRaises(Exception) as raised:
                await provider.generate("system", "user")
        finally:
            await provider.close()

        error_text = str(raised.exception) + "".join(
            traceback.format_exception(
                type(raised.exception), raised.exception, raised.exception.__traceback__
            )
        )
        self.assertNotIn(api_key, error_text)
        self.assertIn("429", error_text)

    async def test_partial_fallback_configuration_is_rejected(self):
        os.environ.pop("AI_FALLBACK_API_KEY")
        with self.assertRaisesRegex(ValueError, "AI_FALLBACK_API_KEY"):
            get_ai_provider()

    async def test_fallback_without_configuration_preserves_primary_provider(self):
        os.environ.pop("AI_FALLBACK_MODEL")
        os.environ.pop("AI_FALLBACK_BASE_URL")
        os.environ.pop("AI_FALLBACK_API_KEY")
        provider = get_ai_provider()
        try:
            self.assertIsNone(provider.fallback_provider)
            self.assertEqual(provider.model, "qwen3.8-flash")
        finally:
            await provider.close()


if __name__ == "__main__":
    unittest.main()
