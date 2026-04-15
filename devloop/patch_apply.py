"""Safe Git-aware structured patch validation and application."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil

from devloop.errors import GitError, PatchApplyError, PatchInfrastructureError
from devloop.git_tools import list_dirty_paths, run_git, summarize_paths_status


@dataclass(slots=True)
class PatchApplyResult:
    affected_files: list[str]
    git_status_summary: str
    warning: str = ""
    file_results: list["AppliedFileResult"] = field(default_factory=list)
    fallbacks_used: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReplacementResult:
    expected_matches: int
    found_matches: int
    matched_line_numbers: list[int]


@dataclass(slots=True)
class AppliedFileResult:
    path: str
    operation: str
    expected_sha256: str | None
    before_sha256: str | None
    after_sha256: str | None
    replacement_results: list[ReplacementResult] = field(default_factory=list)


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


@dataclass(slots=True)
class OriginalFileState:
    existed: bool
    raw_bytes: bytes | None


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
        raise PatchApplyError("Only patch_format=search_replace_v1 is supported", stage="patch_format_validate")
    return apply_search_replace_patch(
        repo_root,
        payload,
        allow_apply_on_dirty_files=allow_apply_on_dirty_files,
    )


def validate_repo_relative_path(path: PurePosixPath) -> PurePosixPath:
    if path.is_absolute():
        raise PatchApplyError(f"Patch path must be repository-relative: {path}", stage="path_validate")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PatchApplyError(f"Unsafe patch path: {path}", stage="path_validate")
    if any(":" in part for part in path.parts):
        raise PatchApplyError(f"Unsafe patch path: {path}", stage="path_validate")
    if path.parts and path.parts[0] == ".git":
        raise PatchApplyError("Patches may not touch .git internals", stage="path_validate")
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
    original_states: dict[Path, OriginalFileState] = {}
    file_results: list[AppliedFileResult] = []
    fallbacks_used: list[str] = []
    mutations_started = False

    try:
        for plan in plans:
            repo_relative_path = Path(*plan.path.parts)
            file_path = repo_root / repo_relative_path

            if plan.operation == "create":
                if file_path.exists():
                    raise PatchApplyError(
                        f"search_replace_v1 create target already exists: {plan.path.as_posix()}",
                        stage="path_state_check",
                        details={"path": plan.path.as_posix(), "operation": plan.operation},
                    )
                original_states[repo_relative_path] = OriginalFileState(existed=False, raw_bytes=None)
                writes.append((file_path, plan.content or "", "utf-8"))
                add_paths.append(repo_relative_path)
                file_results.append(
                    AppliedFileResult(
                        path=plan.path.as_posix(),
                        operation=plan.operation,
                        expected_sha256=plan.expected_sha256,
                        before_sha256=None,
                        after_sha256=_compute_sha256((plan.content or "").encode("utf-8")),
                    )
                )
                continue

            if not file_path.exists() or not file_path.is_file():
                raise PatchApplyError(
                    f"search_replace_v1 path does not exist or is not a file: {plan.path.as_posix()}",
                    stage="path_state_check",
                    details={"path": plan.path.as_posix(), "operation": plan.operation},
                )

            raw_bytes = file_path.read_bytes()
            original_states[repo_relative_path] = OriginalFileState(existed=True, raw_bytes=raw_bytes)
            if plan.expected_sha256:
                actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                if actual_sha256 != plan.expected_sha256:
                    raise PatchApplyError(
                        f"search_replace_v1 sha256 mismatch for {plan.path.as_posix()}: "
                        f"expected {plan.expected_sha256}, got {actual_sha256}",
                        stage="sha_check",
                        details={
                            "path": plan.path.as_posix(),
                            "expected_sha256": plan.expected_sha256,
                            "actual_sha256": actual_sha256,
                        },
                    )

            if plan.operation == "delete":
                delete_paths.append(repo_relative_path)
                file_results.append(
                    AppliedFileResult(
                        path=plan.path.as_posix(),
                        operation=plan.operation,
                        expected_sha256=plan.expected_sha256,
                        before_sha256=_compute_sha256(raw_bytes),
                        after_sha256=None,
                    )
                )
                continue

            original_text, encoding = _decode_text_with_fallback(file_path, raw_bytes)
            new_text, replacement_results = _apply_exact_replacements(original_text, plan)
            writes.append((file_path, new_text, encoding))
            add_paths.append(repo_relative_path)
            file_results.append(
                AppliedFileResult(
                    path=plan.path.as_posix(),
                    operation=plan.operation,
                    expected_sha256=plan.expected_sha256,
                    before_sha256=_compute_sha256(raw_bytes),
                    after_sha256=_compute_sha256(new_text.encode(encoding)),
                    replacement_results=replacement_results,
                )
            )

        for file_path, new_text, encoding in writes:
            mutations_started = True
            file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                file_path.write_bytes(new_text.encode(encoding))
            except OSError as exc:
                raise PatchInfrastructureError(
                    f"Failed to write file {file_path}: {exc}",
                    stage="write_file",
                    details={"path": str(file_path)},
                ) from exc

        for path in delete_paths:
            mutations_started = True
            try:
                run_git(repo_root, ["rm", "--", str(path)])
            except GitError as exc:
                raise PatchInfrastructureError(
                    str(exc),
                    stage="git_remove",
                    details={"path": str(path)},
                ) from exc
        for path in add_paths:
            mutations_started = True
            try:
                run_git(repo_root, ["add", "--", str(path)])
            except Exception as exc:  # noqa: BLE001
                if _can_continue_without_git_staging(plans, exc):
                    status_summary = _safe_status_summary(repo_root, repo_relative_paths)
                    fallbacks_used.append("git_stage_skipped")
                    return PatchApplyResult(
                        affected_files=[plan.path.as_posix() for plan in plans],
                        git_status_summary=status_summary,
                        warning=f"Git staging skipped locally: {exc}",
                        file_results=file_results,
                        fallbacks_used=fallbacks_used,
                    )
                raise PatchInfrastructureError(
                    str(exc),
                    stage="git_stage",
                    details={"path": str(path)},
                ) from exc

        status_summary = summarize_paths_status(repo_root, repo_relative_paths)
        return PatchApplyResult(
            affected_files=[plan.path.as_posix() for plan in plans],
            git_status_summary=status_summary,
            file_results=file_results,
            fallbacks_used=fallbacks_used,
        )
    except Exception as exc:  # noqa: BLE001
        rollback_note = ""
        if mutations_started:
            rollback_note = _rollback_search_replace_changes(repo_root, original_states)
        if isinstance(exc, PatchApplyError):
            if rollback_note:
                message = f"{exc} ({rollback_note})"
                if isinstance(exc, PatchInfrastructureError):
                    raise PatchInfrastructureError(message, stage=exc.stage, details=exc.details) from exc
                raise PatchApplyError(message, stage=exc.stage, details=exc.details) from exc
            raise
        message = str(exc)
        if rollback_note:
            message = f"{message} ({rollback_note})"
        if mutations_started or isinstance(exc, (GitError, OSError)):
            raise PatchInfrastructureError(message, stage="infrastructure") from exc
        raise PatchApplyError(message, stage="payload_validate") from exc


def _parse_search_replace_payload(payload: dict[str, object]) -> list[SearchReplaceFilePlan]:
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise PatchApplyError("search_replace_v1 payload must contain a non-empty files list", stage="payload_validate")
    plans: list[SearchReplaceFilePlan] = []
    seen_paths: set[str] = set()
    for file_entry in files:
        if not isinstance(file_entry, dict):
            raise PatchApplyError("Each search_replace_v1 file entry must be a mapping", stage="payload_validate")
        path_text = str(file_entry["path"])
        path = validate_repo_relative_path(PurePosixPath(path_text.replace("\\", "/")))
        key = path.as_posix()
        if key in seen_paths:
            raise PatchApplyError(
                f"search_replace_v1 touches the same path more than once: {key}",
                stage="payload_validate",
            )
        seen_paths.add(key)
        operation = str(file_entry.get("operation", "replace"))
        if operation not in ALLOWED_FILE_OPERATIONS:
            raise PatchApplyError(
                "Each search_replace_v1 file entry must use operation replace, create, or delete",
                stage="payload_validate",
            )
        expected_sha256 = file_entry.get("expected_sha256")
        replacements: list[SearchReplaceOp] = []
        content: str | None = None

        if operation == "replace":
            replacements_raw = file_entry.get("replacements")
            if not isinstance(replacements_raw, list) or not replacements_raw:
                raise PatchApplyError(
                    "Each replace operation must contain a non-empty replacements list",
                    stage="payload_validate",
                )
            for replacement_raw in replacements_raw:
                if not isinstance(replacement_raw, dict):
                    raise PatchApplyError("Each search_replace_v1 replacement must be a mapping", stage="payload_validate")
                search = replacement_raw.get("search")
                replace = replacement_raw.get("replace")
                if not isinstance(search, str) or not search:
                    raise PatchApplyError(
                        "Each search_replace_v1 replacement must contain a non-empty search string",
                        stage="payload_validate",
                    )
                if not isinstance(replace, str):
                    raise PatchApplyError(
                        "Each search_replace_v1 replacement must contain a replace string",
                        stage="payload_validate",
                    )
                expected_matches = int(replacement_raw.get("expected_matches", 1))
                if expected_matches <= 0:
                    raise PatchApplyError("search_replace_v1 expected_matches must be positive", stage="payload_validate")
                replacements.append(
                    SearchReplaceOp(
                        search=search,
                        replace=replace,
                        expected_matches=expected_matches,
                    )
                )
        elif operation == "create":
            if expected_sha256 is not None:
                raise PatchApplyError("Create operations may not contain expected_sha256", stage="payload_validate")
            if "replacements" in file_entry:
                raise PatchApplyError("Create operations may not contain replacements", stage="payload_validate")
            raw_content = file_entry.get("content")
            if not isinstance(raw_content, str):
                raise PatchApplyError("Each create operation must contain a content string", stage="payload_validate")
            content = raw_content
        else:
            if "replacements" in file_entry:
                raise PatchApplyError("Delete operations may not contain replacements", stage="payload_validate")
            if "content" in file_entry:
                raise PatchApplyError("Delete operations may not contain content", stage="payload_validate")

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


def _apply_exact_replacements(original_text: str, plan: SearchReplaceFilePlan) -> tuple[str, list[ReplacementResult]]:
    normalized_text = _normalize_text_newlines(original_text)
    current_text = normalized_text
    replacement_results: list[ReplacementResult] = []
    for replacement in plan.replacements:
        search = _normalize_text_newlines(replacement.search)
        replace = _normalize_text_newlines(replacement.replace)
        found = current_text.count(search)
        matched_line_numbers = _find_match_line_numbers(current_text, search)
        if found != replacement.expected_matches:
            raise PatchApplyError(
                f"search_replace_v1 expected {replacement.expected_matches} match(es) in "
                f"{plan.path.as_posix()}, found {found}",
                stage="match_count_check",
                details={
                    "path": plan.path.as_posix(),
                    "expected_matches": replacement.expected_matches,
                    "found_matches": found,
                    "matched_line_numbers": matched_line_numbers,
                },
            )
        replacement_results.append(
            ReplacementResult(
                expected_matches=replacement.expected_matches,
                found_matches=found,
                matched_line_numbers=matched_line_numbers,
            )
        )
        current_text = current_text.replace(search, replace)
    if current_text == normalized_text:
        raise PatchApplyError(
            f"search_replace_v1 produced no changes for {plan.path.as_posix()}",
            stage="no_change_check",
            details={"path": plan.path.as_posix()},
        )
    newline = _detect_newline_style(original_text)
    return current_text.replace("\n", newline), replacement_results


def _decode_text_with_fallback(path: Path, raw: bytes) -> tuple[str, str]:
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise PatchApplyError(f"Could not decode file as text: {path}", stage="decode_text", details={"path": str(path)})


def _detect_newline_style(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _normalize_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _find_match_line_numbers(text: str, search: str) -> list[int]:
    if not search:
        return []
    line_numbers: list[int] = []
    start = 0
    while True:
        match_index = text.find(search, start)
        if match_index < 0:
            break
        line_numbers.append(text.count("\n", 0, match_index) + 1)
        start = match_index + len(search)
    return line_numbers


def _compute_sha256(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _rollback_search_replace_changes(
    repo_root: Path,
    original_states: dict[Path, OriginalFileState],
) -> str:
    try:
        for repo_relative_path, state in original_states.items():
            file_path = repo_root / repo_relative_path
            if state.existed:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(state.raw_bytes or b"")
            elif file_path.exists():
                if file_path.is_dir():
                    shutil.rmtree(file_path, ignore_errors=False)
                else:
                    os.chmod(file_path, 0o666)
                    file_path.unlink()

        for repo_relative_path, state in original_states.items():
            if state.existed:
                run_git(repo_root, ["add", "--", str(repo_relative_path)])
            else:
                run_git(repo_root, ["rm", "--cached", "--ignore-unmatch", "--", str(repo_relative_path)])
        return "rollback restored the original local file state"
    except Exception as rollback_exc:  # noqa: BLE001
        return f"rollback failed: {rollback_exc}"


def _can_continue_without_git_staging(
    plans: list[SearchReplaceFilePlan],
    exc: Exception,
) -> bool:
    if not plans or any(plan.operation != "replace" for plan in plans):
        return False
    message = str(exc).lower()
    return "index.lock" in message or "permission denied" in message or "access is denied" in message


def _safe_status_summary(repo_root: Path, repo_relative_paths: list[Path]) -> str:
    try:
        return summarize_paths_status(repo_root, repo_relative_paths)
    except Exception:  # noqa: BLE001
        return "\n".join(f"M  {path.as_posix()}" for path in repo_relative_paths)
