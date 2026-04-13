"""Minimal YAML-compatible loader and dumper for the devloop MVP."""

from __future__ import annotations

import ast
import json
import re
from typing import Any


class YAMLError(ValueError):
    """Raised when the lightweight YAML parser cannot parse input."""


def safe_load(text: str) -> Any:
    parser = _YamlParser(text)
    return parser.parse()


def safe_dump(
    data: Any,
    *,
    sort_keys: bool = False,
    allow_unicode: bool = True,
) -> str:
    rendered = _dump_node(data, indent=0, sort_keys=sort_keys, allow_unicode=allow_unicode)
    return rendered.rstrip() + "\n"


class _YamlParser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()

    def parse(self) -> Any:
        index = self._skip_empty(0)
        if index >= len(self.lines):
            return None
        indent = self._indent(self.lines[index])
        value, next_index = self._parse_block(index, indent)
        next_index = self._skip_empty(next_index)
        if next_index != len(self.lines):
            raise YAMLError("Unexpected trailing content")
        return value

    def _parse_block(self, index: int, indent: int) -> tuple[Any, int]:
        index = self._skip_empty(index)
        if index >= len(self.lines):
            return None, index
        current_indent = self._indent(self.lines[index])
        if current_indent < indent:
            return None, index
        if current_indent != indent:
            raise YAMLError("Unexpected indentation")
        stripped = self.lines[index][indent:]
        if stripped in {"{}", "[]"}:
            return ast.literal_eval(stripped), index + 1
        if stripped == "-" or stripped.startswith("- "):
            return self._parse_sequence(index, indent)
        return self._parse_mapping(index, indent)

    def _parse_mapping(self, index: int, indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while True:
            index = self._skip_empty(index)
            if index >= len(self.lines):
                return mapping, index
            current_indent = self._indent(self.lines[index])
            if current_indent < indent:
                return mapping, index
            if current_indent != indent:
                raise YAMLError("Unexpected indentation inside mapping")
            stripped = self.lines[index][indent:]
            if stripped == "-" or stripped.startswith("- "):
                return mapping, index
            key, value_text = _split_key_value(stripped)
            value, index = self._parse_value(value_text, index + 1, indent)
            mapping[key] = value

    def _parse_sequence(self, index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while True:
            index = self._skip_empty(index)
            if index >= len(self.lines):
                return items, index
            current_indent = self._indent(self.lines[index])
            if current_indent < indent:
                return items, index
            if current_indent != indent:
                raise YAMLError("Unexpected indentation inside sequence")
            stripped = self.lines[index][indent:]
            if not (stripped == "-" or stripped.startswith("- ")):
                return items, index
            rest = stripped[1:].lstrip()
            if not rest:
                next_index = self._skip_empty(index + 1)
                if next_index >= len(self.lines) or self._indent(self.lines[next_index]) <= indent:
                    items.append(None)
                    index += 1
                    continue
                value, index = self._parse_block(next_index, self._indent(self.lines[next_index]))
                items.append(value)
                continue
            if _looks_like_mapping(rest):
                value, index = self._parse_sequence_mapping_item(
                    rest,
                    index + 1,
                    sequence_indent=indent,
                    child_indent=indent + 2,
                )
                items.append(value)
                continue
            if rest == "|":
                value, index = self._parse_block_scalar(index + 1, indent)
                items.append(value)
                continue
            items.append(_parse_scalar(rest))
            index += 1

    def _parse_sequence_mapping_item(
        self,
        first_rest: str,
        index: int,
        *,
        sequence_indent: int,
        child_indent: int,
    ) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        key, value_text = _split_key_value(first_rest)
        value, index = self._parse_value(value_text, index, sequence_indent)
        mapping[key] = value

        while True:
            index = self._skip_empty(index)
            if index >= len(self.lines):
                return mapping, index
            current_indent = self._indent(self.lines[index])
            if current_indent < child_indent:
                return mapping, index
            if current_indent != child_indent:
                raise YAMLError("Unexpected indentation inside sequence mapping item")
            stripped = self.lines[index][child_indent:]
            if stripped == "-" or stripped.startswith("- "):
                return mapping, index
            key, value_text = _split_key_value(stripped)
            value, index = self._parse_value(value_text, index + 1, child_indent)
            mapping[key] = value

    def _parse_value(self, value_text: str, next_index: int, current_indent: int) -> tuple[Any, int]:
        if value_text == "|":
            return self._parse_block_scalar(next_index, current_indent)
        if value_text == "":
            next_index = self._skip_empty(next_index)
            if next_index >= len(self.lines) or self._indent(self.lines[next_index]) <= current_indent:
                return {}, next_index
            return self._parse_block(next_index, self._indent(self.lines[next_index]))
        return _parse_scalar(value_text), next_index

    def _parse_block_scalar(self, index: int, parent_indent: int) -> tuple[str, int]:
        index = self._skip_empty(index)
        if index >= len(self.lines):
            return "", index
        first_indent = self._indent(self.lines[index])
        if first_indent <= parent_indent:
            return "", index
        block_indent = first_indent
        lines: list[str] = []
        while index < len(self.lines):
            raw = self.lines[index]
            if not raw.strip():
                lines.append("")
                index += 1
                continue
            current_indent = self._indent(raw)
            if current_indent < block_indent:
                break
            lines.append(raw[block_indent:])
            index += 1
        return "\n".join(lines), index

    def _skip_empty(self, index: int) -> int:
        while index < len(self.lines) and not self.lines[index].strip():
            index += 1
        return index

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))


