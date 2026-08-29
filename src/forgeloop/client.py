"""Minimal DeepSeek HTTP client; intentionally does not use an agent SDK."""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings


class ModelError(RuntimeError):
    """A safe, credential-free error returned by the model endpoint."""


Transport = Callable[[Request, float], bytes]


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured API
        return response.read()


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


class DeepSeekClient:
    """Synchronous OpenAI-compatible Chat Completions client."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._transport = transport or _default_transport
        self._sleep = sleeper

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "model": self.settings.model,
            "thinking": {"type": "disabled"},
            "messages": messages,
            "temperature": self.settings.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            chat_completions_url(self.settings.base_url),
            data=body,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "forgeloop-agent/0.1.0",
            },
            method="POST",
        )

        raw = self._request_with_retries(request)
        try:
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("Model returned an invalid Chat Completions response") from exc

        if not isinstance(message, dict):
            raise ModelError("Model response message is not an object")
        if finish_reason in {"content_filter", "insufficient_system_resource"}:
            raise ModelError(f"Model stopped without completing: {finish_reason}")
        if finish_reason == "length":
            raise ModelError("Model response exceeded its output limit before completing")
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content"),
        }
        if isinstance(message.get("reasoning_content"), str):
            normalized["reasoning_content"] = message["reasoning_content"]
        tool_calls = message.get("tool_calls")
        if tool_calls:
            if not isinstance(tool_calls, list):
                raise ModelError("Model tool_calls field is not a list")
            _validate_tool_calls(tool_calls)
            normalized["tool_calls"] = tool_calls
        return normalized

    def _request_with_retries(self, request: Request) -> bytes:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                return self._transport(request, self.settings.request_timeout_seconds)
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt == attempts - 1:
                    detail = _safe_http_detail(exc, self.settings.api_key)
                    raise ModelError(f"Model API HTTP {exc.code}: {detail}") from exc
                delay = _retry_delay(attempt, exc.headers.get("Retry-After"))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == attempts - 1:
                    raise ModelError(f"Model API connection failed: {type(exc).__name__}") from exc
                delay = _retry_delay(attempt, None)
            self._sleep(delay)
        raise AssertionError("retry loop exhausted unexpectedly")


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
    return min(0.8 * (2**attempt) + random.uniform(0.0, 0.2), 8.0)


def _safe_http_detail(error: HTTPError, api_key: str) -> str:
    try:
        raw = error.read(2_000).decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        detail = parsed.get("error", {}).get("message")
        if isinstance(detail, str) and detail.strip():
            safe_detail = detail.replace(api_key, "[REDACTED]") if api_key else detail
            return safe_detail[:500]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return "request rejected"


def _validate_tool_calls(tool_calls: list[Any]) -> None:
    seen_ids: set[str] = set()
    for call in tool_calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ModelError("Model returned an invalid function tool call")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            raise ModelError("Model returned a missing or duplicate tool call id")
        seen_ids.add(call_id)
        if not isinstance(function, dict):
            raise ModelError("Model tool call function is not an object")
        name = function.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ModelError("Model returned an invalid tool function name")
        if not isinstance(function.get("arguments"), str):
            raise ModelError("Model tool call arguments are not a JSON string")
