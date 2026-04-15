import unittest

from devloop.detector import ClipboardKind, detect_clipboard_content


COMPILE_SAMPLE = """
[error] C:\\repo\\src\\main\\scala\\com\\acme\\Parser.scala:14:8: not found: object play
[error] import play.api.libs.json._
[error]        ^
[error] (Compile / compileIncremental) Compilation failed
""".strip()

COMPILE_SUCCESS_SAMPLE = """
[info] scalafmt: Formatting 1 Scala sources (C:\\repo\\core)...
[info] compiling 6 Scala sources to C:\\repo\\core\\target\\scala-2.13\\classes ...
[warn] C:\\repo\\core\\src\\main\\scala\\com\\acme\\Parser.scala:10:5: discarded non-Unit value
[info] done compiling
[success] Total time: 12 s, completed Apr 13, 2026, 4:09:45 PM
""".strip()

TEST_SAMPLE = """
[info] ParserSpec:
[info] - should parse empty input *** FAILED ***
[info]   java.lang.AssertionError: expected 1 but got 2
[info]   at com.acme.ParserSpec.$anonfun$new$1(ParserSpec.scala:42)
[info]   at com.acme.Parser.parse(Parser.scala:18)
""".strip()

LLM_SAMPLE = """
Short explanation.
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: DONE
SUMMARY_HUMAN: Done.
NEXT_STEP_HUMAN: Check the changes.
TASK_SUMMARY_EN: Finish the task.
CURRENT_GOAL_EN: Stop.
<<<DEVLOOP_COMMAND_END>>>
""".strip()


class DetectorTests(unittest.TestCase):
    def test_detects_llm_response_first(self) -> None:
        result = detect_clipboard_content(LLM_SAMPLE)
        self.assertEqual(result.kind, ClipboardKind.LLM_RESPONSE)

    def test_detects_compile_log(self) -> None:
        result = detect_clipboard_content(COMPILE_SAMPLE)
        self.assertEqual(result.kind, ClipboardKind.SBT_COMPILE)

    def test_detects_successful_compile_log_without_errors(self) -> None:
        result = detect_clipboard_content(COMPILE_SUCCESS_SAMPLE)
        self.assertEqual(result.kind, ClipboardKind.SBT_COMPILE)

    def test_detects_test_log(self) -> None:
        result = detect_clipboard_content(TEST_SAMPLE)
        self.assertEqual(result.kind, ClipboardKind.SBT_TEST)

    def test_falls_back_to_raw_text(self) -> None:
        result = detect_clipboard_content("plain notes for ChatGPT")
        self.assertEqual(result.kind, ClipboardKind.RAW_TEXT)


if __name__ == "__main__":
    unittest.main()
