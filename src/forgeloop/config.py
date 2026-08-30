"""Runtime configuration loaded from explicit arguments and environment."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    request_timeout_seconds: float = 90.0
    command_timeout_seconds: float = 120.0
    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_tool_calls_per_step: int = 16
    max_runtime_seconds: float | None = None
    max_context_chars: int = 100_000
    max_tool_output_chars: int = 16_000
    max_subagents: int = 0
    max_retries: int = 3
    temperature: float = 0.1
    allow_dangerous_commands: bool = False

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        base_url = self.base_url.strip().rstrip("/")
        model = self.model.strip()
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)
        if not api_key:
            raise ConfigurationError(
                "DEEPSEEK_API_KEY is not configured. Set it in the environment."
            )
        if not model:
            raise ConfigurationError("model must not be empty")
        parsed_url = urlparse(base_url)
        if not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise ConfigurationError("base_url must be an absolute URL without credentials")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed_url.scheme != "https" and not (
            parsed_url.scheme == "http" and parsed_url.hostname in local_hosts
        ):
            raise ConfigurationError(
                "base_url must use HTTPS (HTTP is allowed only for localhost)"
            )
        if parsed_url.query or parsed_url.fragment:
            raise ConfigurationError("base_url must not include a query or fragment")
        if self.max_steps is not None and self.max_steps < 1:
            raise ConfigurationError("max_steps must be at least 1 when configured")
        if self.max_tool_calls is not None and self.max_tool_calls < 1:
            raise ConfigurationError("max_tool_calls must be at least 1 when configured")
        if self.max_tool_calls_per_step < 1:
            raise ConfigurationError("max_tool_calls_per_step must be at least 1")
        if self.max_runtime_seconds is not None and self.max_runtime_seconds <= 0:
            raise ConfigurationError("max_runtime_seconds must be positive when configured")
        if self.max_context_chars < 2_000:
            raise ConfigurationError("max_context_chars must be at least 2000")
        if self.max_tool_output_chars < 500:
            raise ConfigurationError("max_tool_output_chars must be at least 500")
        if (
            isinstance(self.max_subagents, bool)
            or not isinstance(self.max_subagents, int)
            or not 0 <= self.max_subagents <= 4
        ):
            raise ConfigurationError("max_subagents must be an integer between 0 and 4")
        numeric_values = (
            self.request_timeout_seconds,
            self.command_timeout_seconds,
            self.temperature,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise ConfigurationError("timeouts and temperature must be finite")
        if self.max_runtime_seconds is not None and not math.isfinite(
            self.max_runtime_seconds
        ):
            raise ConfigurationError("max_runtime_seconds must be finite when configured")
        if self.request_timeout_seconds <= 0 or self.command_timeout_seconds <= 0:
            raise ConfigurationError("timeouts must be positive")
        if self.max_retries < 0:
            raise ConfigurationError("max_retries cannot be negative")
        if not 0 <= self.temperature <= 2:
            raise ConfigurationError("temperature must be between 0 and 2")

    @classmethod
    def from_env(cls, **overrides: object) -> "Settings":
        """Create settings without ever printing or persisting the API key."""

        values: dict[str, object] = {
            "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
            "base_url": os.environ.get(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ),
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)
