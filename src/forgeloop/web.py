"""Loopback-only Web console for ForgeLoop."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .agent import AgentResult, CodingAgent
from .client import ModelError
from .cli import _one_line, _redact, _summarize_arguments
from .store import SessionRecord, SessionStore, WorkspaceRecord
from .tools import ToolError, Workspace


MAX_BODY_BYTES = 64 * 1024
MAX_BODY_READ_SECONDS = 5.0
MAX_TASK_CHARS = 16_000
MAX_EVENTS_PER_RUN = 2_048
MAX_CONVERSATION_ITEMS = 100
ASSET_TYPES = {
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/yuqi-cool-profile.png": ("yuqi-cool-profile.png", "image/png"),
    "/assets/yuqi-cool-portrait.png": ("yuqi-cool-portrait.png", "image/png"),
    "/assets/yuqi-soft-window.png": ("yuqi-soft-window.png", "image/png"),
}
TEXT_ASSET_NAMES = frozenset({"index.html", "styles.css", "app.js"})
BINARY_ASSET_NAMES = frozenset(
    {
        "yuqi-cool-profile.png",
        "yuqi-cool-portrait.png",
        "yuqi-soft-window.png",
    }
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


AgentFactory = Callable[[Callable[[dict[str, Any]], None]], CodingAgent]
class WebBusyError(RuntimeError):
    """A turn is already running against the shared workspace."""


class WebPoisonedError(RuntimeError):
    """An interrupted turn may have changed files without a closed history."""


class WebClosingError(RuntimeError):
    """The local server has stopped admitting new work."""


@dataclass(slots=True)
class RunState:
    run_id: str
    task: str
    turn: int
    started_at: float = field(default_factory=time.monotonic)
    events: list[dict[str, Any]] = field(default_factory=list)
    base_event_id: int = 0
    next_event_id: int = 0
    done: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)

    def append(self, event: dict[str, Any]) -> None:
        with self.condition:
            record = {"id": self.next_event_id, **event}
            self.next_event_id += 1
            self.events.append(record)
            if len(self.events) > MAX_EVENTS_PER_RUN:
                self.events.pop(0)
                self.base_event_id += 1
            self.condition.notify_all()

    def finish(self) -> None:
        with self.condition:
            self.done = True
            self.condition.notify_all()

    def snapshot(
        self,
        cursor: int,
    ) -> tuple[int, list[dict[str, Any]], int, bool]:
        with self.condition:
            base = self.base_event_id
            start = max(cursor, base) - base
            return base, [dict(item) for item in self.events[start:]], self.next_event_id, self.done

    def wait_for_change(self, cursor: int, timeout: float = 15.0) -> None:
        with self.condition:
            if cursor >= self.next_event_id and not self.done:
                self.condition.wait(timeout=timeout)


class WebApplication:
    """Own one browser conversation and serialize all workspace mutations."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        api_key: str,
        workspace: Workspace | str,
        model: str,
        max_steps: int | None = None,
        max_subagents: int = 0,
        token: str | None = None,
        store_path: Path | None = None,
    ) -> None:
        if not isinstance(max_subagents, int) or isinstance(max_subagents, bool):
            raise ValueError("max_subagents must be an integer")
        if not 0 <= max_subagents <= 4:
            raise ValueError("max_subagents must be between 0 and 4")
        self.agent_factory = agent_factory
        self.api_key = api_key
        self._workspace: Workspace | None = (
            workspace if isinstance(workspace, Workspace) else None
        )
        self.workspace = str(
            workspace.root if isinstance(workspace, Workspace) else workspace
        )
        self.model = model
        self.max_steps = max_steps
        self.max_subagents = max_subagents
        self.token = token or secrets.token_urlsafe(32)
        self.store = SessionStore(store_path)
        self._state_lock = threading.Lock()
        self._history: list[dict[str, Any]] | None = None
        self._verification_pending = False
        self._active_run_id: str | None = None
        self._runs: dict[str, RunState] = {}
        self._workers: set[threading.Thread] = set()
        self._conversation: list[dict[str, Any]] = []
        self._latest_outcome: dict[str, Any] | None = None
        self._turn_count = 0
        self._poisoned = False
        self._closing = False
        self._active_workspace_id = ""
        self._active_session_id = ""
        with self._state_lock:
            self._active_workspace_id = self._workspace_record_for_locked(
                self.workspace
            ).id
            self._active_session_id = self._ensure_blank_session_locked(
                self._active_workspace_id
            )
            self._load_session_views_locked(
                self.store.sessions[self._active_session_id]
            )
            self.store.save()

    def _workspace_record_for_locked(self, path: str) -> WorkspaceRecord:
        try:
            resolved = Path(path).resolve()
        except OSError:
            resolved = Path(path)
        for record in self.store.workspaces.values():
            try:
                if Path(record.path).resolve() == resolved:
                    return record
            except OSError:
                continue
        record = WorkspaceRecord(
            id=secrets.token_urlsafe(8),
            path=path,
            title=self._workspace_title(path),
        )
        self.store.workspaces[record.id] = record
        return record

    @staticmethod
    def _workspace_title(path: str) -> str:
        try:
            name = Path(path).resolve().name
        except OSError:
            name = Path(path).name
        return name or str(path)

    def _ensure_blank_session_locked(self, workspace_id: str) -> str:
        for record in self.store.sessions.values():
            if (
                record.workspace_id == workspace_id
                and record.status == "new"
                and not record.messages
                and not record.conversation
            ):
                return record.id
        record = SessionRecord(
            id=secrets.token_urlsafe(12),
            workspace_id=workspace_id,
        )
        self.store.sessions[record.id] = record
        workspace_record = self.store.workspaces.get(workspace_id)
        if workspace_record is not None and record.id not in workspace_record.session_ids:
            workspace_record.session_ids.append(record.id)
        return record.id

    def _load_session_views_locked(self, record: SessionRecord) -> None:
        self._history = deepcopy(record.messages) if record.messages else None
        self._verification_pending = record.verification_pending
        self._turn_count = record.turn_count
        self._conversation = [dict(item) for item in record.conversation]
        self._latest_outcome = (
            deepcopy(record.latest_outcome) if record.latest_outcome else None
        )

    def _commit_active_session_locked(self, task: str) -> None:
        record = self.store.sessions.get(self._active_session_id)
        if record is None:
            return
        record.messages = deepcopy(self._history) if self._history else []
        record.verification_pending = self._verification_pending
        record.turn_count = self._turn_count
        record.conversation = [dict(item) for item in self._conversation]
        record.latest_outcome = (
            deepcopy(self._latest_outcome) if self._latest_outcome else None
        )
        if self._latest_outcome is not None:
            record.status = str(self._latest_outcome.get("status") or "error")
        if record.title == "新会话" and task.strip():
            record.title = self._session_title(task)
        record.updated_at = time.time()
        self.store.save()

    @staticmethod
    def _session_title(task: str) -> str:
        first_line = task.strip().splitlines()[0].strip()
        return first_line[:24] if first_line else "新会话"

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            active_elapsed_ms = 0
            if self._active_run_id is not None:
                active_state = self._runs.get(self._active_run_id)
                if active_state is not None:
                    active_elapsed_ms = max(
                        0,
                        round(
                            (time.monotonic() - active_state.started_at) * 1_000
                        ),
                    )
            latest_outcome = None
            if self._latest_outcome is not None:
                latest_outcome = {
                    **self._latest_outcome,
                    "changed_files": list(
                        self._latest_outcome.get("changed_files", [])
                    ),
                    "verifications": list(
                        self._latest_outcome.get("verifications", [])
                    ),
                }
            return {
                "workspace": self.workspace,
                "workspace_scope": self._workspace_scope(),
                "model": self.model,
                "max_steps": self.max_steps,
                "max_subagents": self.max_subagents,
                "subagents_read_only": True,
                "busy": self._active_run_id is not None,
                "active_run_id": self._active_run_id,
                "active_elapsed_ms": active_elapsed_ms,
                "turn_count": self._turn_count,
                "verification_pending": self._verification_pending,
                "poisoned": self._poisoned,
                "closing": self._closing,
                "conversation": [dict(item) for item in self._conversation],
                "latest_outcome": latest_outcome,
                "active_workspace_id": self._active_workspace_id,
                "active_session_id": self._active_session_id,
                "workspaces": [
                    {
                        "id": record.id,
                        "path": record.path,
                        "title": record.title,
                        "session_ids": list(record.session_ids),
                    }
                    for record in self.store.workspaces.values()
                ],
                "sessions": [
                    {
                        "id": record.id,
                        "workspace_id": record.workspace_id,
                        "title": record.title,
                        "status": record.status,
                        "created_at": record.created_at,
                        "updated_at": record.updated_at,
                    }
                    for record in self.store.sessions.values()
                ],
            }

    def begin_shutdown(self) -> None:
        with self._state_lock:
            self._closing = True

    def _workspace_scope(self) -> str:
        if self._workspace is not None:
            return str(self._workspace.scope_root)
        return self.workspace

    def browse_directories(self, path: str = "") -> dict[str, Any]:
        """Directory-browser view over the local filesystem."""
        if self._workspace is None:
            return {"path": "", "parent": None, "entries": [], "truncated": False}
        return self._workspace.list_directories(path)

    def switch_workspace(self, path: str) -> dict[str, Any]:
        """Atomically re-bind to a user-chosen directory and adopt its workspace/session."""
        if self._workspace is None:
            raise ValueError("workspace switching is not available in this session")
        with self._state_lock:
            if self._closing:
                raise WebClosingError("ForgeLoop Web is shutting down.")
            if self._active_run_id is not None:
                raise WebBusyError("A ForgeLoop turn is already running.")
            previous_root = str(self._workspace.root)
            previous_scope = str(self._workspace.scope_root)
            self._workspace.rebind_any(path)
            new_root = str(self._workspace.root)
            new_scope = str(self._workspace.scope_root)
            self.workspace = new_root
            workspace_record = self._workspace_record_for_locked(new_root)
            workspace_changed = workspace_record.id != self._active_workspace_id
            self._active_workspace_id = workspace_record.id
            active_record = self.store.sessions.get(self._active_session_id)
            same_workspace_session = (
                active_record is not None
                and active_record.workspace_id == workspace_record.id
            )
            session_reset = False
            if not same_workspace_session:
                blank_id = self._ensure_blank_session_locked(workspace_record.id)
                if blank_id != self._active_session_id:
                    self._active_session_id = blank_id
                    self._load_session_views_locked(self.store.sessions[blank_id])
                    self._poisoned = False
                    session_reset = True
            self._runs.clear()
            self.store.save()
            return {
                "workspace": new_root,
                "workspace_scope": new_scope,
                "previous_workspace": previous_root,
                "workspace_changed": workspace_changed,
                "session_reset": session_reset,
                "conversation_cleared": session_reset,
            }

    def new_session(self) -> None:
        """Start a fresh blank session inside the active workspace."""
        with self._state_lock:
            if self._closing:
                raise WebClosingError("ForgeLoop Web is shutting down.")
            if self._active_run_id is not None:
                raise WebBusyError("A ForgeLoop turn is already running.")
            session_id = self._ensure_blank_session_locked(self._active_workspace_id)
            self._active_session_id = session_id
            self._load_session_views_locked(self.store.sessions[session_id])
            self._poisoned = False
            self._runs.clear()
            self.store.save()

    def select_session(self, session_id: str) -> None:
        """Re-open a stored session, re-binding the workspace it belongs to."""
        with self._state_lock:
            if self._closing:
                raise WebClosingError("ForgeLoop Web is shutting down.")
            if self._active_run_id is not None:
                raise WebBusyError("A ForgeLoop turn is already running.")
            record = self.store.sessions.get(session_id)
            if record is None:
                raise ValueError("session not found")
            self._active_session_id = record.id
            self._active_workspace_id = record.workspace_id
            workspace_record = self.store.workspaces.get(record.workspace_id)
            if workspace_record is not None and self._workspace is not None:
                try:
                    self._workspace.rebind_any(workspace_record.path)
                except ToolError:
                    pass
            if workspace_record is not None:
                self.workspace = workspace_record.path
            self._load_session_views_locked(record)
            self._poisoned = False
            self._runs.clear()

    def start_turn(self, task: str) -> RunState:
        with self._state_lock:
            self._workers = {worker for worker in self._workers if worker.is_alive()}
            if self._closing:
                raise WebClosingError("ForgeLoop Web is shutting down.")
            if self._poisoned:
                raise WebPoisonedError(
                    "The previous turn was interrupted after workspace access; restart ForgeLoop."
                )
            if self._active_run_id is not None:
                raise WebBusyError("A ForgeLoop turn is already running.")
            self._turn_count += 1
            run_id = secrets.token_urlsafe(12)
            state = RunState(run_id=run_id, task=task, turn=self._turn_count)
            self._runs[run_id] = state
            self._active_run_id = run_id
            self._conversation.append({"role": "user", "content": task})
            self._trim_state_locked()
            worker = threading.Thread(
                target=self._execute_turn,
                args=(state,),
                name=f"forgeloop-web-turn-{self._turn_count}",
                daemon=False,
            )
            self._workers.add(worker)
            worker.start()
            return state

    def get_run(self, run_id: str) -> RunState | None:
        with self._state_lock:
            return self._runs.get(run_id)

    def wait_for_workers(self) -> None:
        while True:
            with self._state_lock:
                workers = list(self._workers)
            if not workers:
                return
            for worker in workers:
                worker.join()
            with self._state_lock:
                self._workers.difference_update(workers)

    def _execute_turn(self, state: RunState) -> None:
        state.append({"type": "run_started", "turn": state.turn})

        def on_event(raw_event: dict[str, Any]) -> None:
            safe = _safe_agent_event(raw_event, self.api_key)
            if safe is not None:
                if safe.get("type") == "workspace_changed" and self._workspace is not None:
                    with self._state_lock:
                        self.workspace = str(self._workspace.root)
                state.append(safe)

        try:
            agent = self.agent_factory(on_event)
            with self._state_lock:
                history = self._history
                verification_pending = self._verification_pending
            result = agent.run(
                state.task,
                history=history,
                verification_pending=verification_pending,
            )
            summary = _clip(_redact(result.summary, self.api_key), 24_000)
            result_status = result.status
            if result_status == "completed" and result.verification_pending:
                result_status = "completed_with_verification_risk"
            duration_ms = round((time.monotonic() - state.started_at) * 1_000)
            outcome = {
                "status": result_status,
                "summary": summary,
                "steps": result.steps,
                "changed_files": [
                    _clip(_redact(path, self.api_key), 400)
                    for path in result.changed_files[:50]
                ],
                "verifications": [
                    _clip(_redact(item, self.api_key), 800)
                    for item in result.verifications[:30]
                ],
                "verification_pending": result.verification_pending,
                "duration_ms": duration_ms,
            }
            with self._state_lock:
                self._history = result.messages
                self._verification_pending = result.verification_pending
                self._latest_outcome = outcome
                self._conversation.append(
                    {
                        "role": "assistant",
                        "content": summary,
                        "status": result_status,
                    }
                )
                self._trim_state_locked()
                self._commit_active_session_locked(state.task)
            state.append(
                {
                    "type": "turn_complete",
                    **outcome,
                }
            )
        except (ModelError, OSError, ValueError) as exc:
            self._record_fatal_turn_error(state, exc)
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._record_fatal_turn_error(state, exc)
        finally:
            with self._state_lock:
                if self._active_run_id == state.run_id:
                    self._active_run_id = None
            state.finish()

    def _record_fatal_turn_error(self, state: RunState, exc: Exception) -> None:
        detail = _clip(_redact(str(exc), self.api_key), 1_200)
        message = (
            f"任务中断：{detail}\n为避免工作区与对话历史失配，请关闭并重新启动 ForgeLoop。"
        )
        with self._state_lock:
            self._poisoned = True
            self._latest_outcome = {
                "status": "error",
                "summary": message,
                "steps": 0,
                "changed_files": [],
                "verifications": [],
                "verification_pending": True,
                "duration_ms": round(
                    (time.monotonic() - state.started_at) * 1_000
                ),
            }
            self._conversation.append(
                {"role": "assistant", "content": message, "status": "error"}
            )
            self._trim_state_locked()
            self._commit_active_session_locked(state.task)
        state.append({"type": "turn_error", "message": message})

    def _trim_state_locked(self) -> None:
        if len(self._conversation) > MAX_CONVERSATION_ITEMS:
            self._conversation = self._conversation[-MAX_CONVERSATION_ITEMS:]
        if len(self._runs) > 8:
            removable = [
                run_id
                for run_id, state in self._runs.items()
                if state.done and run_id != self._active_run_id
            ]
            for run_id in removable[: len(self._runs) - 8]:
                self._runs.pop(run_id, None)


