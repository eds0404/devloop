"""Parser for sbt test output."""

from __future__ import annotations

from dataclasses import dataclass, field
import re


SUITE_RE = re.compile(r"^\[info\]\s+(?P<suite>[^-].+?):\s*$")
FAILED_TEST_RE = re.compile(r"^\[info\]\s+-\s+(?P<name>.+?)\s+\*\*\*\s+FAILED\s+\*\*\*\s*$")
STACK_RE = re.compile(
    r"^\[info\]\s+at\s+(?P<symbol>.+?)\((?P<file>[^():]+\.(?:scala|java)):(?P<line>\d+)\)\s*$"
)
MESSAGE_RE = re.compile(r"^\[info\]\s+(?P<message>\S.+)$")


@dataclass(slots=True)
class StackFrame:
    symbol: str
    file_name: str
    line: int


@dataclass(slots=True)
class TestFailure:
    suite_name: str
    test_name: str
    message: str
    stack_frames: list[StackFrame] = field(default_factory=list)
    raw_excerpt: list[str] = field(default_factory=list)

    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.suite_name, self.test_name, self.message)


@dataclass(slots=True)
class TestParseResult:
    failures: list[TestFailure]
    total_failures: int


def parse_sbt_test_output(text: str, max_failures: int | None = None) -> TestParseResult:
    failures: list[TestFailure] = []
    current_suite = ""
    current_failure: TestFailure | None = None

    for line in text.splitlines():
        suite_match = SUITE_RE.match(line)
        if suite_match:
            current_suite = suite_match.group("suite").strip()
            current_failure = None
            continue

        failed_match = FAILED_TEST_RE.match(line)
        if failed_match:
            current_failure = TestFailure(
                suite_name=current_suite or "UnknownSuite",
                test_name=failed_match.group("name").strip(),
                message="",
            )
            failures.append(current_failure)
            continue

        if current_failure is None:
            continue

        stack_match = STACK_RE.match(line)
        if stack_match:
            current_failure.stack_frames.append(
                StackFrame(
                    symbol=stack_match.group("symbol").strip(),
                    file_name=stack_match.group("file").strip(),
                    line=int(stack_match.group("line")),
                )
            )
            current_failure.raw_excerpt.append(line)
            continue

        message_match = MESSAGE_RE.match(line)
        if not message_match:
            continue
        message = message_match.group("message").strip()
        if message.startswith("- "):
            current_failure = None
            continue
        current_failure.raw_excerpt.append(line)
        if not current_failure.message and not message.startswith("at "):
            current_failure.message = message

    deduped: list[TestFailure] = []
    seen: set[tuple[str, str, str]] = set()
    for failure in failures:
        if not failure.message:
            failure.message = "Test failure without explicit message"
        key = failure.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(failure)

    if max_failures is not None:
        deduped = deduped[:max_failures]

    return TestParseResult(failures=deduped, total_failures=len(deduped))