def _split_key_value(text: str) -> tuple[str, str]:
    in_single = False
    in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ":" and not in_single and not in_double:
            key = text[:index].strip()
            if not key:
                raise YAMLError("Empty mapping key")
            return key, text[index + 1 :].lstrip()
    raise YAMLError(f"Expected a key/value pair, got: {text}")


def _looks_like_mapping(text: str) -> bool:
    try:
        _split_key_value(text)
    except YAMLError:
        return False
    return True


def _parse_scalar(text: str) -> Any:
    stripped = text.strip()
    if stripped in {"{}", "[]"}:
        return ast.literal_eval(stripped)
    if stripped in {"null", "~"}:
        return None
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", stripped):
        return int(stripped)
    if re.fullmatch(r"[-+]?\d+\.\d+", stripped):
        return float(stripped)
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        try:
            return ast.literal_eval(stripped)
        except (SyntaxError, ValueError) as exc:
            raise YAMLError(f"Invalid quoted scalar: {stripped}") from exc
    return stripped


def _dump_node(
    value: Any,
    *,
    indent: int,
    sort_keys: bool,
    allow_unicode: bool,
) -> str:
    if isinstance(value, dict):
        return _dump_mapping(value, indent=indent, sort_keys=sort_keys, allow_unicode=allow_unicode)
    if isinstance(value, list):
        return _dump_sequence(value, indent=indent, sort_keys=sort_keys, allow_unicode=allow_unicode)
    return (" " * indent) + _dump_scalar(value, allow_unicode=allow_unicode)


def _dump_mapping(
    value: dict[str, Any],
    *,
    indent: int,
    sort_keys: bool,
    allow_unicode: bool,
) -> str:
    if not value:
        return (" " * indent) + "{}"
    lines: list[str] = []
    keys = sorted(value.keys()) if sort_keys else list(value.keys())
    for key in keys:
        rendered_key = str(key)
        item = value[key]
        prefix = (" " * indent) + f"{rendered_key}:"
        if _is_simple_scalar(item):
            lines.append(f"{prefix} {_dump_scalar(item, allow_unicode=allow_unicode)}")
        elif isinstance(item, dict) and not item:
            lines.append(prefix + " {}")
        elif isinstance(item, list) and not item:
            lines.append(prefix + " []")
        elif isinstance(item, str):
            lines.append(prefix + " |")
            for line in item.splitlines():
                lines.append((" " * (indent + 2)) + line)
        elif isinstance(item, dict):
            lines.append(prefix)
            lines.extend(
                _dump_mapping(item, indent=indent + 2, sort_keys=sort_keys, allow_unicode=allow_unicode).splitlines()
            )
        elif isinstance(item, list):
            lines.append(prefix)
            lines.extend(
                _dump_sequence(item, indent=indent + 2, sort_keys=sort_keys, allow_unicode=allow_unicode).splitlines()
            )
        else:
            lines.append(f"{prefix} {_dump_scalar(item, allow_unicode=allow_unicode)}")
    return "\n".join(lines)


def _dump_sequence(
    value: list[Any],
    *,
    indent: int,
    sort_keys: bool,
    allow_unicode: bool,
) -> str:
    if not value:
        return (" " * indent) + "[]"
    lines: list[str] = []
    for item in value:
        prefix = (" " * indent) + "-"
        if _is_simple_scalar(item):
            lines.append(f"{prefix} {_dump_scalar(item, allow_unicode=allow_unicode)}")
        elif isinstance(item, str):
            lines.append(prefix + " |")
            for line in item.splitlines():
                lines.append((" " * (indent + 2)) + line)
        elif isinstance(item, dict):
            if not item:
                lines.append(prefix + " {}")
            else:
                lines.append(prefix)
                lines.extend(
                    _dump_mapping(item, indent=indent + 2, sort_keys=sort_keys, allow_unicode=allow_unicode).splitlines()
                )
        elif isinstance(item, list):
            if not item:
                lines.append(prefix + " []")
            else:
                lines.append(prefix)
                lines.extend(
                    _dump_sequence(item, indent=indent + 2, sort_keys=sort_keys, allow_unicode=allow_unicode).splitlines()
                )
        else:
            lines.append(f"{prefix} {_dump_scalar(item, allow_unicode=allow_unicode)}")
    return "\n".join(lines)


def _is_simple_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float)) or (
        isinstance(value, str) and "\n" not in value
    )


def _dump_scalar(value: Any, *, allow_unicode: bool) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if not value:
            return '""'
        if re.fullmatch(r"[A-Za-z0-9_./\\-]+", value) and value.lower() not in {"true", "false", "null"}:
            return value
        return json.dumps(value, ensure_ascii=not allow_unicode)
    return json.dumps(str(value), ensure_ascii=not allow_unicode)