def _safe_agent_event(
    event: dict[str, Any], api_key: str
) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type == "model_request":
        return {
            "type": "model_request",
            "step": int(event.get("step", 0)),
            "message_count": int(event.get("message_count", 0)),
        }
    if event_type == "finalization_request":
        return {
            "type": "finalization_request",
            "message_count": int(event.get("message_count", 0)),
        }
    if event_type == "delegation_started":
        return {
            "type": "delegation_started",
            "count": _bounded_nonnegative_int(event.get("count", 0), 64),
        }
    if event_type == "subtask_started":
        return {
            "type": "subtask_started",
            "subtask_id": _safe_event_text(event.get("subtask_id", ""), api_key, 120),
            "label": _safe_event_text(event.get("label", ""), api_key, 120),
            "objective": _safe_event_text(event.get("objective", ""), api_key, 1_200),
        }
    if event_type == "subtask_completed":
        raw_status = str(event.get("status", "unknown"))
        status = raw_status if raw_status in {
            "completed",
            "completed_with_verification_risk",
            "error",
            "failed",
            "cancelled",
            "timed_out",
            "protocol_error",
            "step_limit",
            "tool_call_limit",
            "runtime_limit",
            "repetition_limit",
        } else "unknown"
        return {
            "type": "subtask_completed",
            "subtask_id": _safe_event_text(event.get("subtask_id", ""), api_key, 120),
            "label": _safe_event_text(event.get("label", ""), api_key, 120),
            "status": status,
            "summary": _safe_event_text(event.get("summary", ""), api_key, 2_000),
        }
    if event_type == "delegation_completed":
        return {
            "type": "delegation_completed",
            "completed": _bounded_nonnegative_int(event.get("completed", 0), 64),
            "failed": _bounded_nonnegative_int(event.get("failed", 0), 64),
            "duration_ms": _bounded_nonnegative_int(
                event.get("duration_ms", 0),
                86_400_000,
            ),
            "workspace_stable": event.get("workspace_stable") is True,
        }
    if event_type == "tool_start":
        return {
            "type": "tool_start",
            "step": int(event.get("step", 0)),
            "call_id": _clip(
                _redact(str(event.get("call_id", "")), api_key), 120
            ),
            "tool": _clip(
                _redact(str(event.get("tool", "unknown")), api_key), 80
            ),
            "arguments": _clip(
                _redact(_summarize_arguments(event.get("arguments", {})), api_key),
                1_200,
            ),
        }
    if event_type == "tool_end":
        result = event.get("result", {})
        ok = result.get("ok") is True if isinstance(result, dict) else False
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("output") if ok else result.get("error") or "")
        return {
            "type": "tool_end",
            "step": int(event.get("step", 0)),
            "call_id": _clip(
                _redact(str(event.get("call_id", "")), api_key), 120
            ),
            "tool": _clip(
                _redact(str(event.get("tool", "unknown")), api_key), 80
            ),
            "ok": ok,
            "detail": _clip(_redact(_one_line(detail, 1_200), api_key), 1_200),
        }
    if event_type == "warning":
        return {
            "type": "warning",
            "step": int(event.get("step", 0)),
            "message": _clip(
                _redact(str(event.get("message", "")), api_key), 1_200
            ),
        }
    if event_type == "workspace_changed":
        return {
            "type": "workspace_changed",
            "step": int(event.get("step", 0)),
            "path": _clip(_redact(str(event.get("path", "")), api_key), 400),
        }
    if event_type == "final":
        return {
            "type": "final",
            "status": _clip(str(event.get("status", "unknown")), 80),
            "summary": _clip(
                _redact(str(event.get("summary", "")), api_key), 24_000
            ),
        }
    return None


