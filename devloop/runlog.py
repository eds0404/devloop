"""Append-only execution log for devloop runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from devloop.git_tools import get_head_commit


def default_log_path_for_config(config_path: Path) -> Path:
    return config_path.resolve().with_name(".devloop.log")


def resolve_devloop_head() -> str:
    try:
        repo_root = Path(__file__).resolve().parents[1]
        return get_head_commit(repo_root)
    except Exception:
        return "unknown"


@dataclass(slots=True)
class RunLogRecorder:
    config_path: Path
    argv: list[str]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    devloop_head: str = field(default_factory=resolve_devloop_head)
    log_path: Path = field(init=False)
    clipboard_before: str = ""
    clipboard_after: str = ""
    config_text: str = ""
    console_output: str = ""
    extra_sections: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.config_path = self.config_path.resolve()
        self.log_path = default_log_path_for_config(self.config_path)
        self.config_text = self._read_config_text()

    def append_console(self, text: str) -> None:
        self.console_output += text

    def record_clipboard_before(self, text: str) -> None:
        self.clipboard_before = text

    def record_clipboard_after(self, text: str) -> None:
        self.clipboard_after = text

    def finalize(self, exit_code: int) -> None:
        finished_at = datetime.now(timezone.utc)
        entry = self._format_entry(finished_at=finished_at, exit_code=exit_code)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(entry)

    def add_section(self, title: str, body: str) -> None:
        normalized_title = title.strip()
        normalized_body = body.rstrip("\n")
        if not normalized_title:
            return
        self.extra_sections.append((normalized_title, normalized_body))

    def _read_config_text(self) -> str:
        try:
            return self.config_path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"<failed to read config file: {exc}>"

    def _format_entry(self, *, finished_at: datetime, exit_code: int) -> str:
        lines = [
            "========== DEVLOOP RUN ==========",
            f"Started at (UTC): {self.started_at.isoformat()}",
            f"Finished at (UTC): {finished_at.isoformat()}",
            f"Exit code: {exit_code}",
            f"Devloop HEAD: {self.devloop_head}",
            f"Config path: {self.config_path}",
            "Arguments:",
        ]
        if self.argv:
            lines.extend(f"  [{index}] {value}" for index, value in enumerate(self.argv))
        else:
            lines.append("  <no arguments>")
        lines.extend(
            [
                "----- BEGIN CLIPBOARD BEFORE -----",
                self.clipboard_before,
                "----- END CLIPBOARD BEFORE -----",
                "----- BEGIN CONFIG FILE -----",
                self.config_text,
                "----- END CONFIG FILE -----",
                "----- BEGIN CONSOLE OUTPUT -----",
                self.console_output,
                "----- END CONSOLE OUTPUT -----",
                "----- BEGIN CLIPBOARD AFTER -----",
                self.clipboard_after,
                "----- END CLIPBOARD AFTER -----",
            ]
        )
        for title, body in self.extra_sections:
            lines.extend(
                [
                    f"----- BEGIN {title} -----",
                    body,
                    f"----- END {title} -----",
                ]
            )
        lines.extend(
            [
                "========== END DEVLOOP RUN ==========",
                "",
            ]
        )
        return "\n".join(lines)
