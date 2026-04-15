"""Controlled Git integration used by devloop."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from devloop.errors import GitError


def discover_repo_root(project_path: Path) -> Path:
    """Detect the Git repository root for the configured project path."""
    candidate = project_path.resolve()
    git_executable = find_git_executable()
    if git_executable:
        try:
            result = subprocess.run(
                [git_executable, "-C", str(candidate), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            repo_root = Path(result.stdout.strip())
            if repo_root.exists():
                return repo_root.resolve()
        except subprocess.CalledProcessError:
            pass

    for directory in [candidate, *candidate.parents]:
        dot_git = directory / ".git"
        if dot_git.exists():
            return directory.resolve()
    raise GitError(f"No Git repository found for project path: {project_path}")


def find_git_executable() -> str | None:
    git_path = shutil.which("git")
    if git_path:
        return git_path

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Git" / "cmd" / "git.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def run_git(
    repo_root: Path,
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    git_executable = find_git_executable()
    if not git_executable:
        raise GitError("Git executable was not found")
    try:
        return subprocess.run(
            [git_executable, "-C", str(repo_root), *args],
            input=input_text,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip()
        raise GitError(stderr or f"Git command failed: {' '.join(args)}") from exc


def get_head_commit(repo_root: Path) -> str:
    result = run_git(repo_root, ["rev-parse", "HEAD"])
    return result.stdout.strip()


def get_paths_diff(repo_root: Path, paths: list[Path]) -> str:
    if not paths:
        return ""
    rendered_paths = [str(path) for path in paths]
    sections: list[str] = []
    cached = run_git(repo_root, ["diff", "--cached", "--", *rendered_paths]).stdout.strip()
    if cached:
        sections.append("BEGIN CACHED DIFF")
        sections.append(cached)
        sections.append("END CACHED DIFF")
    worktree = run_git(repo_root, ["diff", "--", *rendered_paths]).stdout.strip()
    if worktree:
        sections.append("BEGIN WORKTREE DIFF")
        sections.append(worktree)
        sections.append("END WORKTREE DIFF")
    return "\n".join(sections).strip()


def list_dirty_paths(repo_root: Path, paths: list[Path]) -> list[str]:
    if not paths:
        return []
    result = run_git(repo_root, ["status", "--porcelain=v1", "--", *[str(path) for path in paths]])
    dirty: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        dirty.append(line[3:].strip())
    return dirty


def summarize_paths_status(repo_root: Path, paths: list[Path]) -> str:
    if not paths:
        return ""
    result = run_git(repo_root, ["status", "--short", "--", *[str(path) for path in paths]])
    return result.stdout.strip()

