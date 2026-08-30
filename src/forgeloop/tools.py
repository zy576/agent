"""Local tools exposed to the model through native function calling."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
import locale
import os
from pathlib import Path
import re
import shutil
import signal
import stat as stat_module
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
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}

IGNORED_FILE_NAMES = {".coverage"}

SENSITIVE_PATH_PARTS = {
    ".aws",
    ".azure",
    ".docker",
    ".git",
    ".gnupg",
    ".kube",
    ".ssh",
}

SENSITIVE_FILE_NAMES = {
    ".env",
    ".envrc",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "_netrc",
    "auth.json",
    "credentials",
    "id_ed25519",
    "id_rsa",
}

SAFE_CHILD_ENV_NAMES = {
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
    "APPDATA",
    "CARGO_HOME",
    "CC",
    "COMSPEC",
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "CURL_CA_BUNDLE",
    "CXX",
    "DOTNET_ROOT",
    "GOPATH",
    "GOROOT",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "LANG",
    "LOCALAPPDATA",
    "JAVA_HOME",
    "NVM_HOME",
    "NVM_SYMLINK",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PNPM_HOME",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "PYTHONUTF8",
    "REQUESTS_CA_BUNDLE",
    "RUSTUP_HOME",
    "SDKROOT",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "USERPROFILE",
    "VCPKG_ROOT",
    "WINDIR",
}

SAFE_CHILD_ENV_PREFIXES = (
    "COMMONPROGRAMFILES",
    "LC_",
    "PROGRAMFILES",
)

SENSITIVE_ENV_MARKERS = (
    "ACCESS_KEY",
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "DATABASE_URL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)

MAX_SNAPSHOT_FILES = 20_000

WORKSPACE_DIR_MARKERS = {".git", ".hg", ".svn"}

WORKSPACE_PROJECT_MARKERS = {
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "CMakeLists.txt",
}


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(limit * 2 // 3, 1)
    tail = max(limit - head - 80, 1)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"


@dataclass(slots=True)
class Workspace:
    """Workspace-scoped filesystem and command operations.

    The active root can be re-bound with :meth:`select_workspace`, but only to
    directories inside ``scope_root`` (the launch-time directory tree).
    """

    root: Path
    max_output_chars: int = 16_000
    command_timeout_seconds: float = 120.0
    allow_dangerous_commands: bool = False
    pass_env_names: tuple[str, ...] = ()
    scope_root: Path | None = None

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Workspace is not a directory: {self.root}")
        self.scope_root = (self.scope_root or self.root).expanduser().resolve()
        if not self.scope_root.is_dir():
            raise ValueError(f"Workspace scope is not a directory: {self.scope_root}")
        try:
            self.root.relative_to(self.scope_root)
        except ValueError as exc:
            raise ValueError(
                "workspace must be inside the workspace scope "
                f"({self.scope_root}): {self.root}"
            ) from exc
        for name in self.pass_env_names:
            upper = name.upper()
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", upper):
                raise ValueError(f"invalid environment variable name: {name}")
            if any(marker in upper for marker in SENSITIVE_ENV_MARKERS):
                raise ValueError(f"refusing to pass likely secret environment variable: {name}")

    def select_workspace(self, path: str) -> str:
        """Re-bind the active root to another directory inside the scope."""
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path must be a non-empty string")
        raw = Path(path.strip()).expanduser()
        candidate = raw if raw.is_absolute() else self.scope_root / raw
        candidate = candidate.resolve()
        try:
            relative = candidate.relative_to(self.scope_root)
        except ValueError as exc:
            raise ToolError("path escapes the workspace scope") from exc
        folded_parts = {part.casefold() for part in relative.parts}
        blocked = folded_parts & SENSITIVE_PATH_PARTS
        if blocked:
            raise ToolError(
                f"cannot switch to a sensitive directory ({sorted(blocked)[0]})"
            )
        if not candidate.is_dir():
            raise ToolError(f"not a directory inside the workspace scope: {path}")
        self.root = candidate
        return f"workspace switched to {self.root}"

    def rebind_any(self, path: str) -> str:
        """User-directed re-bind to any directory on the machine.

        This is the human's explicit choice (like launching with --workspace),
        so it is not limited to the previous scope. The scope follows the new
        root, keeping the model's own select_workspace bounded afterwards.
        """
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path must be a non-empty string")
        candidate = Path(path.strip()).expanduser().resolve()
        if not candidate.is_dir():
            raise ToolError(f"not a directory: {path}")
        folded_parts = {part.casefold() for part in candidate.parts}
        blocked = folded_parts & SENSITIVE_PATH_PARTS
        if blocked:
            raise ToolError(
                f"cannot use a sensitive directory as workspace ({sorted(blocked)[0]})"
            )
        self.root = candidate
        self.scope_root = candidate
        return str(candidate)

    def list_directories(self, path: str = "") -> dict[str, Any]:
        """Directory-browser view: drives, or subfolders of an absolute path."""
        if not isinstance(path, str):
            raise ToolError("path must be a string")
        normalized = path.strip()
        if not normalized:
            roots: list[dict[str, str]] = []
            if os.name == "nt":
                for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                    root = f"{letter}:\\"
                    if os.path.isdir(root):
                        roots.append({"name": root, "path": root})
            else:
                root = str(Path(os.sep).resolve())
                roots.append({"name": root, "path": root})
            return {"path": "", "parent": None, "entries": roots, "truncated": False}
        target = Path(normalized).expanduser()
        if not target.is_absolute():
            raise ToolError("path must be absolute")
        target = target.resolve()
        blocked = {part.casefold() for part in target.parts} & SENSITIVE_PATH_PARTS
        if blocked:
            raise ToolError(
                f"cannot browse a sensitive directory ({sorted(blocked)[0]})"
            )
        if not target.is_dir():
            raise ToolError(f"not a directory: {normalized}")
        parent = target.parent
        entries: list[dict[str, str]] = []
        truncated = False
        try:
            with os.scandir(target) as iterator:
                for entry in iterator:
                    if len(entries) >= 500:
                        truncated = True
                        break
                    if entry.name.casefold() in SENSITIVE_PATH_PARTS:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            entries.append(
                                {
                                    "name": entry.name,
                                    "path": str(Path(entry.path)),
                                }
                            )
                    except OSError:
                        continue
        except OSError as exc:
            raise ToolError(f"cannot read directory: {exc}") from exc
        entries.sort(key=lambda item: item["name"].casefold())
        return {
            "path": str(target),
            "parent": str(parent) if parent != target else None,
            "entries": entries,
            "truncated": truncated,
        }

    def candidate_workspaces(
        self,
        max_depth: int = 3,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Project-like directories inside the scope, for pickers and the model."""
        project_dirs: list[Path] = []
        top_level: list[Path] = []
        for directory, directory_names, file_names in os.walk(
            self.scope_root, followlinks=False
        ):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in IGNORED_DIRECTORY_NAMES
                and name.casefold() not in SENSITIVE_PATH_PARTS
            )
            current = Path(directory)
            try:
                relative = current.relative_to(self.scope_root)
            except ValueError:
                continue
            if len(relative.parts) >= max_depth:
                directory_names[:] = []
            if current == self.scope_root:
                continue
            marked = any(
                (current / name).is_dir() for name in WORKSPACE_DIR_MARKERS
            ) or any(
                (current / name).is_file() for name in WORKSPACE_PROJECT_MARKERS
            )
            if marked:
                project_dirs.append(relative)
            elif len(relative.parts) == 1:
                top_level.append(relative)
        ordered = sorted(
            project_dirs,
            key=lambda item: (len(item.parts), item.as_posix().casefold()),
        )
        chosen = ordered[:limit] if ordered else sorted(
            top_level, key=lambda item: item.as_posix().casefold()
        )[:limit]
        entries: list[dict[str, Any]] = [
            {
                "path": ".",
                "name": self.scope_root.name or str(self.scope_root),
                "current": self.root == self.scope_root,
            }
        ]
        for relative in chosen:
            absolute = self.scope_root / relative
            entries.append(
                {
                    "path": relative.as_posix(),
                    "name": relative.name or relative.as_posix(),
                    "current": self.root == absolute,
                }
            )
        return entries

    def list_workspaces(self) -> str:
        rows = [f"scope: {self.scope_root}"]
        for entry in self.candidate_workspaces():
            marker = "  [current workspace]" if entry["current"] else ""
            rows.append(f"{entry['path']}{marker}")
        return _clip(
            "\n".join(rows) or "[no candidate workspaces]", self.max_output_chars
        )

    def _resolve(self, user_path: str) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise ToolError("path must be a non-empty string")
        candidate = (self.root / user_path).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("path escapes the workspace") from exc
        folded_parts = {part.casefold() for part in relative.parts}
        blocked_parts = folded_parts & SENSITIVE_PATH_PARTS
        if blocked_parts:
            blocked = sorted(blocked_parts)[0]
            raise ToolError(f"access to sensitive directory {blocked} is not allowed")
        lowered_name = candidate.name.lower()
        if (
            lowered_name in SENSITIVE_FILE_NAMES
            or lowered_name.startswith(".env.")
            or candidate.suffix.lower()
            in {".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
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

        rows: list[str] = []
        for directory, directory_names, file_names in os.walk(target, followlinks=False):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in IGNORED_DIRECTORY_NAMES
                and name.casefold() not in SENSITIVE_PATH_PARTS
            )
            for name in [*directory_names, *sorted(file_names)]:
                item = Path(directory) / name
                if item.is_file() and name in IGNORED_FILE_NAMES:
                    continue
                try:
                    relative = item.relative_to(self.root)
                    safe_item = self._resolve(relative.as_posix())
                except (ToolError, ValueError):
                    continue
                suffix = "/" if safe_item.is_dir() else ""
                rows.append(relative.as_posix() + suffix)
                if len(rows) >= 1_000:
                    rows.append("... [file listing limited to 1000 entries]")
                    break
            if len(rows) >= 1_000 or not recursive:
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

        matches: list[str] = []
        for file_path in self._iter_safe_files(target):
            relative = file_path.relative_to(self.root)
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
        original_mode = (
            stat_module.S_IMODE(target.stat().st_mode) if target.exists() else None
        )
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
            if original_mode is not None:
                os.chmod(temporary_name, original_mode)
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
            with target.open("r", encoding="utf-8", newline="") as stream:
                content = stream.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not valid UTF-8 text: {path}") from exc
        search_text = old
        replacement_text = new
        actual_count = content.count(search_text)
        if (
            actual_count == 0
            and "\n" in old
            and "\r" not in old
            and content.count("\r\n") > content.count("\n") - content.count("\r\n")
        ):
            search_text = old.replace("\n", "\r\n")
            replacement_text = (
                new.replace("\n", "\r\n") if "\r" not in new else new
            )
            actual_count = content.count(search_text)
        if actual_count != expected_count:
            raise ToolError(
                f"expected old text {expected_count} time(s), found {actual_count}; "
                "read the file again before retrying"
            )
        updated = content.replace(search_text, replacement_text)
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
        if not self.allow_dangerous_commands and _looks_dangerous(argv):
            raise ToolError(
                "command blocked by the destructive-command policy; ask the user to "
                "rerun ForgeLoop with --allow-dangerous only if this is intentional"
            )
        working_directory = self._resolve(cwd)
        if not working_directory.is_dir():
            raise ToolError(f"command cwd is not a directory: {cwd}")
        timeout = timeout_seconds or self.command_timeout_seconds
        timeout = min(max(float(timeout), 0.1), 300.0)
        child_environment = _safe_child_environment(self.pass_env_names)
        executable = shutil.which(argv[0], path=child_environment.get("PATH"))
        invocation = list(argv)
        if executable:
            invocation[0] = executable
        if os.name == "nt" and executable and Path(executable).suffix.lower() in {
            ".cmd",
            ".bat",
        }:
            unsafe_meta = next(
                (part for part in invocation if re.search(r"[&|<>^%!\r\n]", part)),
                None,
            )
            if unsafe_meta is not None:
                raise ToolError(
                    "Windows batch-wrapper arguments may not contain shell metacharacters"
                )
        try:
            with tempfile.TemporaryFile() as stdout_buffer, tempfile.TemporaryFile() as stderr_buffer:
                popen_options: dict[str, Any] = {}
                if os.name == "nt":
                    popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_options["start_new_session"] = True
                process = subprocess.Popen(
                    invocation,
                    shell=False,
                    cwd=working_directory,
                    env=child_environment,
                    stdout=stdout_buffer,
                    stderr=stderr_buffer,
                    **popen_options,
                )
                timed_out = False
                try:
                    return_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_process_tree(process, child_environment)
                    return_code = process.wait(timeout=10)
                except KeyboardInterrupt as cancellation:
                    try:
                        try:
                            _terminate_process_tree(process, child_environment)
                        except Exception:
                            try:
                                process.kill()
                            except Exception:
                                pass
                        try:
                            process.wait(timeout=10)
                        except Exception:
                            try:
                                process.kill()
                            except Exception:
                                pass
                    finally:
                        raise cancellation
                stdout = _decode_output(
                    _read_bounded(stdout_buffer, self.max_output_chars * 4)
                )
                stderr = _decode_output(
                    _read_bounded(stderr_buffer, self.max_output_chars * 4)
                )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("command process tree did not terminate after timeout") from exc
        except OSError as exc:
            raise ToolError(f"could not start command: {exc}") from exc

        output = "\n".join(part.rstrip() for part in [stdout, stderr] if part)
        if timed_out:
            raise ToolError(
                f"command timed out after {timeout:.1f}s\n"
                f"{_clip(output, self.max_output_chars)}"
            )
        result = f"exit_code={return_code}"
        if output:
            result += f"\n{output}"
        return _clip(result, self.max_output_chars)

    def snapshot_files(self) -> dict[str, tuple[int, int]]:
        """Return a bounded, best-effort signature for command-side change auditing."""

        snapshot: dict[str, tuple[int, int]] = {}
        for item in self._iter_safe_files(self.root, limit=MAX_SNAPSHOT_FILES):
            try:
                relative = item.relative_to(self.root)
                stat = item.stat()
            except (OSError, ValueError):
                continue
            snapshot[relative.as_posix()] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _iter_safe_files(self, target: Path, *, limit: int | None = None):
        if target.is_file():
            try:
                yield self._resolve(target.relative_to(self.root).as_posix())
            except (ToolError, ValueError):
                return
            return
        yielded = 0
        for directory, directory_names, file_names in os.walk(target, followlinks=False):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in IGNORED_DIRECTORY_NAMES
                and name.casefold() not in SENSITIVE_PATH_PARTS
            )
            for file_name in sorted(file_names):
                if file_name in IGNORED_FILE_NAMES:
                    continue
                file_path = Path(directory) / file_name
                try:
                    relative = file_path.relative_to(self.root)
                    safe_file = self._resolve(relative.as_posix())
                except (ToolError, ValueError):
                    continue
                yield safe_file
                yielded += 1
                if limit is not None and yielded >= limit:
                    return


