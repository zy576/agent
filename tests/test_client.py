from __future__ import annotations

from email.message import Message
import io
import json
import unittest
from urllib.error import HTTPError, URLError

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
        self.assertNotIn("tools", captured["payload"])
        self.assertNotIn("tool_choice", captured["payload"])

    def test_request_with_tools_enables_automatic_tool_choice(self) -> None:
        captured = {}

        def transport(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
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

        tools = [{"type": "function", "function": {"name": "list_files"}}]
        client = DeepSeekClient(
            Settings(api_key="test-secret", max_retries=0),
            transport=transport,
        )

        client.complete([{"role": "user", "content": "hi"}], tools)

        self.assertEqual(captured["payload"]["tools"], tools)
        self.assertEqual(captured["payload"]["tool_choice"], "auto")

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

    def test_length_finish_reason_never_executes_partial_tool_calls(self) -> None:
        response = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "partial",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"x.txt","content":"partial',
                                },
                            }
                        ],
                    },
                }
            ]
        }
        client = DeepSeekClient(
            Settings(api_key="x", max_retries=0),
            transport=lambda request, timeout: json.dumps(response).encode(),
        )
        with self.assertRaisesRegex(ModelError, "output limit"):
            client.complete([], [])

    def test_invalid_or_duplicate_tool_calls_are_rejected(self) -> None:
        invalid_variants = [
            [None],
            [
                {
                    "id": "same",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                },
                {
                    "id": "same",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
            [
                {
                    "id": "bad",
                    "type": "function",
                    "function": {"name": "bad name", "arguments": "{}"},
                }
            ],
        ]
        for tool_calls in invalid_variants:
            response = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        },
                    }
                ]
            }
            client = DeepSeekClient(
                Settings(api_key="x", max_retries=0),
                transport=lambda request, timeout, response=response: json.dumps(
                    response
                ).encode(),
            )
            with self.subTest(tool_calls=tool_calls), self.assertRaises(ModelError):
                client.complete([], [])

    def test_429_retries_and_honors_retry_after(self) -> None:
        attempts = 0
        sleeps = []

        def transport(request, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                headers = Message()
                headers["Retry-After"] = "0"
                raise HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    headers,
                    io.BytesIO(b'{"error":{"message":"slow down"}}'),
                )
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "ok"},
                        }
                    ]
                }
            ).encode()

        client = DeepSeekClient(
            Settings(api_key="x", max_retries=2),
            transport=transport,
            sleeper=sleeps.append,
        )
        self.assertEqual(client.complete([], [])["content"], "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [0.0])

    def test_401_does_not_retry(self) -> None:
        attempts = 0

        def transport(request, timeout):
            nonlocal attempts
            attempts += 1
            raise HTTPError(
                request.full_url,
                401,
                "unauthorized",
                Message(),
                io.BytesIO(b'{"error":{"message":"invalid credentials"}}'),
            )

        client = DeepSeekClient(
            Settings(api_key="x", max_retries=3),
            transport=transport,
            sleeper=lambda delay: None,
        )
        with self.assertRaisesRegex(ModelError, "HTTP 401"):
            client.complete([], [])
        self.assertEqual(attempts, 1)

    def test_connection_error_retries_then_fails_safely(self) -> None:
        attempts = 0

        def transport(request, timeout):
            nonlocal attempts
            attempts += 1
            raise URLError("secret-looking low-level detail")

        client = DeepSeekClient(
            Settings(api_key="real-secret", max_retries=2),
            transport=transport,
            sleeper=lambda delay: None,
        )
        with self.assertRaises(ModelError) as caught:
            client.complete([], [])
        self.assertEqual(attempts, 3)
        self.assertNotIn("secret-looking", str(caught.exception))
        self.assertNotIn("real-secret", str(caught.exception))

    def test_http_error_detail_redacts_key_even_for_library_callers(self) -> None:
        def transport(request, timeout):
            raise HTTPError(
                request.full_url,
                400,
                "bad request",
                Message(),
                io.BytesIO(
                    b'{"error":{"message":"echoed credential super-secret"}}'
                ),
            )

        client = DeepSeekClient(
            Settings(api_key="super-secret", max_retries=0),
            transport=transport,
        )
        with self.assertRaises(ModelError) as caught:
            client.complete([], [])
        self.assertNotIn("super-secret", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
