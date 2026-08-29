"""Runtime configuration loaded from explicit arguments and environment."""

from __future__ import annotations

from dataclasses import dataclass, field
import os


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    request_timeout_seconds: float = 90.0
    command_timeout_seconds: float = 120.0
    max_steps: int = 24
    max_context_chars: int = 100_000
    max_tool_output_chars: int = 16_000
    max_retries: int = 3
    temperature: float = 0.1
    allow_dangerous_commands: bool = False

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ConfigurationError(
                "DEEPSEEK_API_KEY is not configured. Set it in the environment."
            )
        if self.max_steps < 1:
            raise ConfigurationError("max_steps must be at least 1")
        if self.max_context_chars < 2_000:
            raise ConfigurationError("max_context_chars must be at least 2000")
        if self.max_tool_output_chars < 500:
            raise ConfigurationError("max_tool_output_chars must be at least 500")
        if self.request_timeout_seconds <= 0 or self.command_timeout_seconds <= 0:
            raise ConfigurationError("timeouts must be positive")
        if self.max_retries < 0:
            raise ConfigurationError("max_retries cannot be negative")

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