def _looks_dangerous(argv: list[str]) -> bool:
    executable = re.split(r"[\\/]", argv[0])[-1].lower()
    executable = re.sub(r"\.(?:exe|cmd|bat|com)$", "", executable)
    args = [argument.lower() for argument in argv[1:]]
    joined = " ".join(args)

    if executable in {"shutdown", "reboot", "halt", "poweroff", "format", "diskpart"}:
        return True
    if executable == "git":
        if len(args) >= 2 and args[0] == "reset" and "--hard" in args[1:]:
            return True
        if args and args[0] == "clean" and any(
            token.startswith("-") and "f" in token.lstrip("-") for token in args[1:]
        ):
            return True
        if args and args[0] == "restore":
            return True
        if len(args) >= 2 and args[0] == "checkout" and "--" in args[1:]:
            return True
    if executable == "rm" and any("r" in token and token.startswith("-") for token in args):
        return True
    if executable in {"rd", "rmdir", "del", "erase"} and any(
        token in {"/s", "-r", "--recursive"} for token in args
    ):
        return True
    if executable in {"cmd", "powershell", "pwsh"}:
        nested_patterns = (
            r"(?i)\b(?:rd|rmdir|del|erase)\b[^\r\n]*(?:/s|-r|--recursive)",
            r"(?i)\bremove-item\b[^\r\n]*(?:-recurse|-r)\b",
            r"(?i)(?:^|\s)git(?:\.exe)?\s+(?:reset\s+--hard|clean\s+-[^\s]*f|restore\b)",
        )
        return any(re.search(pattern, joined) for pattern in nested_patterns)
    return False


