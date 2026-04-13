"""Safe Git-aware patch validation and application."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import shlex
import tempfile

from devloop.errors import PatchApplyError
from devloop.git_tools import list_dirty_paths, run_git, summarize_paths_status


@dataclass(slots=True)
class PatchTarget:
    path: PurePosixPath
    change_type: str


@dataclass(slots=True)
class PatchApplyResult:
    affected_files: list[str]
    git_status_summary: str


PATCH_FENCE_LINES = {
    "```",
    "```diff",
    "```patch",
    "```git",
}
PATCH_METADATA_PREFIXES = (
    "index ",
    "--- ",
    "+++ ",
    "new file mode",
    "deleted file mode",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "copy from ",
    "copy to ",
    "rename from ",
    "rename to ",
    "Binary files ",
    "GIT binary patch",
)


def extract_patch_targets(patch_text: str) -> list[PatchTarget]:
    patch_text = normalize_patch_text(patch_text)
    _validate_patch_shape(patch_text)
    targets: list[PatchTarget] = []
    current_target: PatchTarget | None = None

    for raw_line in patch_text.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("diff --git "):
            current_target = _parse_diff_header(line)
            targets.append(current_target)
            continue
        if current_target is None:
            continue
        if line.startswith("rename from ") or line.startswith("rename to "):
            raise PatchApplyError("Rename patches are not supported in the MVP")
        if line.startswith("GIT binary patch"):
            raise PatchApplyError("Binary patches are not supported in the MVP")
        if line.startswith("new file mode"):
            current_target.change_type = "add"
        elif line.startswith("deleted file mode"):
            current_target.change_type = "delete"
        elif line.startswith("--- /dev/null"):
            current_target.change_type = "add"
        elif line.startswith("+++ /dev/null"):
            current_target.change_type = "delete"

    if not targets:
        raise PatchApplyError("Patch does not contain any diff --git headers")
    return _dedupe_targets(targets)


def apply_patch(
    repo_root: Path,
    state_dir: Path,
    patch_text: str,
    *,
    allow_apply_on_dirty_files: bool,
) -> PatchApplyResult:
    patch_text = normalize_patch_text(patch_text)
    targets = extract_patch_targets(patch_text)
    affected_paths = [validate_repo_relative_path(target.path) for target in targets]
    repo_relative_paths = [Path(*path.parts) for path in affected_paths]

    if not allow_apply_on_dirty_files:
        dirty = list_dirty_paths(repo_root, repo_relative_paths)
        if dirty:
            dirty_text = ", ".join(sorted(dirty))
            raise PatchApplyError(
                f"Refusing to apply patch because affected files are dirty: {dirty_text}"
            )

    state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".patch",
        delete=False,
        dir=state_dir,
        newline="\n",
    ) as handle:
        handle.write(patch_text)
        patch_path = Path(handle.name)

    try:
        run_git(repo_root, ["apply", "--check", "--index", "--verbose", str(patch_path)])
        run_git(repo_root, ["apply", "--index", "--verbose", str(patch_path)])
        _verify_staged_paths(repo_root, repo_relative_paths)
        status_summary = summarize_paths_status(repo_root, repo_relative_paths)
        return PatchApplyResult(
            affected_files=[path.as_posix() for path in affected_paths],
            git_status_summary=status_summary,
        )
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, PatchApplyError):
            raise
        raise PatchApplyError(_patch_apply_failure_message(str(exc))) from exc
    finally:
        try:
            patch_path.unlink(missing_ok=True)
        except OSError:
            pass


def validate_repo_relative_path(path: PurePosixPath) -> PurePosixPath:
    if path.is_absolute():
        raise PatchApplyError(f"Patch path must be repository-relative: {path}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PatchApplyError(f"Unsafe patch path: {path}")
    if any(":" in part for part in path.parts):
        raise PatchApplyError(f"Unsafe patch path: {path}")
    if path.parts and path.parts[0] == ".git":
        raise PatchApplyError("Patches may not touch .git internals")
    return path


def normalize_patch_text(patch_text: str) -> str:
    text = patch_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    normalized_lines: list[str] = []
    in_hunk = False

    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if stripped in PATCH_FENCE_LINES:
            continue

        left_trimmed = raw_line.lstrip(" ")
        if left_trimmed.startswith("diff --git "):
            normalized_lines.append(left_trimmed)
            in_hunk = False
            continue
        if left_trimmed == "@@" or left_trimmed.startswith("@@ "):
            normalized_lines.append(left_trimmed)
            in_hunk = True
            continue
        if in_hunk:
            normalized_lines.append(_normalize_hunk_line(raw_line))
            continue
        if any(left_trimmed.startswith(prefix) for prefix in PATCH_METADATA_PREFIXES):
            normalized_lines.append(left_trimmed)
            continue
        normalized_lines.append(raw_line)

    while normalized_lines and not normalized_lines[0].strip():
        normalized_lines.pop(0)
    while normalized_lines and not normalized_lines[-1].strip():
        normalized_lines.pop()
    return "\n".join(normalized_lines)


def _validate_patch_shape(patch_text: str) -> None:
    first_content_line = None
    for raw_line in patch_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        first_content_line = stripped
        break
    if first_content_line is None:
        raise PatchApplyError("Patch is empty")
    if not first_content_line.startswith("diff --git "):
        raise PatchApplyError(
            "Patch must contain only a Git unified diff and must start with `diff --git `"
        )


def _parse_diff_header(line: str) -> PatchTarget:
    rest = line[len("diff --git ") :]
    try:
        parts = shlex.split(rest)
    except ValueError as exc:
        raise PatchApplyError(f"Failed to parse diff header: {line}") from exc
    if len(parts) < 2:
        raise PatchApplyError(f"Invalid diff header: {line}")
    left, right = parts[0], parts[1]
    if not left.startswith("a/") or not right.startswith("b/"):
        raise PatchApplyError(f"Invalid diff header paths: {line}")
    left_path = PurePosixPath(left[2:])
    right_path = PurePosixPath(right[2:])
    chosen = right_path if right_path.as_posix() != "/dev/null" else left_path
    validate_repo_relative_path(chosen)
    return PatchTarget(path=chosen, change_type="modify")


def _dedupe_targets(targets: list[PatchTarget]) -> list[PatchTarget]:
    seen: dict[str, PatchTarget] = {}
    for target in targets:
        key = target.path.as_posix()
        if key in seen:
            raise PatchApplyError(
                f"Patch touches the same path more than once: {key}. Split or merge the hunks into one diff section."
            )
        seen[key] = target
    return list(seen.values())


def _verify_staged_paths(repo_root: Path, repo_relative_paths: list[Path]) -> None:
    expected = {path.as_posix() for path in repo_relative_paths}
    result = run_git(repo_root, ["diff", "--cached", "--name-only", "--", *[str(path) for path in repo_relative_paths]])
    staged = {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}
    if staged != expected:
        raise PatchApplyError(
            "Patch verification failed after apply. "
            f"Expected staged paths: {sorted(expected)}. Actual staged paths: {sorted(staged)}."
        )


def _normalize_hunk_line(raw_line: str) -> str:
    if raw_line.startswith((" ", "+", "-", "\\")):
        return raw_line

    left_trimmed = raw_line.lstrip(" ")
    if left_trimmed.startswith(("+", "-", "\\")):
        return left_trimmed
    if not left_trimmed:
        return " "
    return f" {left_trimmed}"


def _patch_apply_failure_message(message: str) -> str:
    lowered = message.lower()
    if "corrupt patch" in lowered:
        return (
            "Patch structurally invalid after normalization. "
            "Ask the LLM to return only a Git unified diff, without Markdown fences, "
            "and with a leading single space on unchanged lines inside each hunk. "
            f"Git reported: {message}"
        )
    return message
