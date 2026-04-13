"""Machine-readable protocol parsing for LLM responses."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import re
from typing import Any, Iterable

from devloop.errors import ProtocolError
from devloop import yaml_compat as yaml

COMMAND_START = "<<<DEVLOOP_COMMAND_START>>>"
COMMAND_END = "<<<DEVLOOP_COMMAND_END>>>"
SUPPORTED_COMMANDS = {"COLLECT_CONTEXT", "APPLY_PATCH", "ASK_HUMAN", "DONE"}
ALLOWED_TOP_LEVEL_FIELDS = {
    "version",
    "command",
    "summary_human",
    "next_step_human",
    "task_summary_en",
    "current_goal_en",
    "payload",
    "summary_ru",
    "next_step_ru",
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
SUPPORTED_PATCH_OPERATIONS = {"replace", "create", "delete"}


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

    @property
    def summary_ru(self) -> str:
        return self.summary_human

    @property
    def next_step_ru(self) -> str:
        return self.next_step_human


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
    try:
        parsed = yaml.safe_load(raw_block)
    except yaml.YAMLError as exc:
        fallback_parsed = _try_parse_relaxed_protocol_block(raw_block)
        if fallback_parsed is not None:
            parsed = fallback_parsed
        else:
            hint = _build_yaml_parse_hint(raw_block)
            if hint:
                raise ProtocolError(f"Failed to parse command block YAML: {exc}. Hint: {hint}") from exc
            raise ProtocolError(f"Failed to parse command block YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("Command block must be a mapping")
    parsed = _normalize_top_level_payload_fields(parsed)
    _validate_top_level_fields(parsed)

    command = ProtocolCommand(
        version=_required_string_like(parsed, "version"),
        command=_required_string(parsed, "command"),
        summary_human=_required_string_with_alias(parsed, "summary_human", "summary_ru"),
        next_step_human=_required_string_with_alias(parsed, "next_step_human", "next_step_ru"),
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


def _validate_top_level_fields(parsed: dict[str, Any]) -> None:
    unexpected = sorted(key for key in parsed.keys() if key not in ALLOWED_TOP_LEVEL_FIELDS)
    if not unexpected:
        return
    extras = ", ".join(unexpected)
    raise ProtocolError(
        "Unexpected top-level fields: "
        f"{extras}. This usually means nested payload fields were not indented under `payload:`."
    )


def _normalize_top_level_payload_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        return parsed
    unexpected = [key for key in list(parsed.keys()) if key not in ALLOWED_TOP_LEVEL_FIELDS]
    if not unexpected:
        return parsed
    normalized = dict(parsed)
    normalized_payload = dict(payload)
    for key in unexpected:
        normalized_payload[key] = normalized.pop(key)
    normalized["payload"] = normalized_payload
    return normalized


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


def _required_string_with_alias(data: dict[str, Any], key: str, alias: str) -> str:
    value = data.get(key)
    if value is None:
        value = data.get(alias)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"Field {key} must be a non-empty string")
    return value.strip()


def _validate_marker_counts(response_text: str) -> None:
    if response_text.count(COMMAND_START) != 1 or response_text.count(COMMAND_END) != 1:
        raise ProtocolError("Expected exactly one command block")


def _build_yaml_parse_hint(raw_block: str) -> str:
    if "\t" in raw_block:
        return "Use spaces only, not tabs, inside the command block."

    lines = raw_block.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "payload:":
            next_line = _next_non_empty_line(lines, index + 1)
            if next_line is not None:
                next_indent, next_text = next_line
                if next_indent <= indent:
                    return (
                        "All fields inside `payload:` must be indented by two spaces. "
                        "Example: `payload:` then `  requested_runs:`."
                    )
                if next_text.startswith("- "):
                    return (
                        "Do not start a list directly under `payload:` without a field name. "
                        "Use `payload:` then an indented key such as `  requested_runs:`."
                    )
        if stripped.endswith(":"):
            key = stripped[:-1].strip()
            next_line = _next_non_empty_line(lines, index + 1)
            if next_line is None:
                continue
            next_indent, next_text = next_line
            if next_text.startswith("- ") and next_indent <= indent:
                return (
                    f"List items under `{key}:` must be indented by two spaces. "
                    f"Example: `{key}:` then `  - item`."
                )
    return ""


def _try_parse_relaxed_protocol_block(raw_block: str) -> dict[str, Any] | None:
    parsed = _extract_relaxed_top_level_fields(raw_block)
    if parsed is None:
        return None
    command = parsed.get("command")
    if command in {"ASK_HUMAN", "DONE"}:
        payload_text = parsed.pop("_payload_text", "").strip()
        parsed["payload"] = {"raw_payload_text": payload_text} if payload_text else {}
        return parsed
    if command == "APPLY_PATCH":
        payload_text = parsed.pop("_payload_text", "")
        payload = _parse_relaxed_apply_patch_payload(payload_text)
        if payload is None:
            return None
        parsed["payload"] = payload
        return parsed
    return None


def _extract_relaxed_top_level_fields(raw_block: str) -> dict[str, Any] | None:
    lines = raw_block.splitlines()
    parsed: dict[str, Any] = {}
    payload_lines: list[str] = []
    in_payload = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if not in_payload:
            if not stripped:
                continue
            if stripped == "payload:":
                in_payload = True
                continue
            match = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", stripped)
            if not match:
                return None
            key = match.group(1)
            value_text = match.group(2)
            parsed[key] = _parse_relaxed_scalar(value_text)
            continue
        payload_lines.append(raw_line)

    if "payload" in parsed:
        return None
    if "command" not in parsed:
        return None
    parsed["_payload_text"] = "\n".join(payload_lines).rstrip()
    return parsed


def _parse_relaxed_apply_patch_payload(payload_text: str) -> dict[str, Any] | None:
    normalized_payload = _normalize_relaxed_search_replace_payload(payload_text)
    if normalized_payload:
        parsed = _try_parse_relaxed_payload_mapping(normalized_payload)
        if parsed is not None:
            return parsed

    parsed = _try_parse_relaxed_payload_mapping(payload_text)
    if parsed is not None:
        return parsed
    parsed = _manual_parse_relaxed_search_replace_payload(payload_text)
    if parsed is not None:
        return parsed
    return None


def _try_parse_relaxed_payload_mapping(payload_text: str) -> dict[str, Any] | None:
    if not payload_text.strip():
        return None
    try:
        parsed = yaml.safe_load(payload_text)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_relaxed_search_replace_payload(payload_text: str) -> str:
    normalized_lines: list[str] = []
    block_indent: int | None = None
    state = "top"

    for raw_line in payload_text.splitlines():
        stripped = raw_line.strip()
        if block_indent is not None:
            if stripped and _is_search_replace_control_line(stripped):
                block_indent = None
            else:
                if not stripped:
                    normalized_lines.append("")
                else:
                    current_indent = len(raw_line) - len(raw_line.lstrip(" "))
                    if current_indent < block_indent:
                        normalized_lines.append((" " * (block_indent - current_indent)) + raw_line)
                    else:
                        normalized_lines.append(raw_line)
                continue

        if not stripped:
            normalized_lines.append("")
            continue
        if stripped.startswith("patch_format:"):
            normalized_lines.append(f"patch_format: {stripped.split(':', 1)[1].strip()}")
            state = "top"
            continue
        if stripped == "files:":
            normalized_lines.append("files:")
            state = "files"
            continue
        if stripped.startswith("- path:"):
            normalized_lines.append(f"  {stripped}")
            state = "file"
            continue
        if state == "file" and _starts_with_key(stripped, "replacements"):
            normalized_lines.append("    replacements:")
            state = "replacements"
            continue
        if state == "file" and _starts_with_any_key(stripped, {"operation", "expected_sha256", "content"}):
            normalized_lines.append(f"    {stripped}")
            if _is_block_scalar_field(stripped):
                block_indent = 6
            continue
        if state in {"file", "replacements"} and stripped.startswith("- search:"):
            normalized_lines.append(f"      {stripped}")
            state = "replacement"
            if _is_block_scalar_field(stripped):
                block_indent = 10
            continue
        if state == "replacement" and _starts_with_any_key(stripped, {"replace", "expected_matches"}):
            normalized_lines.append(f"        {stripped}")
            if _is_block_scalar_field(stripped):
                block_indent = 10
            continue
        normalized_lines.append(raw_line)

    return "\n".join(normalized_lines).strip()


def _is_search_replace_control_line(stripped: str) -> bool:
    if stripped == "files:":
        return True
    if stripped.startswith("- path:") or stripped.startswith("- search:"):
        return True
    return _starts_with_any_key(
        stripped,
        {"patch_format", "operation", "expected_sha256", "content", "replacements", "replace", "expected_matches"},
    )


def _starts_with_any_key(stripped: str, keys: Iterable[str]) -> bool:
    return any(_starts_with_key(stripped, key) for key in keys)


def _starts_with_key(stripped: str, key: str) -> bool:
    return stripped == f"{key}:" or stripped.startswith(f"{key}: ")


def _is_block_scalar_field(stripped: str) -> bool:
    return stripped.endswith("|") or stripped.endswith("|-") or stripped.endswith("|+")


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


def _next_non_empty_line(lines: list[str], start_index: int) -> tuple[int, str] | None:
    for line in lines[start_index:]:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        return indent, stripped
    return None


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


def _manual_parse_relaxed_search_replace_payload(payload_text: str) -> dict[str, Any] | None:
    lines = payload_text.splitlines()
    payload: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    current_file: dict[str, Any] | None = None
    current_replacement: dict[str, Any] | None = None
    index = 0

    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue

        if _starts_with_key(stripped, "patch_format"):
            payload["patch_format"] = _parse_relaxed_scalar(stripped.split(":", 1)[1].strip())
            index += 1
            continue

        if stripped == "files:":
            index += 1
            continue

        if stripped.startswith("- path:"):
            path_text = stripped.split(":", 1)[1].strip()
            current_file = {"path": _parse_relaxed_scalar(path_text)}
            files.append(current_file)
            current_replacement = None
            index += 1
            continue

        if current_file is None:
            index += 1
            continue

        if _starts_with_key(stripped, "operation"):
            current_file["operation"] = _parse_relaxed_scalar(stripped.split(":", 1)[1].strip())
            index += 1
            continue

        if _starts_with_key(stripped, "expected_sha256"):
            current_file["expected_sha256"] = str(_parse_relaxed_scalar(stripped.split(":", 1)[1].strip()))
            index += 1
            continue

        if _starts_with_key(stripped, "replacements"):
            current_file.setdefault("replacements", [])
            current_replacement = None
            index += 1
            continue

        if stripped.startswith("- search:"):
            current_file.setdefault("replacements", [])
            current_replacement = {}
            current_file["replacements"].append(current_replacement)
            value, index = _read_relaxed_search_replace_value(lines, index, "search")
            current_replacement["search"] = value
            continue

        if _starts_with_key(stripped, "replace"):
            if current_replacement is None:
                index += 1
                continue
            value, index = _read_relaxed_search_replace_value(lines, index, "replace")
            current_replacement["replace"] = value
            continue

        if _starts_with_key(stripped, "expected_matches"):
            if current_replacement is None:
                index += 1
                continue
            current_replacement["expected_matches"] = _parse_relaxed_scalar(stripped.split(":", 1)[1].strip())
            index += 1
            continue

        if _starts_with_key(stripped, "content"):
            value, index = _read_relaxed_search_replace_value(lines, index, "content")
            current_file["content"] = value
            continue

        index += 1

    if not files:
        return None
    payload["files"] = files
    if payload.get("patch_format") != SUPPORTED_PATCH_FORMAT:
        return None
    return payload


def _read_relaxed_search_replace_value(lines: list[str], start_index: int, key: str) -> tuple[str, int]:
    stripped = lines[start_index].strip()
    value_text = stripped.split(":", 1)[1].lstrip()
    if value_text not in {"|", "|-", "|+"}:
        return str(_parse_relaxed_scalar(value_text)), start_index + 1

    block_lines: list[str] = []
    index = start_index + 1
    while index < len(lines):
        candidate = lines[index]
        candidate_stripped = candidate.strip()
        if candidate_stripped and _is_relaxed_search_replace_boundary(candidate_stripped, key):
            break
        block_lines.append(candidate)
        index += 1
    return _dedent_relaxed_block(block_lines), index


def _is_relaxed_search_replace_boundary(stripped: str, current_key: str) -> bool:
    if current_key == "content":
        return _starts_with_any_key(
            stripped,
            {"patch_format", "files", "operation", "expected_sha256", "replacements", "content"},
        ) or stripped.startswith("- path:")
    return _starts_with_any_key(
        stripped,
        {"patch_format", "files", "operation", "expected_sha256", "replacements", "replace", "expected_matches", "content"},
    ) or stripped.startswith("- path:") or stripped.startswith("- search:")


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
