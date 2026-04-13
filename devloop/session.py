"""Per-repository session state storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid
from typing import Any

from devloop.errors import SessionError
from devloop import yaml_compat as yaml


@dataclass(slots=True)
class SessionState:
    repo_root: str
    session_id: str
    initialized: bool
    last_run_at: str
    last_generated_prompt: str = ""
    last_parsed_llm_response: dict[str, Any] = field(default_factory=dict)
    last_applied_patch_summary: str = ""
    last_known_task_summary: str = ""
    last_known_current_goal: str = ""
    last_truncation_report: str = ""
    command_history_summary: list[str] = field(default_factory=list)
    followup_prompt_count: int = 0

    def touch(self) -> None:
        self.last_run_at = _utc_now()

    def add_history_entry(self, entry: str, max_entries: int = 20) -> None:
        cleaned = " ".join(entry.split())
        if not cleaned:
            return
        self.command_history_summary.append(cleaned)
        if len(self.command_history_summary) > max_entries:
            self.command_history_summary = self.command_history_summary[-max_entries:]

    def note_followup_prompt_generated(self) -> None:
        self.followup_prompt_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "session_id": self.session_id,
            "initialized": self.initialized,
            "last_run_at": self.last_run_at,
            "last_generated_prompt": self.last_generated_prompt,
            "last_parsed_llm_response": self.last_parsed_llm_response,
            "last_applied_patch_summary": self.last_applied_patch_summary,
            "last_known_task_summary": self.last_known_task_summary,
            "last_known_current_goal": self.last_known_current_goal,
            "last_truncation_report": self.last_truncation_report,
            "command_history_summary": list(self.command_history_summary),
            "followup_prompt_count": self.followup_prompt_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        return cls(
            repo_root=str(data["repo_root"]),
            session_id=str(data["session_id"]),
            initialized=bool(data.get("initialized", False)),
            last_run_at=str(data.get("last_run_at", _utc_now())),
            last_generated_prompt=str(data.get("last_generated_prompt", "")),
            last_parsed_llm_response=dict(data.get("last_parsed_llm_response", {})),
            last_applied_patch_summary=str(data.get("last_applied_patch_summary", "")),
            last_known_task_summary=str(data.get("last_known_task_summary", "")),
            last_known_current_goal=str(data.get("last_known_current_goal", "")),
            last_truncation_report=str(data.get("last_truncation_report", "")),
            command_history_summary=list(data.get("command_history_summary", [])),
            followup_prompt_count=int(data.get("followup_prompt_count", 0)),
        )


class SessionStore:
    """Read and write session state outside the repository tree."""

    def __init__(self, repo_root: Path, state_dir_mode: str) -> None:
        self.repo_root = repo_root.resolve()
        self.state_dir = resolve_state_dir(self.repo_root, state_dir_mode)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.state_dir / "session.yaml"

    def load_or_create(self) -> SessionState:
        if not self.session_path.exists():
            session = self.reset()
            return session
        try:
            raw = yaml.safe_load(self.session_path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise SessionError(f"Failed to read session file {self.session_path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise SessionError(f"Failed to parse session file {self.session_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SessionError(f"Session file must contain a mapping: {self.session_path}")
        try:
            return SessionState.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(f"Session file is malformed: {self.session_path}") from exc

    def reset(self) -> SessionState:
        session = SessionState(
            repo_root=str(self.repo_root),
            session_id=str(uuid.uuid4()),
            initialized=False,
            last_run_at=_utc_now(),
        )
        self.save(session)
        return session

    def save(self, session: SessionState) -> None:
        try:
            text = yaml.safe_dump(session.to_dict(), sort_keys=False, allow_unicode=True)
            self.session_path.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise SessionError(f"Failed to write session state: {exc}") from exc


def resolve_state_dir(repo_root: Path, state_dir_mode: str) -> Path:
    if state_dir_mode != "localappdata":
        raise SessionError("Only localappdata session storage is supported")
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("LOCAL_APP_DATA")
        or Path.home() / "AppData" / "Local"
    )
    repo_hash = hashlib.sha256(str(repo_root).lower().encode("utf-8")).hexdigest()[:16]
    return base / "devloop" / "repos" / repo_hash


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
