import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from analog_design.model_clients import ModelClient, ModelConfig, NoRedirect, ProviderError, extract_reply, strict_json


SCHEMA = {"type": "object", "properties": {"value": {"type": "number"}},
          "required": ["value"], "additionalProperties": False}
MESSAGES = [{"role": "user", "content": "Return JSON"},
            {"role": "assistant", "content": "{}"},
            {"role": "user", "content": "Please correct the JSON"}]


def chat_response(content='{"value": 1}'):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": content, "reasoning_content": "This must not be parsed as the answer."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def mock_response(data):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(data).encode()
    return response


class ModelClientTests(unittest.TestCase):
    def test_openai_uses_responses_schema_and_preserves_exact_model(self):
        client = ModelClient(ModelConfig(provider="openai", model="chosen-model-id"))
        request = client.prepare("system instructions", MESSAGES, SCHEMA)
        self.assertEqual(request["url"], "https://api.openai.com/v1/responses")
        body = request["body"]
        self.assertEqual(body["model"], "chosen-model-id")
        self.assertFalse(body["store"])
        self.assertEqual(body["text"]["format"]["schema"], SCHEMA)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertNotIn("temperature", body)

    def test_gemini_native_body_roles_schema_and_model_path(self):
        client = ModelClient(ModelConfig(provider="gemini", model="models/example-gemini"))
        request = client.prepare("system", MESSAGES, SCHEMA)
        self.assertTrue(request["url"].endswith("/models/example-gemini:generateContent"))
        self.assertEqual([item["role"] for item in request["body"]["contents"]], ["user", "model", "user"])
        self.assertEqual(request["body"]["generationConfig"]["responseJsonSchema"], SCHEMA)
        self.assertEqual(request["body"]["systemInstruction"]["parts"][0]["text"], "system")

    def test_gemini_omits_unsupported_string_limits_without_mutating_local_schema(self):
        schema = {"type": "string", "minLength": 5, "maxLength": 30}
        client = ModelClient(ModelConfig(provider="gemini", model="example"))
        remote = client.prepare("s", MESSAGES, schema)["body"]["generationConfig"]["responseJsonSchema"]
        self.assertEqual(remote, {"type": "string"})
        self.assertEqual(schema["minLength"], 5)

    def test_deepseek_and_zai_use_chat_and_json_object(self):
        for provider, base in (("deepseek", "https://api.deepseek.com"), ("zai", "https://api.z.ai/api/paas/v4")):
            with self.subTest(provider=provider):
                client = ModelClient(ModelConfig(provider=provider, model="exact-model-id"))
                request = client.prepare("system", MESSAGES, SCHEMA)
                self.assertEqual(request["url"], base + "/chat/completions")
                self.assertEqual(request["body"]["response_format"], {"type": "json_object"})
                self.assertFalse(request["body"]["stream"])

    def test_local_model_can_omit_auth_and_select_json_mode(self):
        for mode in ("prompt", "object", "schema"):
            client = ModelClient(ModelConfig(provider="openai-compatible", model="organization/served-model",
                                             base_url="http://127.0.0.1:8000/v1/", json_mode=mode))
            client.check_credentials()
            request = client.prepare("system", MESSAGES, SCHEMA)
            self.assertEqual(request["body"]["model"], "organization/served-model")
            self.assertEqual("response_format" in request["body"], mode != "prompt")

    def test_provider_options_cannot_replace_messages_or_budget(self):
        for options in ({"messages": []}, {"max_tokens": 1000000}, {"tools": []}):
            with self.assertRaises(ValueError):
                ModelConfig(provider="deepseek", model="example", options=options)
        config = ModelConfig(provider="deepseek", model="example", options={"thinking": {"type": "disabled"}})
        self.assertEqual(ModelClient(config).prepare("s", MESSAGES, SCHEMA)["body"]["thinking"], {"type": "disabled"})

    def test_missing_credentials_fail_before_transport(self):
        client = ModelClient(ModelConfig(provider="openai", model="example", api_key_env="TEST_MODEL_KEY"))
        with patch.dict(os.environ, {}, clear=True), patch("analog_design.model_clients.build_opener") as opener:
            with self.assertRaisesRegex(ProviderError, "Set TEST_MODEL_KEY"):
                client.complete(client.prepare("s", MESSAGES, SCHEMA))
            opener.assert_not_called()

    def test_credentials_are_headers_and_never_saved_in_request_or_reply(self):
        for provider in ("gemini", "deepseek"):
            client = ModelClient(ModelConfig(provider=provider, model="example", api_key_env="TEST_MODEL_KEY"))
            request = client.prepare("s", MESSAGES, SCHEMA)
            data = ({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "test-secret-123"}]}}]}
                    if provider == "gemini" else chat_response("test-secret-123"))
            with patch.dict(os.environ, {"TEST_MODEL_KEY": "test-secret-123"}), \
                 patch("analog_design.model_clients.build_opener") as build:
                build.return_value.open.return_value = mock_response(data)
                reply = client.complete(request)
                wire_request = build.return_value.open.call_args.args[0]
                expected = "test-secret-123" if provider == "gemini" else "Bearer test-secret-123"
                header = "X-goog-api-key" if provider == "gemini" else "Authorization"
                self.assertEqual(wire_request.get_header(header), expected)
                self.assertNotIn("test-secret-123", json.dumps([request, reply, client.describe()]))

    def test_retry_is_bounded_and_authentication_errors_are_not_retried(self):
        client = ModelClient(ModelConfig(provider="openai-compatible", model="test", base_url="http://localhost:8000/v1", retries=1))
        request = client.prepare("s", MESSAGES, SCHEMA)
        with patch("analog_design.model_clients.build_opener") as build, patch("analog_design.model_clients.time.sleep"):
            build.return_value.open.side_effect = [HTTPError(request["url"], 429, "limit", {}, io.BytesIO()), mock_response(chat_response())]
            self.assertEqual(client.complete(request)["http_attempts"], 2)
            self.assertEqual(build.return_value.open.call_count, 2)
        for status in (400, 401, 403, 404, 302):
            with patch("analog_design.model_clients.build_opener") as build:
                build.return_value.open.side_effect = HTTPError(request["url"], status, "error", {}, io.BytesIO(b"secret"))
                with self.assertRaisesRegex(ProviderError, f"HTTP {status}"):
                    client.complete(request)
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_network_failure_stops_at_retry_limit(self):
        client = ModelClient(ModelConfig(provider="openai-compatible", model="test", base_url="http://localhost:8000/v1", retries=1))
        with patch("analog_design.model_clients.build_opener") as build, patch("analog_design.model_clients.time.sleep"):
            build.return_value.open.side_effect = URLError("connection failed")
            with self.assertRaises(ProviderError):
                client.complete(client.prepare("s", MESSAGES, SCHEMA))
            self.assertEqual(build.return_value.open.call_count, 2)

    def test_provider_error_details_are_useful_and_redacted(self):
        client = ModelClient(ModelConfig(provider="openai", model="example", api_key_env="TEST_MODEL_KEY"))
        body = json.dumps({"error": {"message": "Invalid key test-secret-123"}}).encode()
        with patch.dict(os.environ, {"TEST_MODEL_KEY": "test-secret-123"}), \
             patch("analog_design.model_clients.build_opener") as build:
            build.return_value.open.side_effect = HTTPError("https://api.openai.com/v1/responses", 401, "error", {}, io.BytesIO(body))
            with self.assertRaises(ProviderError) as caught:
                client.complete(client.prepare("s", MESSAGES, SCHEMA))
            self.assertIn("Invalid key", str(caught.exception))
            self.assertNotIn("test-secret-123", str(caught.exception) + json.dumps(caught.exception.response))

    def test_extracts_final_text_without_reasoning(self):
        self.assertEqual(extract_reply("chat", chat_response())["text"], '{"value": 1}')
        response = {"status": "completed", "output": [{"type": "reasoning", "summary": []},
                    {"type": "message", "content": [{"type": "output_text", "text": '{"value": 1}'}]}]}
        self.assertEqual(extract_reply("responses", response)["text"], '{"value": 1}')
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "private thought", "thought": True}, {"text": '{"value": 1}'}]}}]}
        self.assertEqual(extract_reply("gemini", response)["text"], '{"value": 1}')

    def test_refused_truncated_empty_and_tool_responses_fail(self):
        cases = [("responses", {"status": "incomplete", "output": []}),
                 ("responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]}),
                 ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS"}]}),
                 ("gemini", {"promptFeedback": {"blockReason": "SAFETY"}}),
                 ("chat", {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}),
                 ("chat", {"choices": [{"finish_reason": "stop", "message": {"tool_calls": [{}]}}]}),
                 ("chat", chat_response("")), ("chat", [])]
        for protocol, response in cases:
            with self.subTest(protocol=protocol, response=response), self.assertRaises(ProviderError):
                extract_reply(protocol, response)

    def test_invalid_urls_limits_and_redirects(self):
        for url in ("https://user:key@example.com/v1", "https://example.com/v1?key=secret", "file:///tmp/model", "http://example.com/v1"):
            with self.assertRaises(ValueError):
                ModelConfig(provider="openai-compatible", model="test", base_url=url)
        for limits in ({"retries": 4}, {"max_output_tokens": True}, {"timeout_s": float("nan")}):
            with self.assertRaises(ValueError):
                ModelConfig(provider="openai", model="test", **limits)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_strict_json_rejects_duplicate_keys_nan_and_overflow(self):
        for value in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.assertRaises(ValueError):
                strict_json(value)


if __name__ == "__main__":
    unittest.main()
