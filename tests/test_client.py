from __future__ import annotations

import json
import unittest

from forgeloop.client import DeepSeekClient, ModelError, chat_completions_url
from forgeloop.config import Settings


class ClientTests(unittest.TestCase):
    def test_endpoint_normalization(self) -> None:
        self.assertEqual(
            chat_completions_url("https://api.deepseek.com/"),
            "https://api.deepseek.com/chat/completions",
        )
        self.assertEqual(
            chat_completions_url("https://host/v1/chat/completions"),
            "https://host/v1/chat/completions",
        )

    def test_request_uses_current_model_and_disables_thinking(self) -> None:
        captured = {}

        def transport(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.headers["Authorization"]
            captured["timeout"] = timeout
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "done"},
                        }
                    ]
                }
            ).encode()

        settings = Settings(api_key="test-secret", max_retries=0)
        client = DeepSeekClient(settings, transport=transport)
        message = client.complete([{"role": "user", "content": "hi"}], [])
        self.assertEqual(message["content"], "done")
        self.assertEqual(captured["payload"]["model"], "deepseek-v4-pro")
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["authorization"], "Bearer test-secret")

    def test_tool_calls_and_reasoning_content_are_preserved(self) -> None:
        def transport(request, timeout):
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "list_files",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            ).encode()

        client = DeepSeekClient(Settings(api_key="x", max_retries=0), transport=transport)
        message = client.complete([], [])
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertIn("reasoning_content", message)

    def test_invalid_json_response_raises_safe_error(self) -> None:
        client = DeepSeekClient(
            Settings(api_key="x", max_retries=0),
            transport=lambda request, timeout: b"not json",
        )
        with self.assertRaises(ModelError):
            client.complete([], [])

    def test_content_filter_finish_reason_is_error(self) -> None:
        response = {
            "choices": [
                {
                    "finish_reason": "content_filter",
                    "message": {"role": "assistant", "content": None},
                }
            ]
        }
        client = DeepSeekClient(
            Settings(api_key="x", max_retries=0),
            transport=lambda request, timeout: json.dumps(response).encode(),
        )
        with self.assertRaises(ModelError):
            client.complete([], [])


if __name__ == "__main__":
    unittest.main()
