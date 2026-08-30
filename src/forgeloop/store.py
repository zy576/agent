"""Persistent workspace/session state for the Web workbench (DSH-style).

Workspaces and sessions are stored as one local JSON state file so that
conversation history survives restarts. The file is user-owned data in a
dot-directory; corruption degrades to an empty store instead of crashing.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
from typing import Any

DEFAULT_STATE_DIR_NAME = ".forgeloop"
STATE_FILE_NAME = "state.json"


def default_state_dir() -> Path:
    override = os.environ.get("FORGELOOP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_STATE_DIR_NAME


@dataclass(slots=True)
class WorkspaceRecord:
    id: str
    path: str
    title: str
    created_at: float = field(default_factory=time.time)
    session_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "created_at": self.created_at,
            "session_ids": list(self.session_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceRecord":
        return cls(
            id=str(data["id"]),
            path=str(data["path"]),
            title=str(data["title"]),
            created_at=float(data.get("created_at") or time.time()),
            session_ids=[str(item) for item in data.get("session_ids", [])],
        )


@dataclass(slots=True)
class SessionRecord:
    id: str
    workspace_id: str
    path: str = ""
    title: str = "新会话"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "new"
    archived: bool = False
    verification_pending: bool = False
    turn_count: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    conversation: list[dict[str, Any]] = field(default_factory=list)
    latest_outcome: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "path": self.path,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "archived": self.archived,
            "verification_pending": self.verification_pending,
            "turn_count": self.turn_count,
            "messages": deepcopy(self.messages),
            "conversation": deepcopy(self.conversation),
            "latest_outcome": deepcopy(self.latest_outcome),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        return cls(
            id=str(data["id"]),
            workspace_id=str(data["workspace_id"]),
            path=str(data.get("path") or ""),
            title=str(data.get("title") or "新会话"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            status=str(data.get("status") or "new"),
            archived=bool(data.get("archived")),
            verification_pending=bool(data.get("verification_pending")),
            turn_count=int(data.get("turn_count") or 0),
            messages=list(data.get("messages") or []),
            conversation=list(data.get("conversation") or []),
            latest_outcome=data.get("latest_outcome"),
        )


class SessionStore:
    """In-memory registry of workspaces and sessions backed by one JSON file."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = (state_dir or default_state_dir()).expanduser().resolve()
        self.state_file = self.state_dir / STATE_FILE_NAME
        self.workspaces: dict[str, WorkspaceRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        try:
            for item in raw.get("workspaces", []):
                record = WorkspaceRecord.from_dict(item)
                self.workspaces[record.id] = record
            for item in raw.get("sessions", []):
                record = SessionRecord.from_dict(item)
                self.sessions[record.id] = record
        except (KeyError, TypeError, ValueError):
            self.workspaces.clear()
            self.sessions.clear()

    def save(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "workspaces": [
                    record.to_dict() for record in self.workspaces.values()
                ],
                "sessions": [record.to_dict() for record in self.sessions.values()],
            }
            data = json.dumps(payload, ensure_ascii=False, indent=2)
            temporary = self.state_file.with_name(self.state_file.name + ".tmp")
            temporary.write_text(data, encoding="utf-8")
            os.replace(temporary, self.state_file)
        except OSError:
            # Persistence is best-effort: a failed save must not kill a turn.
            return
