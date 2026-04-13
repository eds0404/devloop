"""Windows clipboard helpers backed by PowerShell."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from devloop.errors import ClipboardError


def get_clipboard_text() -> str:
    """Read Unicode text from the Windows clipboard via PowerShell."""
    completed = _run_powershell(
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw"
    )
    return completed.stdout.decode("utf-8", errors="replace")


def set_clipboard_text(text: str) -> None:
    """Write Unicode text to the Windows clipboard via PowerShell."""
    command = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
        "$text = [Console]::In.ReadToEnd(); "
        "Set-Clipboard -Value $text"
    )
    _run_powershell(command, input_bytes=text.encode("utf-8"))


def _run_powershell(command: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    _ensure_windows()
    executable = _resolve_powershell_executable()
    try:
        return subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", command],
            input=input_bytes,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ClipboardError("PowerShell was not found. Clipboard access requires PowerShell.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        if stderr:
            raise ClipboardError(f"PowerShell clipboard operation failed: {stderr}") from exc
        raise ClipboardError("PowerShell clipboard operation failed.") from exc


def _resolve_powershell_executable() -> str:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidates = [
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        shutil.which("powershell.exe"),
        shutil.which("powershell"),
        shutil.which("pwsh.exe"),
        shutil.which("pwsh"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
    raise ClipboardError("PowerShell was not found. Windows clipboard support requires PowerShell.")


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise ClipboardError("Windows clipboard support is required for this MVP")