def _safe_event_text(value: Any, api_key: str, limit: int) -> str:
    return _clip(_redact(str(value), api_key), limit)


def _bounded_nonnegative_int(value: Any, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(normalized, 0), maximum)


def create_server(
    application: WebApplication,
    *,
    port: int = 0,
) -> tuple[ThreadingHTTPServer, str]:
    if not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    handler = _handler_factory(application)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    actual_port = int(server.server_address[1])
    return server, f"{_loopback_origin(actual_port)}/"


def serve_web(
    application: WebApplication,
    *,
    port: int = 0,
    open_browser: bool = True,
    browser_open: Callable[[str], Any] = webbrowser.open,
) -> int:
    server, url = create_server(application, port=port)
    print(f"ForgeLoop Web: {url}", flush=True)
    print("Only this computer can access the interface. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        def open_preview() -> None:
            try:
                if browser_open(url) is False:
                    print(
                        "Browser did not open automatically; copy the URL above.",
                        file=sys.stderr,
                    )
            except Exception:
                print(
                    "Browser did not open automatically; copy the URL above.",
                    file=sys.stderr,
                )

        opener = threading.Timer(0.05, open_preview)
        opener.name = "forgeloop-web-browser-opener"
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping ForgeLoop Web...", flush=True)
    finally:
        application.begin_shutdown()
        server.server_close()
        application.wait_for_workers()
    return 0


def _handler_factory(application: WebApplication):
    class ForgeLoopHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ForgeLoopWeb/1.0"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if not self._request_is_local():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                source = _read_asset("index.html")
                rendered = source.replace(
                    "__FORGELOOP_TOKEN__", escape(application.token, quote=True)
                )
                self._send_bytes(
                    HTTPStatus.OK,
                    rendered.encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if parsed.path in ASSET_TYPES:
                asset_name, content_type = ASSET_TYPES[parsed.path]
                self._send_bytes(
                    HTTPStatus.OK,
                    _read_asset_bytes(asset_name),
                    content_type,
                )
                return
            if parsed.path == "/api/browse":
                if not self._token_is_valid():
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"path"} or any(
                    len(values) != 1 for values in query.values()
                ):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid query"})
                    return
                raw_path = (query.get("path") or [""])[0]
                if len(raw_path) > 1_024:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid path"})
                    return
                try:
                    view = application.browse_directories(raw_path)
                except (ToolError, ValueError) as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": _redact(str(exc), application.api_key)},
                    )
                    return
                self._send_json(HTTPStatus.OK, view)
                return
            if parsed.path == "/api/status":
                if not self._token_is_valid():
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                    return
                self._send_json(HTTPStatus.OK, application.snapshot())
                return
            if parsed.path == "/api/events":
                if not self._token_is_valid():
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) != {"run_id", "cursor"} or any(
                    len(values) != 1 for values in query.values()
                ):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid stream"})
                    return
                run_id = (query.get("run_id") or [""])[0]
                raw_cursor = (query.get("cursor") or ["0"])[0]
                try:
                    cursor = int(raw_cursor)
                except ValueError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid cursor"})
                    return
                if cursor < 0 or len(run_id) > 80:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid stream"})
                    return
                state = application.get_run(run_id)
                if state is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                    return
                _, _, next_event_id, _ = state.snapshot(0)
                cursor = min(cursor, next_event_id)
                self._stream_events(state, cursor)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            # Never reuse a connection whose request body may remain unread. This
            # prevents a rejected body from being parsed as a second HTTP request.
            self.close_connection = True
            if not self._request_is_local():
                self._reject_unread_body(HTTPStatus.FORBIDDEN, "forbidden")
                return
            parsed = urlsplit(self.path)
            if parsed.path not in {"/api/turn", "/api/workspace", "/api/dir", "/api/session"}:
                self._reject_unread_body(HTTPStatus.NOT_FOUND, "not found")
                return
            if not self._mutation_is_authorized():
                self._reject_unread_body(HTTPStatus.FORBIDDEN, "forbidden")
                return
            try:
                payload = self._read_json_body()
            except _HttpInputError as exc:
                self._send_json(exc.status, {"error": exc.message})
                return
            if parsed.path == "/api/workspace":
                self._handle_workspace_switch(payload)
                return
            if parsed.path == "/api/dir":
                self._handle_directory_create(payload)
                return
            if parsed.path == "/api/session":
                self._handle_session_action(payload)
                return
            if set(payload) != {"task"} or not isinstance(payload.get("task"), str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
                return
            task = payload["task"].strip()
            if not task or len(task) > MAX_TASK_CHARS:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid task"})
                return
            try:
                state = application.start_turn(task)
            except WebBusyError:
                self._send_json(HTTPStatus.CONFLICT, {"error": "agent is busy"})
                return
            except WebPoisonedError:
                self._send_json(
                    HTTPStatus.LOCKED,
                    {"error": "restart ForgeLoop before continuing"},
                )
                return
            except WebClosingError:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "ForgeLoop Web is shutting down"},
                )
                return
            self._send_json(
                HTTPStatus.ACCEPTED,
                {"run_id": state.run_id, "turn": state.turn},
            )

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
            self.close_connection = True
            self._reject_unread_body(HTTPStatus.METHOD_NOT_ALLOWED, "not allowed")

        def _handle_workspace_switch(self, payload: dict[str, Any]) -> None:
            if set(payload) != {"path"} or not isinstance(payload.get("path"), str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
                return
            path = payload["path"].strip()
            if not path or len(path) > 512:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid path"})
                return
            try:
                result = application.switch_workspace(path)
            except WebBusyError:
                self._send_json(HTTPStatus.CONFLICT, {"error": "agent is busy"})
                return
            except WebClosingError:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "ForgeLoop Web is shutting down"},
                )
                return
            except (ToolError, ValueError) as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": _redact(str(exc), application.api_key)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {**result, "state": application.snapshot()},
            )

        def _handle_directory_create(self, payload: dict[str, Any]) -> None:
            if (
                set(payload) != {"parent", "name"}
                or not isinstance(payload.get("parent"), str)
                or not isinstance(payload.get("name"), str)
            ):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
                return
            parent = payload["parent"].strip()
            name = payload["name"]
            if not parent or len(parent) > 1_024 or not name or len(name) > 255:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
                return
            if application._workspace is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "directory creation is not available in this session"},
                )
                return
            try:
                created = application._workspace.create_directory(parent, name)
            except (ToolError, ValueError) as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": _redact(str(exc), application.api_key)},
                )
                return
            self._send_json(HTTPStatus.OK, {"path": created})

        def _handle_session_action(self, payload: dict[str, Any]) -> None:
            action = payload.get("action")
            if action == "new":
                if set(payload) != {"action"}:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
                    return
                try:
                    application.new_session()
                except WebBusyError:
                    self._send_json(HTTPStatus.CONFLICT, {"error": "agent is busy"})
                    return
                except WebClosingError:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "ForgeLoop Web is shutting down"},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"state": application.snapshot()})
                return
            if action == "select":
                if (
                    set(payload) != {"action", "session_id"}
                    or not isinstance(payload.get("session_id"), str)
                    or not payload["session_id"]
                ):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
                    return
                try:
                    application.select_session(payload["session_id"])
                except ValueError:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "session not found"})
                    return
                except WebBusyError:
                    self._send_json(HTTPStatus.CONFLICT, {"error": "agent is busy"})
                    return
                except WebClosingError:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "ForgeLoop Web is shutting down"},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"state": application.snapshot()})
                return
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})

        def _request_is_local(self) -> bool:
            expected_host = _loopback_authority(int(self.server.server_address[1]))
            return (
                self.client_address[0] == "127.0.0.1"
                and self.headers.get("Host", "") == expected_host
            )

        def _token_is_valid(self) -> bool:
            supplied = self.headers.get("X-ForgeLoop-Token", "")
            return secrets.compare_digest(supplied, application.token)

        def _mutation_is_authorized(self) -> bool:
            expected_origin = _loopback_origin(int(self.server.server_address[1]))
            return (
                self._token_is_valid()
                and self.headers.get("Origin", "") == expected_origin
                and self.headers.get("Content-Type", "").lower()
                == "application/json"
            )

        def _read_json_body(self) -> dict[str, Any]:
            if self.headers.get("Transfer-Encoding"):
                raise _HttpInputError(HTTPStatus.BAD_REQUEST, "unsupported encoding")
            if self.headers.get("Content-Encoding"):
                raise _HttpInputError(HTTPStatus.BAD_REQUEST, "unsupported encoding")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise _HttpInputError(HTTPStatus.LENGTH_REQUIRED, "length required")
            if len(self.headers.get_all("Content-Length", [])) != 1:
                raise _HttpInputError(HTTPStatus.BAD_REQUEST, "invalid length")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise _HttpInputError(HTTPStatus.BAD_REQUEST, "invalid length") from exc
            if length < 0:
                raise _HttpInputError(HTTPStatus.BAD_REQUEST, "invalid length")
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                raise _HttpInputError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large"
                )
            previous_timeout = self.connection.gettimeout()
            self.connection.settimeout(MAX_BODY_READ_SECONDS)
            try:
                raw_body = self.rfile.read(length)
                if len(raw_body) != length:
                    raise _HttpInputError(
                        HTTPStatus.BAD_REQUEST, "incomplete request body"
                    )
                decoded = raw_body.decode("utf-8")
                payload = json.loads(decoded)
            except TimeoutError as exc:
                raise _HttpInputError(
                    HTTPStatus.REQUEST_TIMEOUT, "request body timed out"
                ) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _HttpInputError(HTTPStatus.BAD_REQUEST, "invalid json") from exc
            finally:
                self.connection.settimeout(previous_timeout)
            if not isinstance(payload, dict):
                raise _HttpInputError(HTTPStatus.BAD_REQUEST, "invalid request")
            return payload

        def _reject_unread_body(self, status: HTTPStatus, message: str) -> None:
            self._discard_bounded_body()
            self._send_json(status, {"error": message})

        def _discard_bounded_body(self) -> None:
            if self.headers.get("Transfer-Encoding") or self.headers.get(
                "Content-Encoding"
            ):
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                return
            try:
                length = int(lengths[0])
            except ValueError:
                return
            if not 0 <= length <= MAX_BODY_BYTES:
                return
            previous_timeout = self.connection.gettimeout()
            self.connection.settimeout(MAX_BODY_READ_SECONDS)
            try:
                self.rfile.read(length)
            except (TimeoutError, OSError):
                return
            finally:
                self.connection.settimeout(previous_timeout)

        def _stream_events(self, state: RunState, cursor: int) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Connection", "close")
            self._send_security_headers()
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    base, records, next_id, done = state.snapshot(cursor)
                    if cursor < base:
                        self._write_stream_record(
                            {
                                "id": base - 1,
                                "type": "gap",
                                "message": "较早的执行事件已被压缩。",
                            }
                        )
                        cursor = base
                    for record in records:
                        self._write_stream_record(record)
                        cursor = int(record["id"]) + 1
                    if done and cursor >= next_id:
                        break
                    state.wait_for_change(cursor)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _write_stream_record(self, record: dict[str, Any]) -> None:
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(encoded.encode("utf-8") + b"\n")
            self.wfile.flush()

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self._send_bytes(status, encoded, "application/json; charset=utf-8")

        def _send_bytes(
            self,
            status: HTTPStatus,
            payload: bytes,
            content_type: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self._send_security_headers()
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def _send_security_headers(self) -> None:
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ForgeLoopHandler


@dataclass(slots=True)
class _HttpInputError(Exception):
    status: HTTPStatus
    message: str


def _read_asset(name: str) -> str:
    if name not in TEXT_ASSET_NAMES:
        raise FileNotFoundError(name)
    return files("forgeloop").joinpath("web_static", name).read_text(encoding="utf-8")


def _read_asset_bytes(name: str) -> bytes:
    if name not in TEXT_ASSET_NAMES | BINARY_ASSET_NAMES:
        raise FileNotFoundError(name)
    return files("forgeloop").joinpath("web_static", name).read_bytes()


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _loopback_authority(port: int) -> str:
    return "127.0.0.1" if port == 80 else f"127.0.0.1:{port}"


def _loopback_origin(port: int) -> str:
    return f"http://{_loopback_authority(port)}"
