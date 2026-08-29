"""Command-line interface for ForgeLoop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from . import __version__
from .agent import CodingAgent
from .client import DeepSeekClient, ModelError
from .config import ConfigurationError, Settings
from .tools import ToolRegistry, Workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forgeloop",
        description=(
            "Run a small coding agent backed by DeepSeek native tool calling. "
            "The model can inspect and modify only the selected workspace."
        ),
    )
    parser.add_argument("task", nargs="*", help="Programming task in plain language.")
    parser.add_argument("--task-file", type=Path, help="Read the task from a UTF-8 file.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--model", help="Override DEEPSEEK_MODEL.")
    parser.add_argument("--base-url", help="Override DEEPSEEK_BASE_URL.")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-context-chars", type=int, default=100_000)
    parser.add_argument("--max-tool-output-chars", type=int, default=16_000)
    parser.add_argument("--request-timeout", type=float, default=90.0)
    parser.add_argument("--command-timeout", type=float, default=120.0)
    parser.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Disable only the built-in destructive-command denylist (not a sandbox).",
    )
    parser.add_argument("--quiet", action="store_true", help="Show only the final report.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        task = _load_task(arguments, parser)
        settings = Settings.from_env(
            base_url=arguments.base_url,
            model=arguments.model,
            request_timeout_seconds=arguments.request_timeout,
            command_timeout_seconds=arguments.command_timeout,
            max_steps=arguments.max_steps,
            max_context_chars=arguments.max_context_chars,
            max_tool_output_chars=arguments.max_tool_output_chars,
            allow_dangerous_commands=arguments.allow_dangerous,
        )
        workspace = Workspace(
            arguments.workspace,
            max_output_chars=settings.max_tool_output_chars,
            command_timeout_seconds=settings.command_timeout_seconds,
            allow_dangerous_commands=settings.allow_dangerous_commands,
        )
        printer = EventPrinter(settings.api_key, quiet=arguments.quiet)
        agent = CodingAgent(
            DeepSeekClient(settings),
            ToolRegistry(workspace),
            max_steps=settings.max_steps,
            max_context_chars=settings.max_context_chars,
            on_event=printer,
        )
        if not arguments.quiet:
            print(f"ForgeLoop workspace: {workspace.root}", flush=True)
            print(f"Model: {settings.model} | Max steps: {settings.max_steps}\n", flush=True)
        result = agent.run(task)
        if arguments.quiet:
            print(_redact(result.summary, settings.api_key))
        return 0 if result.status == "completed" else 1
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        return 130
    except (ConfigurationError, ModelError, OSError, ValueError) as exc:
        print(f"error: {_redact(str(exc), _current_key())}", file=sys.stderr)
        return 2


def _load_task(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if arguments.task_file and arguments.task:
        parser.error("use either a positional task or --task-file, not both")
    if arguments.task_file:
        return arguments.task_file.read_text(encoding="utf-8").strip()
    if arguments.task:
        return " ".join(arguments.task).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    parser.error("provide a task as arguments, --task-file, or standard input")
    raise AssertionError("argparse.error always exits")


class EventPrinter:
    def __init__(self, api_key: str, *, quiet: bool = False) -> None:
        self.api_key = api_key
        self.quiet = quiet

    def __call__(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if self.quiet and event_type != "final":
            return
        if event_type == "model_request":
            print(
                f"[step {event['step']}] model decision "
                f"({event['message_count']} context messages)",
                flush=True,
            )
        elif event_type == "tool_start":
            arguments = _summarize_arguments(event.get("arguments", {}))
            print(f"  -> {event.get('tool')} {arguments}", flush=True)
        elif event_type == "tool_end":
            result = event.get("result", {})
            marker = "ok" if result.get("ok") else "error"
            detail = result.get("output") if result.get("ok") else result.get("error")
            print(
                f"  <- {marker}: {_redact(_one_line(str(detail or '')), self.api_key)}",
                flush=True,
            )
        elif event_type == "warning":
            print(f"  ! {_redact(str(event.get('message', '')), self.api_key)}", flush=True)
        elif event_type == "final":
            summary = _redact(str(event.get("summary", "")), self.api_key)
            print(f"\n[{event.get('status')}]\n{summary}", flush=True)


def _summarize_arguments(arguments: dict[str, Any]) -> str:
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = key.lower()
        if any(word in lowered for word in ("key", "token", "secret", "password")):
            safe[key] = "[REDACTED]"
        elif key in {"content", "old", "new"} and isinstance(value, str):
            safe[key] = f"<{len(value)} chars>"
        else:
            safe[key] = value
    rendered = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    return _one_line(rendered, 500)


def _one_line(text: str, limit: int = 260) -> str:
    flattened = " ".join(text.splitlines())
    return flattened if len(flattened) <= limit else flattened[: limit - 3] + "..."


def _redact(text: str, api_key: str) -> str:
    redacted = text.replace(api_key, "[REDACTED]") if api_key else text
    return re.sub(
        r"(?i)\b(?:sk|key|token)-[A-Za-z0-9._-]{12,}\b",
        "[REDACTED]",
        redacted,
    )


def _current_key() -> str:
    # Kept local to the error path so configuration never appears in normal output.
    import os

    return os.environ.get("DEEPSEEK_API_KEY", "")


if __name__ == "__main__":
    raise SystemExit(main())
