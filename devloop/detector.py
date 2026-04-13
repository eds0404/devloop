"""Clipboard content type detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re

from devloop.protocol import COMMAND_END, COMMAND_START


class ClipboardKind(str, Enum):
    LLM_RESPONSE = "llm_response"
    SBT_COMPILE = "sbt_compile"
    SBT_TEST = "sbt_test"
    RAW_TEXT = "raw_text"


@dataclass(slots=True)
class DetectionResult:
    kind: ClipboardKind
    score: int
    reasons: list[str] = field(default_factory=list)


def detect_clipboard_content(text: str) -> DetectionResult:
    if COMMAND_START in text and COMMAND_END in text:
        return DetectionResult(
            kind=ClipboardKind.LLM_RESPONSE,
            score=100,
            reasons=["Found exact devloop command markers"],
        )

    compile_result = _score_compile_output(text)
    if compile_result.score >= 4:
        return compile_result

    test_result = _score_test_output(text)
    if test_result.score >= 4:
        return test_result

    return DetectionResult(
        kind=ClipboardKind.RAW_TEXT,
        score=1,
        reasons=["No LLM block or strong sbt compile/test signals detected"],
    )


def _score_compile_output(text: str) -> DetectionResult:
    score = 0
    reasons: list[str] = []
    if re.search(r"(?im)^\[error\]\s+.*\.scala:\d+:\d+:", text):
        score += 3
        reasons.append("Found Scala file:line:column compiler errors")
    error_count = len(re.findall(r"(?im)^\[error\]\s+", text))
    if error_count >= 3:
        score += 2
        reasons.append(f"Found {error_count} [error] lines")
    if re.search(r"(?i)Compilation failed", text):
        score += 3
        reasons.append("Found 'Compilation failed'")
    if re.search(r"(?im)^\[error\]\s+\(.+Compile.+\)", text):
        score += 2
        reasons.append("Found sbt compile task failure marker")
    if re.search(r"(?im)^\[error\]\s+\^$", text):
        score += 1
        reasons.append("Found compiler caret lines")
    return DetectionResult(kind=ClipboardKind.SBT_COMPILE, score=score, reasons=reasons)


def _score_test_output(text: str) -> DetectionResult:
    score = 0
    reasons: list[str] = []
    failed_count = len(re.findall(r"\*\*\*\s+FAILED\s+\*\*\*", text))
    if failed_count:
        score += 3
        reasons.append(f"Found {failed_count} ScalaTest failure markers")
    if re.search(r"(?im)^\[info\]\s+.+Test:\s*$", text):
        score += 2
        reasons.append("Found suite headers ending with Test:")
    stack_count = len(re.findall(r"(?im)^\[info\]\s+at\s+.+\((?:.+\.scala|.+\.java):\d+\)", text))
    if stack_count >= 2:
        score += 2
        reasons.append("Found project stack frames")
    if re.search(r"(?i)\bTEST FAILED\b", text):
        score += 2
        reasons.append("Found 'TEST FAILED'")
    if re.search(r"(?im)^\[info\]\s+-\s+.+\*\*\*\s+FAILED\s+\*\*\*", text):
        score += 2
        reasons.append("Found failed test case lines")
    return DetectionResult(kind=ClipboardKind.SBT_TEST, score=score, reasons=reasons)
