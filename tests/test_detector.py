import unittest

from devloop.detector import ClipboardKind, detect_clipboard_content


COMPILE_SAMPLE = """
[error] C:\\repo\\src\\main\\scala\\com\\acme\\Parser.scala:14:8: not found: object play
[error] import play.api.libs.json._
[error]        ^
[error] (Compile / compileIncremental) Compilation failed
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
version: "1"
command: "DONE"
summary_human: "Done."
next_step_human: "Check the changes."
task_summary_en: "Finish the task."
current_goal_en: "Stop."
payload: {}
<<<DEVLOOP_COMMAND_END>>>
""".strip()


class DetectorTests(unittest.TestCase):
    def test_detects_llm_response_first(self) -> None:
        result = detect_clipboard_content(LLM_SAMPLE)
        self.assertEqual(result.kind, ClipboardKind.LLM_RESPONSE)

    def test_detects_compile_log(self) -> None:
        result = detect_clipboard_content(COMPILE_SAMPLE)
        self.assertEqual(result.kind, ClipboardKind.SBT_COMPILE)

    def test_detects_test_log(self) -> None:
        result = detect_clipboard_content(TEST_SAMPLE)
        self.assertEqual(result.kind, ClipboardKind.SBT_TEST)

    def test_falls_back_to_raw_text(self) -> None:
        result = detect_clipboard_content("plain notes for ChatGPT")
        self.assertEqual(result.kind, ClipboardKind.RAW_TEXT)


if __name__ == "__main__":
    unittest.main()
