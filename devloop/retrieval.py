"""Deterministic repository retrieval and safe file access."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re
from typing import Iterable

from devloop.config import DevloopConfig
from devloop.errors import RetrievalError
from devloop.parsers.sbt_compile import CompileParseResult
from devloop.parsers.sbt_test import TestParseResult


TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp1251", "cp1252", "latin-1")


@dataclass(slots=True)
class QueryResult:
    query_type: str
    title: str
    body: str


class RepositoryRetriever:
    """Safe deterministic retrieval inside the repository root."""

    def __init__(self, repo_root: Path, config: DevloopConfig) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config

    def execute_queries(self, queries: list[dict[str, object]]) -> list[QueryResult]:
        results: list[QueryResult] = []
        for index, query in enumerate(queries, start=1):
            if "type" not in query:
                raise RetrievalError("Each query must define a type")
            query_type = str(query["type"])
            results.append(self._execute_single_query(index, query_type, query))
        return results

    def build_compile_query_results(self, parsed: CompileParseResult) -> list[QueryResult]:
        diagnostics: list[str] = []
        for index, diagnostic in enumerate(parsed.diagnostics[: self.config.max_error_groups], start=1):
            snippet = self._read_source_snippet_from_log_path(diagnostic.file_path, diagnostic.line)
            detail_lines = [
                f"Diagnostic {index}",
                f"Location: {diagnostic.file_path}:{diagnostic.line}"
                + (f":{diagnostic.column}" if diagnostic.column is not None else ""),
                f"Message: {diagnostic.message}",
            ]
            if diagnostic.details:
                detail_lines.append("Details:")
                detail_lines.extend(f"- {item}" for item in diagnostic.details)
            if diagnostic.log_snippet:
                detail_lines.append("Log snippet:")
                detail_lines.extend(diagnostic.log_snippet[:4])
            if snippet:
                detail_lines.append("Source snippet:")
                detail_lines.append(snippet)
            diagnostics.append("\n".join(detail_lines))
        compile_status = "yes" if parsed.succeeded else "no"
        summary = (
            f"Compile succeeded: {compile_status}\n"
            f"Compile diagnostics included: {len(parsed.diagnostics)}\n"
            f"Distinct files: {parsed.file_count}\n"
            f"Raw [error] lines seen: {parsed.raw_error_lines}\n"
            f"Raw [warn] lines seen: {parsed.raw_warning_lines}"
        )
        details_body = "\n\n".join(diagnostics)
        if not details_body and parsed.succeeded:
            details_body = "No compile errors parsed. The compile run completed successfully."
        return [
            QueryResult("compile_summary", "Parsed compile diagnostics", summary),
            QueryResult("compile_details", "Compile detail blocks", details_body or "No diagnostics parsed."),
        ]

    def build_test_query_results(self, parsed: TestParseResult) -> list[QueryResult]:
        failure_blocks: list[str] = []
        for index, failure in enumerate(parsed.failures[: self.config.max_test_failures], start=1):
            lines = [
                f"Failure {index}",
                f"Suite: {failure.suite_name}",
                f"Test: {failure.test_name}",
                f"Message: {failure.message}",
            ]
            relevant_frames = []
            for frame in failure.stack_frames[:5]:
                frame_snippet = self._find_snippet_for_frame(frame.file_name, frame.line)
                frame_text = f"- {frame.symbol} ({frame.file_name}:{frame.line})"
                if frame_snippet:
                    frame_text += "\n" + frame_snippet
                relevant_frames.append(frame_text)
            if relevant_frames:
                lines.append("Relevant frames:")
                lines.extend(relevant_frames)
            failure_blocks.append("\n".join(lines))
        return [
            QueryResult(
                "test_summary",
                "Parsed failing tests",
                f"Failing tests included: {len(parsed.failures)}",
            ),
            QueryResult("test_details", "Test failure blocks", "\n\n".join(failure_blocks) or "No failures parsed."),
        ]

    def build_raw_clipboard_query_result(self, text: str) -> QueryResult:
        excerpt = _clip_lines(text, self.config.max_snippet_lines)
        return QueryResult("raw_clipboard", "Raw clipboard content", excerpt)

    def iter_project_files(self) -> Iterable[Path]:
        files: list[Path] = []
        for path in self.repo_root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.repo_root).as_posix()
            if self._is_excluded(rel):
                continue
            if not self._is_included(rel):
                continue
            files.append(path)
        files.sort(key=lambda item: item.relative_to(self.repo_root).as_posix())
        return files

    def resolve_repo_path(self, user_path: str) -> Path:
        normalized_user_path = user_path.replace("\\", "/")
        path = Path(normalized_user_path)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            resolved = (self.repo_root / path).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise RetrievalError(f"Path escapes repository root: {user_path}") from exc
        if not resolved.exists():
            raise RetrievalError(f"Path does not exist: {user_path}")
        if not resolved.is_file():
            raise RetrievalError(f"Path is not a file: {user_path}")
        return resolved

    def read_text_file(self, path: Path) -> str:
        for encoding in TEXT_ENCODINGS:
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise RetrievalError(f"Could not decode file as text: {path}")

    def read_snippet(self, path: Path, start_line: int, end_line: int) -> str:
        if start_line <= 0 or end_line < start_line:
            raise RetrievalError("Invalid snippet line range")
        lines = self.read_text_file(path).splitlines()
        snippet_lines = lines[start_line - 1 : end_line]
        rendered = []
        for index, line in enumerate(snippet_lines, start=start_line):
            rendered.append(f"{index:>5} | {line}")
        return "\n".join(rendered) if rendered else "<empty snippet>"

    def project_tree_summary(self) -> str:
        files = list(self.iter_project_files())
        lines: list[str] = []
        for path in files[: self.config.max_files]:
            lines.append(f"- {path.relative_to(self.repo_root).as_posix()}")
        omitted = max(0, len(files) - len(lines))
        if omitted:
            lines.append(f"- ... omitted {omitted} more files")
        return "\n".join(lines) or "<no files matched include/exclude rules>"

    def find_related_files(self, target: str) -> list[Path]:
        stem = Path(target).stem if "." in target or "/" in target or "\\" in target else target
        matches: list[Path] = []
        for path in self.iter_project_files():
            rel = path.relative_to(self.repo_root).as_posix()
            if stem.lower() in path.stem.lower() and "src/test/" not in rel:
                matches.append(path)
        return matches[: self.config.max_files]

    def find_related_tests(self, target: str) -> list[Path]:
        stem = Path(target).stem if "." in target or "/" in target or "\\" in target else target
        matches: list[Path] = []
        for path in self.iter_project_files():
            rel = path.relative_to(self.repo_root).as_posix().lower()
            if "src/test/" not in rel:
                continue
            if stem.lower() in path.stem.lower():
                matches.append(path)
        return matches[: self.config.max_files]

    def _execute_single_query(
        self,
        index: int,
        query_type: str,
        query: dict[str, object],
    ) -> QueryResult:
        if query_type == "project_tree":
            return QueryResult(query_type, f"Query {index}: project_tree", self.project_tree_summary())
        if query_type == "file_search":
            name = self._require_string(query, "query")
            limit = self._bounded_limit(query.get("limit"), self.config.max_search_results)
            matches = [
                path
                for path in self.iter_project_files()
                if name.lower() in path.name.lower()
            ][:limit]
            return QueryResult(query_type, f"Query {index}: file_search", self._render_path_list(matches))
        if query_type == "path_search":
            needle = self._require_string(query, "query").lower()
            limit = self._bounded_limit(query.get("limit"), self.config.max_search_results)
            matches = [
                path
                for path in self.iter_project_files()
                if needle in path.relative_to(self.repo_root).as_posix().lower()
            ][:limit]
            return QueryResult(query_type, f"Query {index}: path_search", self._render_path_list(matches))
        if query_type in {"text_search", "regex_search"}:
            return self._run_textual_search(index, query_type, query)
        if query_type == "read_file":
            file_path = self.resolve_repo_path(self._require_string(query, "file"))
            text = self.read_text_file(file_path)
            return QueryResult(query_type, f"Query {index}: read_file", _clip_lines(text, self.config.max_snippet_lines))
        if query_type == "read_snippet":
            file_path = self.resolve_repo_path(self._require_string(query, "file"))
            start_line = int(query.get("start_line", 1))
            end_line = int(query.get("end_line", start_line + self.config.max_snippet_lines - 1))
            return QueryResult(
                query_type,
                f"Query {index}: read_snippet",
                self.read_snippet(file_path, start_line, end_line),
            )
        if query_type == "read_around_match":
            return self._run_read_around_match(index, query)
        if query_type == "related_files":
            target = self._require_string(query, "query")
            return QueryResult(
                query_type,
                f"Query {index}: related_files",
                self._render_path_list(self.find_related_files(target)),
            )
        if query_type == "related_tests":
            target = self._require_string(query, "query")
            return QueryResult(
                query_type,
                f"Query {index}: related_tests",
                self._render_path_list(self.find_related_tests(target)),
            )
        raise RetrievalError(f"Unsupported query type: {query_type}")

    def _run_textual_search(
        self,
        index: int,
        query_type: str,
        query: dict[str, object],
    ) -> QueryResult:
        needle = self._require_string(query, "query")
        limit = self._bounded_limit(query.get("limit"), self.config.max_search_results)
        glob = str(query.get("glob", "**/*"))
        try:
            regex = re.compile(needle) if query_type == "regex_search" else None
        except re.error as exc:
            raise RetrievalError(f"Invalid regex query: {exc}") from exc
        matches: list[str] = []
        for path in self.iter_project_files():
            rel = path.relative_to(self.repo_root).as_posix()
            if not fnmatch.fnmatch(rel, glob):
                continue
            try:
                lines = self.read_text_file(path).splitlines()
            except RetrievalError:
                continue
            for line_number, line in enumerate(lines, start=1):
                matched = bool(regex.search(line)) if regex else needle in line
                if matched:
                    matches.append(f"{rel}:{line_number}: {line.strip()}")
                    if len(matches) >= limit:
                        return QueryResult(query_type, f"Query {index}: {query_type}", "\n".join(matches))
        return QueryResult(query_type, f"Query {index}: {query_type}", "\n".join(matches) or "<no matches>")

    def _run_read_around_match(self, index: int, query: dict[str, object]) -> QueryResult:
        needle = self._require_string(query, "query")
        glob = str(query.get("glob", "**/*"))
        limit = self._bounded_limit(query.get("limit"), self.config.max_search_results)
        before = int(query.get("before", self.config.snippet_context_before))
        after = int(query.get("after", self.config.snippet_context_after))
        blocks: list[str] = []
        for path in self.iter_project_files():
            rel = path.relative_to(self.repo_root).as_posix()
            if not fnmatch.fnmatch(rel, glob):
                continue
            lines = self.read_text_file(path).splitlines()
            for line_number, line in enumerate(lines, start=1):
                if needle not in line:
                    continue
                start_line = max(1, line_number - before)
                end_line = min(len(lines), line_number + after)
                blocks.append(f"File: {rel}\n{self.read_snippet(path, start_line, end_line)}")
                if len(blocks) >= limit:
                    return QueryResult("read_around_match", f"Query {index}: read_around_match", "\n\n".join(blocks))
        return QueryResult("read_around_match", f"Query {index}: read_around_match", "\n\n".join(blocks) or "<no matches>")

    def _read_source_snippet_from_log_path(self, file_path: str, line_number: int) -> str:
        candidate = self._map_log_path_to_repo_file(file_path)
        if not candidate:
            return ""
        start_line = max(1, line_number - self.config.snippet_context_before)
        end_line = line_number + self.config.snippet_context_after
        return self.read_snippet(candidate, start_line, end_line)

    def _find_snippet_for_frame(self, file_name: str, line_number: int) -> str:
        matches = [path for path in self.iter_project_files() if path.name == file_name]
        if not matches:
            return ""
        candidate = matches[0]
        start_line = max(1, line_number - self.config.snippet_context_before)
        end_line = line_number + self.config.snippet_context_after
        return self.read_snippet(candidate, start_line, end_line)

    def _map_log_path_to_repo_file(self, file_path: str) -> Path | None:
        path = Path(file_path)
        if path.is_absolute():
            try:
                resolved = path.resolve()
                resolved.relative_to(self.repo_root)
                return resolved
            except (FileNotFoundError, OSError, ValueError):
                pass
        normalized = file_path.replace("\\", "/")
        if "/src/" in normalized:
            suffix = normalized.split("/src/", maxsplit=1)[1]
            match_suffix = f"src/{suffix}"
            matches = [path for path in self.iter_project_files() if path.as_posix().endswith(match_suffix)]
            if len(matches) == 1:
                return matches[0]
        by_name = [path for path in self.iter_project_files() if path.name == Path(file_path).name]
        return by_name[0] if len(by_name) == 1 else None

    def _bounded_limit(self, raw_value: object, default_limit: int) -> int:
        if raw_value is None:
            return default_limit
        limit = int(raw_value)
        if limit <= 0 or limit > default_limit:
            raise RetrievalError(f"Query limit must be between 1 and {default_limit}")
        return limit

    def _require_string(self, query: dict[str, object], key: str) -> str:
        value = query.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RetrievalError(f"Query field {key} must be a non-empty string")
        return value

    def _render_path_list(self, paths: list[Path]) -> str:
        if not paths:
            return "<no matches>"
        return "\n".join(f"- {path.relative_to(self.repo_root).as_posix()}" for path in paths)

    def _is_included(self, rel_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in self.config.include_globs)

    def _is_excluded(self, rel_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in self.config.exclude_globs)


def _clip_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    omitted = len(lines) - max_lines
    kept.append(f"... omitted {omitted} more lines")
    return "\n".join(kept)