def _safe_child_environment(extra_names: tuple[str, ...] = ()) -> dict[str, str]:
    environment: dict[str, str] = {}
    allowed_names = SAFE_CHILD_ENV_NAMES | {name.upper() for name in extra_names}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in allowed_names or any(
            upper.startswith(prefix) for prefix in SAFE_CHILD_ENV_PREFIXES
        ):
            environment[name] = value
    return environment


def _read_bounded(stream: Any, max_bytes: int) -> bytes:
    marker = b"\n... [process output truncated] ...\n"
    stream.flush()
    size = stream.seek(0, os.SEEK_END)
    stream.seek(0)
    if size <= max_bytes:
        return stream.read()
    available = max(max_bytes - len(marker), 2)
    head_size = available * 2 // 3
    tail_size = available - head_size
    head = stream.read(head_size)
    stream.seek(-tail_size, os.SEEK_END)
    return head + marker + stream.read(tail_size)


def _decode_output(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.count(b"\x00") > len(raw) // 5:
        return raw.decode("utf-16-le", errors="replace")
    encodings = ["utf-8", locale.getpreferredencoding(False)]
    if os.name == "nt":
        encodings.append("mbcs")
    for encoding in dict.fromkeys(encodings):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _terminate_process_tree(
    process: subprocess.Popen[Any], environment: dict[str, str]
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()


ToolFunction = Callable[..., str]


READ_ONLY_TOOL_NAMES = frozenset({"list_files", "read_file", "search_files"})
DELEGATION_TOOL_NAME = "delegate_readonly"


class ToolRegistry:
    """Tool schemas plus strict local dispatch for model-generated calls."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        read_only: bool = False,
        delegate_handler: ToolFunction | None = None,
        max_delegated_tasks: int = 4,
        on_run_start: Callable[[], None] | None = None,
    ) -> None:
        if read_only and delegate_handler is not None:
            raise ValueError("a read-only registry cannot delegate to more agents")
        if not 1 <= max_delegated_tasks <= 4:
            raise ValueError("max_delegated_tasks must be between 1 and 4")
        self.workspace = workspace
        all_functions: dict[str, ToolFunction] = {
            "list_files": workspace.list_files,
            "read_file": workspace.read_file,
            "search_files": workspace.search_files,
            "write_file": workspace.write_file,
            "replace_in_file": workspace.replace_in_file,
            "run_command": workspace.run_command,
        }
        if not read_only:
            all_functions["list_workspaces"] = workspace.list_workspaces
            all_functions["select_workspace"] = workspace.select_workspace
        allowed_names = READ_ONLY_TOOL_NAMES if read_only else frozenset(all_functions)
        self._functions = {
            name: handler
            for name, handler in all_functions.items()
            if name in allowed_names
        }
        self._schemas = [
            schema
            for schema in TOOL_SCHEMAS
            if schema["function"]["name"] in allowed_names
        ]
        self._on_run_start = on_run_start
        if delegate_handler is not None:
            self._functions[DELEGATION_TOOL_NAME] = delegate_handler
            self._schemas.append(_delegation_schema(max_delegated_tasks))

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def start_run(self) -> None:
        """Reset per-user-turn tool state when a configured extension needs it."""

        if self._on_run_start is not None:
            self._on_run_start()

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


def _delegation_schema(max_tasks: int) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": DELEGATION_TOOL_NAME,
            "description": (
                "Run independent, bounded, read-only investigations in parallel. "
                "Subagents can only list, read, and search workspace files; the main "
                "agent remains solely responsible for edits, commands, and verification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tasks,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Unique short identifier.",
                                },
                                "objective": {
                                    "type": "string",
                                    "description": (
                                        "A self-contained read-only investigation goal."
                                    ),
                                },
                            },
                            "required": ["id", "objective"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
        },
    }


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
    {
        "type": "function",
        "function": {
            "name": "list_workspaces",
            "description": (
                "List candidate workspace directories inside the workspace scope "
                "(the scope root and project-like subdirectories). Paths are "
                "scope-relative; the current workspace is marked."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_workspace",
            "description": (
                "Switch the active workspace root to another directory inside the "
                "workspace scope. The path is scope-relative (use . for the scope "
                "root). File tools and command cwd immediately resolve against the "
                "new root. Never use run_command to move between directories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Scope-relative directory path.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]
