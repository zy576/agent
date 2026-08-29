"""Local tools exposed to the model through native function calling."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable


class ToolError(RuntimeError):
    """An expected tool failure that can be returned to the model."""


IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}

SECRET_ENV_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIAL",
    "_PRIVATE_KEY",
)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(limit * 2 // 3, 1)
    tail = max(limit - head - 80, 1)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"


@dataclass(slots=True)
class Workspace:
    """Workspace-scoped filesystem and command operations."""

    root: Path
    max_output_chars: int = 16_000
    command_timeout_seconds: float = 120.0
    allow_dangerous_commands: bool = False

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Workspace is not a directory: {self.root}")

    def _resolve(self, user_path: str) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise ToolError("path must be a non-empty string")
        candidate = (self.root / user_path).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("path escapes the workspace") from exc
        if ".git" in relative.parts:
            raise ToolError("direct access to .git internals is not allowed")
        lowered_name = candidate.name.lower()
        if (
            lowered_name == ".env"
            or lowered_name.startswith(".env.")
            or lowered_name in {"id_rsa", "id_ed25519"}
            or candidate.suffix.lower() in {".pem", ".p12", ".pfx"}
        ):
            raise ToolError("access to likely credential files is not allowed")
        return candidate

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"file not found: {path}")
        if start_line < 1:
            raise ToolError("start_line must be at least 1")
        if end_line is not None and end_line < start_line:
            raise ToolError("end_line must be greater than or equal to start_line")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not valid UTF-8 text: {path}") from exc
        selected = lines[start_line - 1 : end_line]
        numbered = [
            f"{number:>5} | {line}"
            for number, line in enumerate(selected, start=start_line)
        ]
        header = f"{path} ({len(lines)} lines)"
        return _clip("\n".join([header, *numbered]), self.max_output_chars)

    def list_files(self, path: str = ".", recursive: bool = True) -> str:
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"path not found: {path}")
        if target.is_file():
            return target.relative_to(self.root).as_posix()

        iterator = target.rglob("*") if recursive else target.iterdir()
        rows: list[str] = []
        for item in iterator:
            relative = item.relative_to(self.root)
            if any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts):
                continue
            suffix = "/" if item.is_dir() else ""
            rows.append(relative.as_posix() + suffix)
            if len(rows) >= 1_000:
                rows.append("... [file listing limited to 1000 entries]")
                break
        rows.sort()
        return _clip("\n".join(rows) or "[empty directory]", self.max_output_chars)

    def search_files(
        self,
        query: str,
        path: str = ".",
        glob: str = "*",
        use_regex: bool = False,
    ) -> str:
        if not query:
            raise ToolError("query must not be empty")
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"path not found: {path}")
        try:
            matcher = re.compile(query) if use_regex else None
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc

        files = [target] if target.is_file() else target.rglob("*")
        matches: list[str] = []
        for file_path in files:
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(self.root)
            if any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts):
                continue
            if not fnmatch.fnmatch(file_path.name, glob):
                continue
            try:
                with file_path.open("r", encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        found = bool(matcher.search(line)) if matcher else query in line
                        if found:
                            matches.append(
                                f"{relative.as_posix()}:{line_number}:{line.rstrip()}"
                            )
                            if len(matches) >= 200:
                                matches.append("... [search limited to 200 matches]")
                                return _clip("\n".join(matches), self.max_output_chars)
            except (UnicodeDecodeError, OSError):
                continue
        return _clip("\n".join(matches) or "[no matches]", self.max_output_chars)

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after directory creation to catch an existing symlink parent.
        target = self._resolve(path)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            ) as stream:
                stream.write(content)
                temporary_name = stream.name
            os.replace(temporary_name, target)
        except OSError as exc:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise ToolError(f"could not write {path}: {exc}") from exc
        return f"wrote {len(content)} characters to {path}"

    def replace_in_file(
        self,
        path: str,
        old: str,
        new: str,
        expected_count: int = 1,
    ) -> str:
        if not old:
            raise ToolError("old text must not be empty")
        if expected_count < 1:
            raise ToolError("expected_count must be at least 1")
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"file not found: {path}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not valid UTF-8 text: {path}") from exc
        actual_count = content.count(old)
        if actual_count != expected_count:
            raise ToolError(
                f"expected old text {expected_count} time(s), found {actual_count}; "
                "read the file again before retrying"
            )
        updated = content.replace(old, new)
        self.write_file(path, updated)
        return f"replaced {actual_count} occurrence(s) in {path}"

    def run_command(
        self,
        argv: list[str],
        cwd: str = ".",
        timeout_seconds: float | None = None,
    ) -> str:
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(part, str) or not part for part in argv)
        ):
            raise ToolError("argv must be a non-empty array of non-empty strings")
        command_text = subprocess.list2cmdline(argv)
        if not self.allow_dangerous_commands and _looks_dangerous(command_text):
            raise ToolError(
                "command blocked by the destructive-command policy; ask the user to "
                "rerun ForgeLoop with --allow-dangerous only if this is intentional"
            )
        working_directory = self._resolve(cwd)
        if not working_directory.is_dir():
            raise ToolError(f"command cwd is not a directory: {cwd}")
        timeout = timeout_seconds or self.command_timeout_seconds
        timeout = min(max(float(timeout), 0.1), 300.0)
        child_environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().endswith(SECRET_ENV_SUFFIXES)
        }
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                cwd=working_directory,
                env=child_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            partial = "\n".join(
                part for part in [exc.stdout or "", exc.stderr or ""] if part
            )
            raise ToolError(
                f"command timed out after {timeout:.1f}s\n"
                f"{_clip(partial, self.max_output_chars)}"
            ) from exc
        except OSError as exc:
            raise ToolError(f"could not start command: {exc}") from exc

        output = "\n".join(
            part.rstrip() for part in [completed.stdout, completed.stderr] if part
        )
        result = f"exit_code={completed.returncode}"
        if output:
            result += f"\n{output}"
        return _clip(result, self.max_output_chars)


def _looks_dangerous(command: str) -> bool:
    normalized = " ".join(command.strip().split())
    patterns = (
        r"(?i)(?:^|[;&|])\s*rm\s+-[^\s]*r[^\s]*f\s+(?:/|~)(?:\s|$)",
        r"(?i)(?:^|[;&|])\s*(?:shutdown|reboot|halt|poweroff)\b",
        r"(?i)(?:^|[;&|])\s*(?:format|diskpart)\b",
        r"(?i)\bgit\s+(?:reset\s+--hard|clean\s+-[^\s]*f)",
        r"(?i)\bRemove-Item\b[^\r\n]*(?:-[A-Za-z]*Recurse|-r)\b[^\r\n]*(?:[A-Za-z]:\\(?:\s|$)|\\$)",
        r"(?i)\b(?:del|rmdir)\b[^\r\n]*(?:/s|/q)[^\r\n]*[A-Za-z]:\\(?:\s|$)",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


ToolFunction = Callable[..., str]


class ToolRegistry:
    """Tool schemas plus strict local dispatch for model-generated calls."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._functions: dict[str, ToolFunction] = {
            "list_files": workspace.list_files,
            "read_file": workspace.read_file,
            "search_files": workspace.search_files,
            "write_file": workspace.write_file,
            "replace_in_file": workspace.replace_in_file,
            "run_command": workspace.run_command,
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def execute(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        try:
            function = tool_call["function"]
            name = function["name"]
            raw_arguments = function.get("arguments", "{}")
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments or "{}")
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise ToolError("tool arguments must be a JSON object")
            if not isinstance(arguments, dict):
                raise ToolError("tool arguments must decode to a JSON object")
            handler = self._functions.get(name)
            if handler is None:
                raise ToolError(f"unknown tool: {name}")
            output = handler(**arguments)
            return {"ok": True, "tool": name, "output": output}
        except (json.JSONDecodeError, KeyError, TypeError, ToolError, ValueError) as exc:
            return {
                "ok": False,
                "tool": _best_effort_tool_name(tool_call),
                "error": str(exc),
            }
        except Exception as exc:  # Defensive boundary: keep the agent loop alive.
            return {
                "ok": False,
                "tool": _best_effort_tool_name(tool_call),
                "error": f"unexpected {type(exc).__name__}: {exc}",
            }


def _best_effort_tool_name(tool_call: dict[str, Any]) -> str:
    try:
        return str(tool_call["function"]["name"])
    except (KeyError, TypeError):
        return "unknown"


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List workspace files and directories. Paths are workspace-relative.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory or file path."},
                    "recursive": {"type": "boolean", "description": "Recurse into directories."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search UTF-8 text files and return matching path:line:text rows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string", "description": "Filename glob such as *.py."},
                    "use_regex": {"type": "boolean"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Atomically create or fully overwrite a UTF-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace exact text only when its occurrence count matches the expectation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_count": {"type": "integer", "minimum": 1},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run one process without a shell in the workspace and return exit code, stdout, and stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Executable and arguments, for example [\"python\", \"-m\", \"unittest\"].",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Workspace-relative working directory.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0.1,
                        "maximum": 300,
                    },
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        },
    },
]
