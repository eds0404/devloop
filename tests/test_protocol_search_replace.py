import unittest

from devloop.errors import ProtocolError
from devloop.protocol import parse_protocol_response


class ProtocolSearchReplaceTests(unittest.TestCase):
    def test_parses_v2_search_replace_apply_patch_payload(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: APPLY_PATCH
SUMMARY_HUMAN: Применяю точечный патч.
NEXT_STEP_HUMAN: Запусти compile.
TASK_SUMMARY_EN: Replace the leftover Play JSON usage.
CURRENT_GOAL_EN: Apply exact source replacements.
PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1
*** BEGIN FILE ***
PATH: src/main/scala/com/acme/Parser.scala
OP: REPLACE
MATCH_COUNT: 1
@@@SEARCH@@@
import play.api.libs.json.Json
@@@REPLACE@@@
import io.circe.syntax._
@@@END@@@
*** END FILE ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.payload["patch_format"], "search_replace_v1")
        self.assertEqual(envelope.command.payload["files"][0]["path"], "src/main/scala/com/acme/Parser.scala")
        self.assertEqual(envelope.command.payload["files"][0]["replacements"][0]["expected_matches"], 1)

    def test_parses_search_replace_apply_patch_payload(self) -> None:
        response = """
Patch ready.

<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "APPLY_PATCH"
summary_human: "Apply the safer structured patch."
next_step_human: "Run compile."
task_summary_en: "Replace the leftover Play JSON usage."
current_goal_en: "Apply exact source replacements."
payload:
  patch_format: "search_replace_v1"
  files:
    - path: "src/main/scala/com/acme/Parser.scala"
      replacements:
        - search: |
            import play.api.libs.json.Json
          replace: |
            import io.circe.syntax._
          expected_matches: 1
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.payload["patch_format"], "search_replace_v1")
        self.assertEqual(envelope.command.payload["files"][0]["path"], "src/main/scala/com/acme/Parser.scala")

    def test_relaxed_parser_accepts_misaligned_search_replace_payload(self) -> None:
        malformed = """
Patch ready.

<<<DEVLOOP_COMMAND_START>>>
version: 1
command: APPLY_PATCH
summary_human: "Применяю безопасный структурный патч."
next_step_human: "Запусти compile."
task_summary_en: "Fix the remaining compile issue."
current_goal_en: "Apply the safest exact replacements."
payload:
patch_format: search_replace_v1
files:
- path: "src/main/scala/com/acme/Parser.scala"
replacements:
- search: |
    old
  replace: |
    new
  expected_matches: 1
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(malformed)
        self.assertEqual(envelope.command.command, "APPLY_PATCH")
        self.assertEqual(envelope.command.payload["patch_format"], "search_replace_v1")
        self.assertEqual(envelope.command.payload["files"][0]["path"], "src/main/scala/com/acme/Parser.scala")

    def test_relaxed_parser_accepts_realistic_bad_search_replace_reply(self) -> None:
        malformed = """
Ниже даю корректный блок под текущий devloop-протокол.

<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "APPLY_PATCH"
summary_human: "Подготовил минимальный патч."
next_step_human: "Примени патч и снова запусти compile."
task_summary_en: "Replace leftover Play JSON usage in DayEndCommandsSpec with Circe."
current_goal_en: "Apply the smallest safe patch to fix current IT compilation errors."
payload:
patch_format: "search_replace_v1"
files:
- path: "external-api/src/it/scala/com/acme/DayEndCommandsSpec.scala"
replacements:
- search: |
import org.scalatest.OptionValues.convertOptionToValuable
import play.api.libs.json.Json

        import java.time.Instant
      replace: |
        import io.circe.syntax._
        import org.scalatest.OptionValues.convertOptionToValuable

        import java.time.Instant
      expected_matches: 1
    - search: |
        val json    = Json.toJson(payload).toString
      replace: |
        val json    = payload.asJson.noSpaces
      expected_matches: 4
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(malformed)
        self.assertEqual(envelope.command.command, "APPLY_PATCH")
        self.assertEqual(envelope.command.payload["patch_format"], "search_replace_v1")
        self.assertEqual(
            envelope.command.payload["files"][0]["path"],
            "external-api/src/it/scala/com/acme/DayEndCommandsSpec.scala",
        )
        self.assertEqual(len(envelope.command.payload["files"][0]["replacements"]), 2)
        self.assertEqual(envelope.command.payload["files"][0]["replacements"][1]["expected_matches"], 4)

    def test_parses_v2_ask_human_payload(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: ASK_HUMAN
SUMMARY_HUMAN: Нужен прогон тестов.
NEXT_STEP_HUMAN: Запусти sbt test.
TASK_SUMMARY_EN: Continue after migration.
CURRENT_GOAL_EN: Collect the next failing test.
*** BEGIN REQUESTED_RUN ***
KIND: sbt
PURPOSE: Run focused tests
COMMAND_EXAMPLE: sbt test
*** END REQUESTED_RUN ***
*** BEGIN EXPECTED_ARTIFACT ***
TEXT: First failing test name
*** END EXPECTED_ARTIFACT ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.command, "ASK_HUMAN")
        self.assertEqual(envelope.command.payload["requested_runs"][0]["kind"], "sbt")
        self.assertEqual(envelope.command.payload["expected_artifacts_from_human"][0], "First failing test name")

    def test_rejects_legacy_unified_diff_patch_format(self) -> None:
        response = """
Patch ready.

<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "APPLY_PATCH"
summary_human: "Apply the legacy diff."
next_step_human: "Run compile."
task_summary_en: "Fix the compile issue."
current_goal_en: "Try the old diff format."
payload:
  patch_format: "git_unified_diff"
  patch: |
    diff --git a/src/main/scala/com/acme/Parser.scala b/src/main/scala/com/acme/Parser.scala
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        with self.assertRaises(ProtocolError) as context:
            parse_protocol_response(response)
        self.assertIn("search_replace_v1", str(context.exception))


if __name__ == "__main__":
    unittest.main()
