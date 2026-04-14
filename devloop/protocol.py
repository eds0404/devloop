"""Machine-readable protocol parsing for LLM responses."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import re
from typing import Any, Iterable

from devloop.errors import ProtocolError

COMMAND_START = "<<<DEVLOOP_COMMAND_START>>>"
COMMAND_END = "<<<DEVLOOP_COMMAND_END>>>"
COMMAND_V2_HEADER = "DEVLOOP_COMMAND_V2"
SUPPORTED_COMMANDS = {"COLLECT_CONTEXT", "APPLY_PATCH", "ASK_HUMAN", "DONE"}
ALLOWED_TOP_LEVEL_FIELDS = {
    "version",
    "command",
    "summary_human",
    "next_step_human",
    "task_summary_en",
    "current_goal_en",
    "payload",
}
SUPPORTED_QUERY_TYPES = {
    "project_tree",
    "file_search",
    "path_search",
    "text_search",
    "regex_search",
    "read_file",
    "read_snippet",
    "read_around_match",
    "related_files",
    "related_tests",
}
SUPPORTED_PATCH_FORMAT = "search_replace_v1"
SUPPORTED_PATCH_FORMAT_V2 = "SEARCH_REPLACE_BLOCKS_V1"
SUPPORTED_PATCH_OPERATIONS = {"replace", "create", "delete"}
FILE_BEGIN = "*** BEGIN FILE ***"
FILE_END = "*** END FILE ***"
QUERY_BEGIN = "*** BEGIN QUERY ***"
QUERY_END = "*** END QUERY ***"
RUN_BEGIN = "*** BEGIN REQUESTED_RUN ***"
RUN_END = "*** END REQUESTED_RUN ***"
ARTIFACT_BEGIN = "*** BEGIN EXPECTED_ARTIFACT ***"
ARTIFACT_END = "*** END EXPECTED_ARTIFACT ***"
SEARCH_BEGIN = "@@@SEARCH@@@"
REPLACE_BEGIN = "@@@REPLACE@@@"
CONTENT_BEGIN = "@@@CONTENT@@@"
BLOCK_END = "@@@END@@@"


@dataclass(slots=True)
class ProtocolCommand:
    version: str
    command: str
    summary_human: str
    next_step_human: str
    task_summary_en: str
    current_goal_en: str
    payload: dict[str, Any]

    def to_session_summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "command": self.command,
            "summary_human": self.summary_human,
            "next_step_human": self.next_step_human,
            "task_summary_en": self.task_summary_en,
            "current_goal_en": self.current_goal_en,
            "payload_keys": sorted(self.payload.keys()),
        }

@dataclass(slots=True)
class ProtocolEnvelope:
    human_text: str
    raw_block: str
    command: ProtocolCommand


def extract_command_block(response_text: str) -> str:
    blocks = _extract_command_blocks(response_text)
    if len(blocks) == 1:
        return blocks[0]
    if all(block == blocks[0] for block in blocks[1:]):
        return blocks[0]
    raise ProtocolError("Expected exactly one command block")


def parse_protocol_response(response_text: str) -> ProtocolEnvelope:
    raw_block = extract_command_block(response_text)
    if not _looks_like_v2_protocol_block(raw_block):
        raise ProtocolError(
            "Only DEVLOOP_COMMAND_V2 line-based protocol is supported. "
            "YAML command blocks are no longer accepted."
        )
    parsed = _parse_v2_protocol_block(raw_block)
    command = ProtocolCommand(
        version=_required_string_like(parsed, "version"),
        command=_required_string(parsed, "command"),
        summary_human=_required_string(parsed, "summary_human"),
        next_step_human=_required_string(parsed, "next_step_human"),
        task_summary_en=_required_string(parsed, "task_summary_en"),
        current_goal_en=_required_string(parsed, "current_goal_en"),
        payload=_required_dict(parsed, "payload"),
    )
    _validate_command(command)
    human_text = _strip_command_block(response_text, raw_block)
    return ProtocolEnvelope(human_text=human_text, raw_block=raw_block, command=command)


def _validate_command(command: ProtocolCommand) -> None:
    if command.command not in SUPPORTED_COMMANDS:
        raise ProtocolError(f"Unsupported command: {command.command}")
    if command.command == "COLLECT_CONTEXT":
        _validate_collect_context_payload(command.payload)
    elif command.command == "APPLY_PATCH":
        _validate_apply_patch_payload(command.payload)
    elif command.command in {"ASK_HUMAN", "DONE"} and not isinstance(command.payload, dict):
        raise ProtocolError("payload must be a mapping")


def _validate_collect_context_payload(payload: dict[str, Any]) -> None:
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ProtocolError("COLLECT_CONTEXT payload must contain a non-empty queries list")
    prompt_goal = payload.get("prompt_goal")
    if prompt_goal is not None and not isinstance(prompt_goal, str):
        raise ProtocolError("prompt_goal must be a string when present")
    for query in queries:
        if not isinstance(query, dict):
            raise ProtocolError("Each COLLECT_CONTEXT query must be a mapping")
        query_type = query.get("type")
        if query_type not in SUPPORTED_QUERY_TYPES:
            raise ProtocolError(f"Unsupported query type: {query_type}")


def _validate_apply_patch_payload(payload: dict[str, Any]) -> None:
    patch_format = payload.get("patch_format")
    if patch_format != SUPPORTED_PATCH_FORMAT:
        raise ProtocolError("Only patch_format=search_replace_v1 is supported")

    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ProtocolError("search_replace_v1 payload must contain a non-empty files list")
    for file_entry in files:
        if not isinstance(file_entry, dict):
            raise ProtocolError("Each search_replace_v1 file entry must be a mapping")
        path = file_entry.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ProtocolError("Each search_replace_v1 file entry must define a non-empty path")
        operation = str(file_entry.get("operation", "replace"))
        if operation not in SUPPORTED_PATCH_OPERATIONS:
            raise ProtocolError(
                "Each search_replace_v1 file entry must use operation replace, create, or delete"
            )
        expected_sha256 = file_entry.get("expected_sha256")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not expected_sha256.strip()):
            raise ProtocolError("expected_sha256 must be a non-empty string when present")
        if operation == "replace":
            if "content" in file_entry:
                raise ProtocolError("Replace operations may not contain content")
            replacements = file_entry.get("replacements")
            if not isinstance(replacements, list) or not replacements:
                raise ProtocolError("Each replace operation must contain a non-empty replacements list")
            for replacement in replacements:
                if not isinstance(replacement, dict):
                    raise ProtocolError("Each search_replace_v1 replacement must be a mapping")
                search = replacement.get("search")
                replace = replacement.get("replace")
                expected_matches = replacement.get("expected_matches", 1)
                if not isinstance(search, str) or not search:
                    raise ProtocolError("Each search_replace_v1 replacement must contain a non-empty search string")
                if not isinstance(replace, str):
                    raise ProtocolError("Each search_replace_v1 replacement must contain a replace string")
                try:
                    expected_matches_int = int(expected_matches)
                except (TypeError, ValueError) as exc:
                    raise ProtocolError("expected_matches must be an integer") from exc
                if expected_matches_int <= 0:
                    raise ProtocolError("expected_matches must be positive")
            continue

        if operation == "create":
            if expected_sha256 is not None:
                raise ProtocolError("Create operations may not contain expected_sha256")
            if "replacements" in file_entry:
                raise ProtocolError("Create operations may not contain replacements")
            content = file_entry.get("content")
            if not isinstance(content, str):
                raise ProtocolError("Each create operation must contain a content string")
            continue

        if "replacements" in file_entry:
            raise ProtocolError("Delete operations may not contain replacements")
        if "content" in file_entry:
            raise ProtocolError("Delete operations may not contain content")


def _strip_command_block(response_text: str, raw_block: str) -> str:
    _ = raw_block
    cleaned = _COMMAND_BLOCK_RE.sub("", response_text)
    return cleaned.strip()


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"Field {key} must be a non-empty string")
    return value.strip()


def _required_string_like(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if isinstance(value, (int, float)):
        return str(value)
    return _required_string(data, key)


def _required_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ProtocolError(f"Field {key} must be a mapping")
    return value


def _validate_marker_counts(response_text: str) -> None:
    if response_text.count(COMMAND_START) != 1 or response_text.count(COMMAND_END) != 1:
        raise ProtocolError("Expected exactly one command block")


def _parse_relaxed_scalar(value_text: str) -> Any:
    stripped = value_text.strip()
    if stripped == "":
        return ""
    if stripped in {"true", "false"}:
        return stripped == "true"
    if stripped in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?\d+", stripped):
        return int(stripped)
    if re.fullmatch(r"[-+]?\d+\.\d+", stripped):
        return float(stripped)
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        try:
            return ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            return stripped[1:-1]
    return stripped

_COMMAND_BLOCK_RE = re.compile(
    re.escape(COMMAND_START) + r"(.*?)" + re.escape(COMMAND_END),
    re.DOTALL,
)


def _extract_command_blocks(response_text: str) -> list[str]:
    matches = [match.group(1).strip() for match in _COMMAND_BLOCK_RE.finditer(response_text)]
    if not matches:
        raise ProtocolError("Expected exactly one command block")
    if any(not match for match in matches):
        raise ProtocolError("Command block is empty")
    return matches

def _dedent_relaxed_block(lines: list[str]) -> str:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    common_indent = min(indents) if indents else 0
    return "\n".join(line[common_indent:] if len(line) >= common_indent else "" for line in lines)


def _looks_like_v2_protocol_block(raw_block: str) -> bool:
    lines = [line.strip() for line in raw_block.splitlines() if line.strip()]
    if not lines:
        return False
    if _normalize_v2_token(lines[0]) == COMMAND_V2_HEADER:
        return True
    normalized_lines = [_normalize_v2_token(line) for line in lines[:16]]
    has_command = any(line.startswith("COMMAND:") for line in normalized_lines)
    has_payload = any(line == "PAYLOAD:" for line in normalized_lines)
    has_v2_markers = any(
        line in {FILE_BEGIN, QUERY_BEGIN, RUN_BEGIN, ARTIFACT_BEGIN, SEARCH_BEGIN, REPLACE_BEGIN, CONTENT_BEGIN}
        for line in normalized_lines
    )
    return has_command and has_v2_markers and not has_payload


def _parse_v2_protocol_block(raw_block: str) -> dict[str, Any]:
    lines = raw_block.splitlines()
    index = _skip_blank_lines(lines, 0)
    if index < len(lines) and _normalize_v2_token(lines[index].strip()) == COMMAND_V2_HEADER:
        index += 1

    top_fields: dict[str, Any] = {}
    while index < len(lines):
        index = _skip_blank_lines(lines, index)
        if index >= len(lines):
            break
        stripped = lines[index].strip()
        if _is_v2_section_marker(stripped):
            break
        key, value = _parse_v2_key_value_line(stripped)
        normalized_key = _normalize_v2_key(key)
        if not normalized_key:
            raise ProtocolError(f"Unsupported DEVLOOP_COMMAND_V2 field: {key}")
        top_fields[normalized_key] = _parse_relaxed_scalar(value)
        index += 1

    command = str(top_fields.get("command", "")).strip()
    payload = _parse_v2_payload(command, top_fields, lines, index)
    parsed = {
        "version": str(top_fields.get("version", "")).strip(),
        "command": command,
        "summary_human": str(top_fields.get("summary_human", "")).strip(),
        "next_step_human": str(top_fields.get("next_step_human", "")).strip(),
        "task_summary_en": str(top_fields.get("task_summary_en", "")).strip(),
        "current_goal_en": str(top_fields.get("current_goal_en", "")).strip(),
        "payload": payload,
    }
    return parsed


def _parse_v2_payload(
    command: str,
    top_fields: dict[str, Any],
    lines: list[str],
    index: int,
) -> dict[str, Any]:
    if command == "DONE":
        _ensure_only_blank_tail(lines, index)
        return {}
    if command == "ASK_HUMAN":
        return _parse_v2_ask_human_payload(lines, index)
    if command == "COLLECT_CONTEXT":
        return _parse_v2_collect_context_payload(top_fields, lines, index)
    if command == "APPLY_PATCH":
        return _parse_v2_apply_patch_payload(top_fields, lines, index)
    raise ProtocolError(f"Unsupported command: {command}")


def _parse_v2_ask_human_payload(lines: list[str], index: int) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    artifacts: list[str] = []
    while True:
        index = _skip_blank_lines(lines, index)
        if index >= len(lines):
            break
        stripped = lines[index].strip()
        normalized = _normalize_v2_token(stripped)
        if normalized == RUN_BEGIN:
            section_lines, index = _collect_v2_section(lines, index + 1, RUN_END)
            runs.append(_parse_v2_mapping_section(section_lines))
            continue
        if normalized == ARTIFACT_BEGIN:
            section_lines, index = _collect_v2_section(lines, index + 1, ARTIFACT_END)
            artifacts.append(_parse_v2_text_section(section_lines))
            continue
        raise ProtocolError(f"Unexpected DEVLOOP_COMMAND_V2 ASK_HUMAN payload line: {stripped}")
    payload: dict[str, Any] = {}
    if runs:
        payload["requested_runs"] = runs
    if artifacts:
        payload["expected_artifacts_from_human"] = artifacts
    return payload


def _parse_v2_collect_context_payload(
    top_fields: dict[str, Any],
    lines: list[str],
    index: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    prompt_goal = top_fields.get("prompt_goal")
    if isinstance(prompt_goal, str) and prompt_goal.strip():
        payload["prompt_goal"] = prompt_goal.strip()

    queries: list[dict[str, Any]] = []
    while True:
        index = _skip_blank_lines(lines, index)
        if index >= len(lines):
            break
        stripped = lines[index].strip()
        if _normalize_v2_token(stripped) != QUERY_BEGIN:
            raise ProtocolError(f"Unexpected DEVLOOP_COMMAND_V2 COLLECT_CONTEXT payload line: {stripped}")
        section_lines, index = _collect_v2_section(lines, index + 1, QUERY_END)
        queries.append(_parse_v2_mapping_section(section_lines))
    payload["queries"] = queries
    return payload


def _parse_v2_apply_patch_payload(
    top_fields: dict[str, Any],
    lines: list[str],
    index: int,
) -> dict[str, Any]:
    patch_format = _normalize_v2_token(str(top_fields.get("patch_format", "")).strip())
    if patch_format and patch_format != SUPPORTED_PATCH_FORMAT_V2:
        raise ProtocolError(
            f"Unsupported DEVLOOP_COMMAND_V2 PATCH_FORMAT: {patch_format}. "
            f"Expected {SUPPORTED_PATCH_FORMAT_V2}."
        )

    files: list[dict[str, Any]] = []
    while True:
        index = _skip_blank_lines(lines, index)
        if index >= len(lines):
            break
        stripped = lines[index].strip()
        if _normalize_v2_token(stripped) != FILE_BEGIN:
            raise ProtocolError(f"Unexpected DEVLOOP_COMMAND_V2 APPLY_PATCH payload line: {stripped}")
        file_entry, index = _parse_v2_file_section(lines, index + 1)
        files.append(file_entry)
    return {
        "patch_format": SUPPORTED_PATCH_FORMAT,
        "files": files,
    }


def _parse_v2_file_section(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    path: str | None = None
    operation = "replace"
    expected_sha256: str | None = None
    replacements: list[dict[str, Any]] = []
    content: str | None = None
    pending_match_count = 1

    while True:
        index = _skip_blank_lines(lines, index)
        if index >= len(lines):
            raise ProtocolError(f"Missing {FILE_END} in DEVLOOP_COMMAND_V2 APPLY_PATCH payload")
        stripped = lines[index].strip()
        normalized = _normalize_v2_token(stripped)
        if normalized == FILE_END:
            index += 1
            break
        if normalized == SEARCH_BEGIN:
            search_text, index = _collect_v2_block(lines, index + 1, REPLACE_BEGIN)
            replace_text, index = _collect_v2_block(lines, index, BLOCK_END)
            replacements.append(
                {
                    "search": search_text,
                    "replace": replace_text,
                    "expected_matches": pending_match_count,
                }
            )
            pending_match_count = 1
            continue
        if normalized == CONTENT_BEGIN:
            content, index = _collect_v2_block(lines, index + 1, BLOCK_END)
            continue

        key, value = _parse_v2_key_value_line(stripped)
        if key == "PATH":
            path = str(_parse_relaxed_scalar(value))
        elif key in {"OP", "OPERATION"}:
            operation = _normalize_v2_file_operation(value)
        elif key in {"EXPECTED_SHA256", "HASH"}:
            expected_sha256 = str(_parse_relaxed_scalar(value)).removeprefix("sha256:").strip()
        elif key in {"MATCH_COUNT", "EXPECTED_MATCHES"}:
            pending_match_count = int(_parse_relaxed_scalar(value))
        else:
            raise ProtocolError(f"Unsupported DEVLOOP_COMMAND_V2 APPLY_PATCH file field: {key}")
        index += 1

    if not path:
        raise ProtocolError("Each DEVLOOP_COMMAND_V2 file section must define PATH")

    file_entry: dict[str, Any] = {
        "path": path,
        "operation": operation,
    }
    if expected_sha256:
        file_entry["expected_sha256"] = expected_sha256
    if operation == "replace":
        file_entry["replacements"] = replacements
    elif operation == "create":
        file_entry["content"] = content or ""
    return file_entry, index


def _parse_v2_mapping_section(lines: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        key, value = _parse_v2_key_value_line(stripped)
        normalized_key = _normalize_v2_nested_key(key)
        parsed[normalized_key] = _parse_relaxed_scalar(value)
    return parsed


def _parse_v2_text_section(lines: list[str]) -> str:
    text = "\n".join(line.rstrip() for line in lines).strip()
    if _normalize_v2_token(text).startswith("TEXT:"):
        return text.split(":", 1)[1].strip()
    return text


def _collect_v2_section(lines: list[str], index: int, end_marker: str) -> tuple[list[str], int]:
    collected: list[str] = []
    normalized_end_marker = _normalize_v2_token(end_marker)
    while index < len(lines):
        stripped = lines[index].strip()
        if _normalize_v2_token(stripped) == normalized_end_marker:
            return collected, index + 1
        collected.append(lines[index])
        index += 1
    raise ProtocolError(f"Missing {end_marker} in DEVLOOP_COMMAND_V2 payload")


def _collect_v2_block(lines: list[str], index: int, end_marker: str) -> tuple[str, int]:
    collected: list[str] = []
    normalized_end_marker = _normalize_v2_token(end_marker)
    while index < len(lines):
        stripped = lines[index].strip()
        if _normalize_v2_token(stripped) == normalized_end_marker:
            return _dedent_relaxed_block(collected), index + 1
        collected.append(lines[index])
        index += 1
    raise ProtocolError(f"Missing {end_marker} in DEVLOOP_COMMAND_V2 block payload")


def _normalize_v2_file_operation(value: str) -> str:
    normalized = str(_parse_relaxed_scalar(value)).strip().upper()
    mapping = {
        "REPLACE": "replace",
        "CREATE": "create",
        "CREATE_FILE": "create",
        "DELETE": "delete",
        "DELETE_FILE": "delete",
    }
    if normalized not in mapping:
        raise ProtocolError(f"Unsupported DEVLOOP_COMMAND_V2 file operation: {value}")
    return mapping[normalized]


def _normalize_v2_key(key: str) -> str:
    mapping = {
        "VERSION": "version",
        "COMMAND": "command",
        "SUMMARY_HUMAN": "summary_human",
        "NEXT_STEP_HUMAN": "next_step_human",
        "TASK_SUMMARY_EN": "task_summary_en",
        "CURRENT_GOAL_EN": "current_goal_en",
        "PROMPT_GOAL": "prompt_goal",
        "PATCH_FORMAT": "patch_format",
    }
    return mapping.get(key, "")


def _normalize_v2_nested_key(key: str) -> str:
    mapping = {
        "TYPE": "type",
        "QUERY": "query",
        "GLOB": "glob",
        "LIMIT": "limit",
        "FILE": "file",
        "START_LINE": "start_line",
        "END_LINE": "end_line",
        "BEFORE": "before",
        "AFTER": "after",
        "KIND": "kind",
        "PURPOSE": "purpose",
        "COMMAND_EXAMPLE": "command_example",
    }
    return mapping.get(key, key.lower())


def _parse_v2_key_value_line(stripped: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", stripped)
    if not match:
        raise ProtocolError(f"Expected DEVLOOP_COMMAND_V2 key/value line, got: {stripped}")
    return match.group(1).upper(), match.group(2)


def _skip_blank_lines(lines: list[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _ensure_only_blank_tail(lines: list[str], index: int) -> None:
    index = _skip_blank_lines(lines, index)
    if index != len(lines):
        raise ProtocolError("Unexpected trailing content after DEVLOOP_COMMAND_V2 payload")


def _is_v2_section_marker(stripped: str) -> bool:
    return _normalize_v2_token(stripped) in {FILE_BEGIN, QUERY_BEGIN, RUN_BEGIN, ARTIFACT_BEGIN}


def _normalize_v2_token(value: str) -> str:
    return value.strip().upper()
