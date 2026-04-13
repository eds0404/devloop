"""Machine-readable protocol parsing for LLM responses."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import re
from typing import Any

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
    _validate_marker_counts(response_text)
    start_index = response_text.index(COMMAND_START) + len(COMMAND_START)
    end_index = response_text.index(COMMAND_END)
    if end_index <= start_index:
        raise ProtocolError("Command block markers are in the wrong order")
    block = response_text[start_index:end_index].strip()
    if not block:
        raise ProtocolError("Command block is empty")
    return block


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
    patch = payload.get("patch")
    if patch_format != "git_unified_diff":
        raise ProtocolError("Only patch_format=git_unified_diff is supported")
    if not isinstance(patch, str) or not patch.strip():
        raise ProtocolError("APPLY_PATCH payload must contain a non-empty patch string")


def _strip_command_block(response_text: str, raw_block: str) -> str:
    _validate_marker_counts(response_text)
    start_index = response_text.index(COMMAND_START)
    end_index = response_text.index(COMMAND_END) + len(COMMAND_END)
    return (response_text[:start_index] + response_text[end_index:]).strip()


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
    patch_format: str | None = None
    patch: str | None = None
    lines = payload_text.splitlines()

    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("patch_format:"):
            patch_format = str(_parse_relaxed_scalar(stripped.split(":", 1)[1].strip()))
            continue
        if stripped.startswith("patch:"):
            after_colon = stripped.split(":", 1)[1].strip()
            if after_colon in {"|", "|-", "|+"}:
                patch = "\n".join(lines[index + 1 :]).rstrip("\n")
            else:
                patch = after_colon
            break

    if patch_format is None or patch is None:
        return None
    return {
        "patch_format": patch_format,
        "patch": patch,
    }


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
