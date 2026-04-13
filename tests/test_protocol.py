import unittest

from devloop.errors import ProtocolError
from devloop.protocol import extract_command_block, parse_protocol_response


SAMPLE_RESPONSE = """
I will first collect only the needed context.

<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "COLLECT_CONTEXT"
summary_human: "Collecting the needed context."
next_step_human: "Paste the new prompt into ChatGPT."
task_summary_en: "Fix the compile issue."
current_goal_en: "Inspect the parser implementation."
payload:
  prompt_goal: "Diagnose the compile failure."
  queries:
    - type: "read_snippet"
      file: "src/main/scala/com/acme/Parser.scala"
      start_line: 10
      end_line: 30
<<<DEVLOOP_COMMAND_END>>>
""".strip()


class ProtocolTests(unittest.TestCase):
    def test_extracts_single_command_block(self) -> None:
        block = extract_command_block(SAMPLE_RESPONSE)
        self.assertIn('command: "COLLECT_CONTEXT"', block)

    def test_parses_protocol_envelope(self) -> None:
        envelope = parse_protocol_response(SAMPLE_RESPONSE)
        self.assertEqual(envelope.command.command, "COLLECT_CONTEXT")
        self.assertEqual(envelope.command.task_summary_en, "Fix the compile issue.")
        self.assertEqual(envelope.command.summary_human, "Collecting the needed context.")
        self.assertEqual(envelope.command.payload["queries"][0]["type"], "read_snippet")

    def test_rejects_multiple_blocks(self) -> None:
        invalid = SAMPLE_RESPONSE + "\n" + SAMPLE_RESPONSE
        with self.assertRaises(ProtocolError):
            extract_command_block(invalid)

    def test_accepts_legacy_human_fields(self) -> None:
        legacy_response = SAMPLE_RESPONSE.replace("summary_human", "summary_ru").replace(
            "next_step_human",
            "next_step_ru",
        )
        envelope = parse_protocol_response(legacy_response)
        self.assertEqual(envelope.command.summary_human, "Collecting the needed context.")
        self.assertEqual(envelope.command.next_step_human, "Paste the new prompt into ChatGPT.")

    def test_extracts_command_block_markers(self) -> None:
        block = extract_command_block(SAMPLE_RESPONSE)
        self.assertIn('version: "1"', block)

    def test_reports_indentation_hint_for_collect_context_payload_lists(self) -> None:
        malformed = """
Brief explanation.

<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "COLLECT_CONTEXT"
summary_human: "Нужно собрать контекст."
next_step_human: "Вставь новый prompt в ChatGPT."
task_summary_en: "Continue after migration."
current_goal_en: "Collect current source context."
payload:
queries:
- type: "read_snippet"
  file: "src/main/scala/com/acme/Parser.scala"
  start_line: 10
  end_line: 20
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        with self.assertRaises(ProtocolError) as context:
            parse_protocol_response(malformed)
        self.assertIn("payload", str(context.exception))
        self.assertIn("indented", str(context.exception))

    def test_moves_unexpected_top_level_fields_into_payload(self) -> None:
        malformed = """
Brief explanation.

<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "ASK_HUMAN"
summary_human: "Need a rerun."
next_step_human: "Run the tests."
task_summary_en: "Continue after migration."
current_goal_en: "Collect fresh IT failures."
payload: {}
requested_runs: []
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(malformed)
        self.assertEqual(envelope.command.command, "ASK_HUMAN")
        self.assertEqual(envelope.command.payload["requested_runs"], [])

    def test_relaxed_parser_accepts_misaligned_ask_human_payload(self) -> None:
        malformed = """
Сначала нужен свежий результат прогона.

<<<DEVLOOP_COMMAND_START>>>
version: 1
command: ASK_HUMAN
summary_human: "Сначала нужен свежий результат прогона."
next_step_human: "Запусти узкий прогон."
task_summary_en: "Continue after migration."
current_goal_en: "Collect current IT failures."
payload:
requested_runs:
- kind: sbt
purpose: "Collect current failures"
command_example: "sbt 'core/IntegrationTest/test'"
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(malformed)
        self.assertEqual(envelope.command.command, "ASK_HUMAN")
        self.assertIn("requested_runs", envelope.command.payload["raw_payload_text"])

    def test_relaxed_parser_accepts_apply_patch_payload(self) -> None:
        malformed = """
Patch ready.

<<<DEVLOOP_COMMAND_START>>>
version: 1
command: APPLY_PATCH
summary_human: "Применяю минимальный патч."
next_step_human: "Запусти compile."
task_summary_en: "Fix the remaining compile issue."
current_goal_en: "Apply the smallest diff."
payload:
patch_format: git_unified_diff
patch: |
  diff --git a/src/main/scala/com/acme/Parser.scala b/src/main/scala/com/acme/Parser.scala
  --- a/src/main/scala/com/acme/Parser.scala
  +++ b/src/main/scala/com/acme/Parser.scala
  @@
  -old
  +new
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(malformed)
        self.assertEqual(envelope.command.command, "APPLY_PATCH")
        self.assertEqual(envelope.command.payload["patch_format"], "git_unified_diff")
        self.assertIn("diff --git", envelope.command.payload["patch"])

    def test_relaxed_parser_accepts_realistic_malformed_apply_patch_block(self) -> None:
        malformed = """
Patch ready.

<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "APPLY_PATCH"
summary_human: "Apply the patch."
next_step_human: "Run compile."
task_summary_en: "Fix the remaining integration-test compile issue."
current_goal_en: "Remove the leftover Play JSON usage."
payload:
patch_format: "git_unified_diff"
patch: |
diff --git a/external-api/src/it/scala/com/acme/DayEndCommandsSpec.scala b/external-api/src/it/scala/com/acme/DayEndCommandsSpec.scala
--- a/external-api/src/it/scala/com/acme/DayEndCommandsSpec.scala
+++ b/external-api/src/it/scala/com/acme/DayEndCommandsSpec.scala
@@ -1,4 +1,4 @@
import com.acme.LegacyJson
+import io.circe.syntax._
-import play.api.libs.json.Json
```
 import java.time.Instant
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(malformed)
        self.assertEqual(envelope.command.command, "APPLY_PATCH")
        self.assertEqual(envelope.command.payload["patch_format"], "git_unified_diff")
        self.assertIn("DayEndCommandsSpec.scala", envelope.command.payload["patch"])
        self.assertIn("```", envelope.command.payload["patch"])


if __name__ == "__main__":
    unittest.main()
