"""Safe Git-aware structured patch validation and application."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath

from devloop.errors import PatchApplyError
from devloop.git_tools import list_dirty_paths, run_git, summarize_paths_status


@dataclass(slots=True)
class PatchApplyResult:
    affected_files: list[str]
    git_status_summary: str


@dataclass(slots=True)
class SearchReplaceOp:
    search: str
    replace: str
    expected_matches: int


@dataclass(slots=True)
class SearchReplaceFilePlan:
    path: PurePosixPath
    operation: str
    expected_sha256: str | None
    replacements: list[SearchReplaceOp]
    content: str | None = None


TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp1251", "cp1252", "latin-1")
ALLOWED_FILE_OPERATIONS = {"replace", "create", "delete"}


def apply_patch_payload(
    repo_root: Path,
    state_dir: Path,
    payload: dict[str, object],
    *,
    allow_apply_on_dirty_files: bool,
) -> PatchApplyResult:
    _ = state_dir
    patch_format = str(payload.get("patch_format", ""))
    if patch_format != "search_replace_v1":
        raise PatchApplyError("Only patch_format=search_replace_v1 is supported")
    return apply_search_replace_patch(
        repo_root,
        payload,
        allow_apply_on_dirty_files=allow_apply_on_dirty_files,
    )


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

def apply_search_replace_patch(
    repo_root: Path,
    payload: dict[str, object],
    *,
    allow_apply_on_dirty_files: bool,
) -> PatchApplyResult:
    plans = _parse_search_replace_payload(payload)
    repo_relative_paths = [Path(*plan.path.parts) for plan in plans]
    if not allow_apply_on_dirty_files:
        dirty = list_dirty_paths(repo_root, repo_relative_paths)
        if dirty:
            dirty_text = ", ".join(sorted(dirty))
            raise PatchApplyError(
                f"Refusing to apply patch because affected files are dirty: {dirty_text}"
            )

    writes: list[tuple[Path, str, str]] = []
    add_paths: list[Path] = []
    delete_paths: list[Path] = []

    try:
        for plan in plans:
            repo_relative_path = Path(*plan.path.parts)
            file_path = repo_root / repo_relative_path

            if plan.operation == "create":
                if file_path.exists():
                    raise PatchApplyError(
                        f"search_replace_v1 create target already exists: {plan.path.as_posix()}"
                    )
                writes.append((file_path, plan.content or "", "utf-8"))
                add_paths.append(repo_relative_path)
                continue

            if not file_path.exists() or not file_path.is_file():
                raise PatchApplyError(
                    f"search_replace_v1 path does not exist or is not a file: {plan.path.as_posix()}"
                )

            raw_bytes = file_path.read_bytes()
            if plan.expected_sha256:
                actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                if actual_sha256 != plan.expected_sha256:
                    raise PatchApplyError(
                        f"search_replace_v1 sha256 mismatch for {plan.path.as_posix()}: "
                        f"expected {plan.expected_sha256}, got {actual_sha256}"
                    )

            if plan.operation == "delete":
                delete_paths.append(repo_relative_path)
                continue

            original_text, encoding = _decode_text_with_fallback(file_path, raw_bytes)
            new_text = _apply_exact_replacements(original_text, plan)
            writes.append((file_path, new_text, encoding))
            add_paths.append(repo_relative_path)

        for file_path, new_text, encoding in writes:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(new_text.encode(encoding))

        if delete_paths:
            run_git(repo_root, ["rm", "--", *[str(path) for path in delete_paths]])
        if add_paths:
            run_git(repo_root, ["add", "--", *[str(path) for path in add_paths]])

        status_summary = summarize_paths_status(repo_root, repo_relative_paths)
        return PatchApplyResult(
            affected_files=[plan.path.as_posix() for plan in plans],
            git_status_summary=status_summary,
        )
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, PatchApplyError):
            raise
        raise PatchApplyError(str(exc)) from exc


def _parse_search_replace_payload(payload: dict[str, object]) -> list[SearchReplaceFilePlan]:
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise PatchApplyError("search_replace_v1 payload must contain a non-empty files list")
    plans: list[SearchReplaceFilePlan] = []
    seen_paths: set[str] = set()
    for file_entry in files:
        if not isinstance(file_entry, dict):
            raise PatchApplyError("Each search_replace_v1 file entry must be a mapping")
        path_text = str(file_entry["path"])
        path = validate_repo_relative_path(PurePosixPath(path_text.replace("\\", "/")))
        key = path.as_posix()
        if key in seen_paths:
            raise PatchApplyError(f"search_replace_v1 touches the same path more than once: {key}")
        seen_paths.add(key)
        operation = str(file_entry.get("operation", "replace"))
        if operation not in ALLOWED_FILE_OPERATIONS:
            raise PatchApplyError(
                "Each search_replace_v1 file entry must use operation replace, create, or delete"
            )
        expected_sha256 = file_entry.get("expected_sha256")
        replacements: list[SearchReplaceOp] = []
        content: str | None = None

        if operation == "replace":
            replacements_raw = file_entry.get("replacements")
            if not isinstance(replacements_raw, list) or not replacements_raw:
                raise PatchApplyError("Each replace operation must contain a non-empty replacements list")
            for replacement_raw in replacements_raw:
                if not isinstance(replacement_raw, dict):
                    raise PatchApplyError("Each search_replace_v1 replacement must be a mapping")
                search = replacement_raw.get("search")
                replace = replacement_raw.get("replace")
                if not isinstance(search, str) or not search:
                    raise PatchApplyError("Each search_replace_v1 replacement must contain a non-empty search string")
                if not isinstance(replace, str):
                    raise PatchApplyError("Each search_replace_v1 replacement must contain a replace string")
                expected_matches = int(replacement_raw.get("expected_matches", 1))
                if expected_matches <= 0:
                    raise PatchApplyError("search_replace_v1 expected_matches must be positive")
                replacements.append(
                    SearchReplaceOp(
                        search=search,
                        replace=replace,
                        expected_matches=expected_matches,
                    )
                )
        elif operation == "create":
            if expected_sha256 is not None:
                raise PatchApplyError("Create operations may not contain expected_sha256")
            if "replacements" in file_entry:
                raise PatchApplyError("Create operations may not contain replacements")
            raw_content = file_entry.get("content")
            if not isinstance(raw_content, str):
                raise PatchApplyError("Each create operation must contain a content string")
            content = raw_content
        else:
            if "replacements" in file_entry:
                raise PatchApplyError("Delete operations may not contain replacements")
            if "content" in file_entry:
                raise PatchApplyError("Delete operations may not contain content")

        plans.append(
            SearchReplaceFilePlan(
                path=path,
                operation=operation,
                expected_sha256=str(expected_sha256).strip() if expected_sha256 is not None else None,
                replacements=replacements,
                content=content,
            )
        )
    return plans


def _apply_exact_replacements(original_text: str, plan: SearchReplaceFilePlan) -> str:
    normalized_text = _normalize_text_newlines(original_text)
    current_text = normalized_text
    for replacement in plan.replacements:
        search = _normalize_text_newlines(replacement.search)
        replace = _normalize_text_newlines(replacement.replace)
        found = current_text.count(search)
        if found != replacement.expected_matches:
            raise PatchApplyError(
                f"search_replace_v1 expected {replacement.expected_matches} match(es) in "
                f"{plan.path.as_posix()}, found {found}"
            )
        current_text = current_text.replace(search, replace)
    if current_text == normalized_text:
        raise PatchApplyError(f"search_replace_v1 produced no changes for {plan.path.as_posix()}")
    newline = _detect_newline_style(original_text)
    return current_text.replace("\n", newline)


def _decode_text_with_fallback(path: Path, raw: bytes) -> tuple[str, str]:
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise PatchApplyError(f"Could not decode file as text: {path}")


def _detect_newline_style(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _normalize_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
