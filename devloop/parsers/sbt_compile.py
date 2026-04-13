"""Parser for sbt compile output."""

from __future__ import annotations

from dataclasses import dataclass, field
import re


HEADER_RE = re.compile(
    r"^\[error\]\s+(?P<path>(?:[A-Za-z]:)?[^:\r\n]+?\.scala):(?P<line>\d+):(?:(?P<column>\d+):)?\s*(?P<message>.*)$"
)


@dataclass(slots=True)
class CompileDiagnostic:
    file_path: str
    line: int
    column: int | None
    message: str
    details: list[str] = field(default_factory=list)
    log_snippet: list[str] = field(default_factory=list)
    caret_line: str | None = None

    def dedupe_key(self) -> tuple[str, int, int | None, str]:
        return (self.file_path, self.line, self.column, self.message)


@dataclass(slots=True)
class CompileParseResult:
    diagnostics: list[CompileDiagnostic]
    total_errors: int
    file_count: int
    raw_error_lines: int


def parse_sbt_compile_output(text: str, max_error_groups: int | None = None) -> CompileParseResult:
    diagnostics: list[CompileDiagnostic] = []
    current: CompileDiagnostic | None = None
    raw_error_lines = 0

    for line in text.splitlines():
        if not line.startswith("[error]"):
            current = None
            continue

        raw_error_lines += 1
        header_match = HEADER_RE.match(line)
        if header_match:
            current = CompileDiagnostic(
                file_path=header_match.group("path"),
                line=int(header_match.group("line")),
                column=int(header_match.group("column")) if header_match.group("column") else None,
                message=header_match.group("message").strip(),
            )
            diagnostics.append(current)
            continue

        if current is None:
            continue

        detail = line[len("[error]") :].rstrip()
        if not detail.strip():
            continue
        stripped = detail.strip()
        if stripped == "^":
            current.caret_line = stripped
        elif stripped.startswith("found") or stripped.startswith("required"):
            current.details.append(stripped)
        elif stripped.startswith("(") and "compilation failed" in stripped.lower():
            continue
        elif stripped.startswith("(") and "compile" in stripped.lower():
            continue
        else:
            current.log_snippet.append(detail.rstrip())

    deduped: list[CompileDiagnostic] = []
    seen: set[tuple[str, int, int | None, str]] = set()
    for diagnostic in diagnostics:
        key = diagnostic.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(diagnostic)

    if max_error_groups is not None:
        deduped = deduped[:max_error_groups]

    file_count = len({item.file_path for item in deduped})
    return CompileParseResult(
        diagnostics=deduped,
        total_errors=len(deduped),
        file_count=file_count,
        raw_error_lines=raw_error_lines,
    )
