"""Command-line interface for ForgeLoop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable

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
            "File tools stay inside the selected workspace; commands start there but "
            "are not an OS sandbox."
        ),
    )
    parser.add_argument("task", nargs="*", help="Programming task in plain language.")
    parser.add_argument("--task-file", type=Path, help="Read the task from a UTF-8 file.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--model", help="Override DEEPSEEK_MODEL.")
    parser.add_argument("--base-url", help="Override DEEPSEEK_BASE_URL.")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=(
            "Optional model-decision limit; by default ForgeLoop runs until "
            "completion or another safety limit."
        ),
    )
    parser.add_argument("--max-tool-calls", type=int, default=128)
    parser.add_argument("--max-tool-calls-per-step", type=int, default=16)
    parser.add_argument("--max-runtime-seconds", type=float, default=900.0)
    parser.add_argument("--max-context-chars", type=int, default=100_000)
    parser.add_argument("--max-tool-output-chars", type=int, default=16_000)
    parser.add_argument("--request-timeout", type=float, default=90.0)
    parser.add_argument("--command-timeout", type=float, default=120.0)
    parser.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Disable only the built-in destructive-command denylist (not a sandbox).",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Pass an additional non-secret environment variable to child processes.",
    )
    parser.add_argument("--quiet", action="store_true", help="Show only the final report.")
    session_mode = parser.add_mutually_exclusive_group()
    session_mode.add_argument(
        "--interactive",
        action="store_true",
        help="Keep the session open for follow-up tasks; use /help for commands.",
    )
    session_mode.add_argument(
        "--web",
        action="store_true",
        help="Open the local ForgeLoop Web workbench for interactive tasks.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port for --web (default: choose an available localhost port).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="With --web, print the URL without opening a browser.",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        help="Create a best-effort-redacted plain-text transcript (refuses overwrite).",
    )
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
            max_tool_calls=arguments.max_tool_calls,
            max_tool_calls_per_step=arguments.max_tool_calls_per_step,
            max_runtime_seconds=arguments.max_runtime_seconds,
            max_context_chars=arguments.max_context_chars,
            max_tool_output_chars=arguments.max_tool_output_chars,
            allow_dangerous_commands=arguments.allow_dangerous,
        )
        workspace = Workspace(
            arguments.workspace,
            max_output_chars=settings.max_tool_output_chars,
            command_timeout_seconds=settings.command_timeout_seconds,
            allow_dangerous_commands=settings.allow_dangerous_commands,
            pass_env_names=tuple(arguments.pass_env),
        )
        if arguments.web:
            from .web import WebApplication, serve_web

            audit_printer = EventPrinter(
                settings.api_key,
                quiet=True,
                transcript=arguments.transcript,
            )
            if arguments.transcript:
                audit_printer.header(workspace.root, settings.model, settings.max_steps)

            def agent_factory(on_event: Callable[[dict[str, Any]], None]) -> CodingAgent:
                def emit(event: dict[str, Any]) -> None:
                    audit_printer(event)
                    on_event(event)

                return CodingAgent(
                    DeepSeekClient(settings),
                    ToolRegistry(workspace),
                    max_steps=settings.max_steps,
                    max_tool_calls=settings.max_tool_calls,
                    max_tool_calls_per_step=settings.max_tool_calls_per_step,
                    max_runtime_seconds=settings.max_runtime_seconds,
                    max_context_chars=settings.max_context_chars,
                    on_event=emit,
                )

            application = WebApplication(
                agent_factory,
                api_key=settings.api_key,
                workspace=str(workspace.root),
                model=settings.model,
            )
            return serve_web(
                application,
                port=arguments.port,
                open_browser=not arguments.no_open,
            )
        printer = EventPrinter(
            settings.api_key,
            quiet=arguments.quiet,
            transcript=arguments.transcript,
        )
        agent = CodingAgent(
            DeepSeekClient(settings),
            ToolRegistry(workspace),
            max_steps=settings.max_steps,
            max_tool_calls=settings.max_tool_calls,
            max_tool_calls_per_step=settings.max_tool_calls_per_step,
            max_runtime_seconds=settings.max_runtime_seconds,
            max_context_chars=settings.max_context_chars,
            on_event=printer,
        )
        if not arguments.quiet:
            printer.header(workspace.root, settings.model, settings.max_steps)
        elif arguments.transcript:
            printer.header(workspace.root, settings.model, settings.max_steps)
        if arguments.interactive:
            return _run_interactive(
                agent,
                task,
                settings.api_key,
                quiet=arguments.quiet,
            )
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
    if arguments.web:
        if arguments.task_file or arguments.task:
            parser.error("--web opens an interactive workbench and does not take a task")
        if not 0 <= arguments.port <= 65_535:
            parser.error("--port must be between 0 and 65535")
        return ""
    if arguments.no_open:
        parser.error("--no-open requires --web")
    if arguments.port != 0:
        parser.error("--port requires --web")
    if arguments.task_file and arguments.task:
        parser.error("use either a positional task or --task-file, not both")
    if arguments.task_file:
        return arguments.task_file.read_text(encoding="utf-8").strip()
    if arguments.task:
        return " ".join(arguments.task).strip()
    if arguments.interactive:
        return ""
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    parser.error("provide a task as arguments, --task-file, or standard input")
    raise AssertionError("argparse.error always exits")


def _run_interactive(
    agent: CodingAgent,
    initial_task: str,
    api_key: str,
    *,
    quiet: bool = False,
    read_line: Callable[[str], str] | None = None,
    write_line: Callable[[str], None] | None = None,
    write_error: Callable[[str], None] | None = None,
) -> int:
    reader = read_line or input
    output = write_line or print
    error_output = write_error or (lambda text: print(text, file=sys.stderr))
    if not quiet:
        output(
            "Interactive mode: enter a coding task, then keep adding follow-ups.\n"
            "Commands: /help, /quit"
        )

    history: list[dict[str, Any]] | None = None
    verification_pending = False
    pending_task = initial_task.strip()
    while True:
        if not pending_task:
            try:
                entered = reader("ForgeLoop> ").strip()
            except EOFError:
                if not quiet:
                    output("Session closed.")
                return 0
            except KeyboardInterrupt:
                if not quiet:
                    output("\nSession cancelled.")
                return 130
            if not entered:
                continue
            command = entered.casefold()
            if command in {"/quit", "/exit"}:
                if not quiet:
                    output("Session closed.")
                return 0
            if command == "/help":
                output(
                    "Enter a task or follow-up in natural language. "
                    "/quit exits."
                )
                continue
            pending_task = entered

        try:
            result = agent.run(
                pending_task,
                history=history,
                verification_pending=verification_pending,
            )
        except KeyboardInterrupt:
            error_output("Current task cancelled; the interactive session is closing.")
            return 130
        except (ModelError, OSError, ValueError) as exc:
            error_output(f"error: {_redact(str(exc), api_key)}")
            error_output(
                "Session closed because the interrupted task may have changed workspace "
                "files; restart and ask ForgeLoop to inspect the current state."
            )
            return 2
        history = result.messages
        verification_pending = result.verification_pending
        if quiet:
            output(_redact(result.summary, api_key))
        pending_task = ""


class EventPrinter:
    def __init__(
        self,
        api_key: str,
        *,
        quiet: bool = False,
        transcript: Path | None = None,
    ) -> None:
        self.api_key = api_key
        self.quiet = quiet
        self.transcript = transcript
        if self.transcript:
            self.transcript.parent.mkdir(parents=True, exist_ok=True)
            with self.transcript.open("x", encoding="utf-8"):
                pass

    def header(self, workspace: Path, model: str, max_steps: int | None) -> None:
        step_limit = (
            "none (tool/runtime safety limits still apply)"
            if max_steps is None
            else str(max_steps)
        )
        self._write(
            f"ForgeLoop workspace: {workspace}\n"
            f"Model: {model} | Decision step cap: {step_limit}\n"
        )

    def __call__(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "model_request":
            self._write(
                f"[step {event['step']}] model decision "
                f"({event['message_count']} context messages)"
            )
        elif event_type == "finalization_request":
            self._write(
                "[finalizing] report-only model decision "
                f"({event['message_count']} context messages)"
            )
        elif event_type == "tool_start":
            arguments = _summarize_arguments(event.get("arguments", {}))
            self._write(f"  -> {event.get('tool')} {arguments}")
        elif event_type == "tool_end":
            result = event.get("result", {})
            marker = "ok" if result.get("ok") else "error"
            detail = result.get("output") if result.get("ok") else result.get("error")
            self._write(
                f"  <- {marker}: {_redact(_one_line(str(detail or '')), self.api_key)}"
            )
        elif event_type == "warning":
            self._write(f"  ! {_redact(str(event.get('message', '')), self.api_key)}")
        elif event_type == "final":
            summary = _redact(str(event.get("summary", "")), self.api_key)
            self._write(f"\n[{event.get('status')}]\n{summary}")

    def _write(self, text: str) -> None:
        safe_text = _redact(text, self.api_key)
        if self.transcript:
            with self.transcript.open("a", encoding="utf-8", newline="") as stream:
                stream.write(safe_text + "\n")
        if not self.quiet:
            print(safe_text, flush=True)


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
    patterns = (
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]"),
        (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED]"),
        (r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "[REDACTED]"),
        (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "[REDACTED]"),
        (
            r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|database_url)"
            r"\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
        ),
    )
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
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
