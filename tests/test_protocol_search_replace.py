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
SUMMARY_HUMAN: Apply the exact patch.
NEXT_STEP_HUMAN: Run compile.
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

    def test_parses_v2_search_replace_with_mixed_case_and_hash_alias(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
Version: 1
Command: APPLY_PATCH
Summary_Human: Apply the exact patch.
Next_Step_Human: Run compile.
Task_Summary_En: Validate parser robustness.
Current_Goal_En: Accept mixed-case V2 patch fields.
Patch_Format: search_replace_blocks_v1
*** BEGIN FILE ***
Path: src/main/scala/com/acme/Parser.scala
Op: replace
Hash: sha256:abc123
Match_Count: 1
@@@SEARCH@@@
old
@@@REPLACE@@@
new
@@@END@@@
*** END FILE ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.payload["patch_format"], "search_replace_v1")
        self.assertEqual(envelope.command.payload["files"][0]["expected_sha256"], "abc123")
        self.assertEqual(envelope.command.payload["files"][0]["replacements"][0]["search"], "old")

    def test_parses_v2_search_replace_with_lowercase_markers(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: APPLY_PATCH
SUMMARY_HUMAN: Apply the exact patch.
NEXT_STEP_HUMAN: Run compile.
TASK_SUMMARY_EN: Validate parser robustness.
CURRENT_GOAL_EN: Accept lowercase markers.
PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1
*** begin file ***
PATH: src/main/scala/com/acme/Parser.scala
OP: REPLACE
MATCH_COUNT: 1
@@@search@@@
old
@@@replace@@@
new
@@@end@@@
*** end file ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.payload["files"][0]["replacements"][0]["replace"], "new")

    def test_parses_v2_create_and_delete_operations(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: APPLY_PATCH
SUMMARY_HUMAN: Apply create and delete operations.
NEXT_STEP_HUMAN: Run compile.
TASK_SUMMARY_EN: Validate create and delete patch sections.
CURRENT_GOAL_EN: Parse non-replace file operations.
PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1
*** BEGIN FILE ***
PATH: src/main/scala/com/acme/NewFile.scala
OP: CREATE_FILE
@@@CONTENT@@@
object NewFile {}
@@@END@@@
*** END FILE ***
*** BEGIN FILE ***
PATH: src/main/scala/com/acme/OldFile.scala
OP: DELETE_FILE
EXPECTED_SHA256: abc123
*** END FILE ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        files = envelope.command.payload["files"]
        self.assertEqual(files[0]["operation"], "create")
        self.assertEqual(files[0]["content"], "object NewFile {}")
        self.assertEqual(files[1]["operation"], "delete")
        self.assertEqual(files[1]["expected_sha256"], "abc123")

    def test_rejects_invalid_file_operation(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: APPLY_PATCH
SUMMARY_HUMAN: Apply the exact patch.
NEXT_STEP_HUMAN: Run compile.
TASK_SUMMARY_EN: Validate parser robustness.
CURRENT_GOAL_EN: Reject invalid file operations.
PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1
*** BEGIN FILE ***
PATH: src/main/scala/com/acme/Parser.scala
OP: MOVE
*** END FILE ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        with self.assertRaises(ProtocolError) as context:
            parse_protocol_response(response)
        self.assertIn("Unsupported DEVLOOP_COMMAND_V2 file operation", str(context.exception))

    def test_rejects_replace_without_replacement_blocks(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: APPLY_PATCH
SUMMARY_HUMAN: Apply the exact patch.
NEXT_STEP_HUMAN: Run compile.
TASK_SUMMARY_EN: Validate parser robustness.
CURRENT_GOAL_EN: Reject incomplete replace sections.
PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1
*** BEGIN FILE ***
PATH: src/main/scala/com/acme/Parser.scala
OP: REPLACE
*** END FILE ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        with self.assertRaises(ProtocolError) as context:
            parse_protocol_response(response)
        self.assertIn("replacements list", str(context.exception))

    def test_parses_v2_ask_human_payload(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: ASK_HUMAN
SUMMARY_HUMAN: A test run is needed.
NEXT_STEP_HUMAN: Run sbt test.
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

    def test_rejects_yaml_search_replace_payload(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
version: "1"
command: "APPLY_PATCH"
summary_human: "Apply the structured patch."
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
        with self.assertRaises(ProtocolError) as context:
            parse_protocol_response(response)
        self.assertIn("DEVLOOP_COMMAND_V2", str(context.exception))


if __name__ == "__main__":
    unittest.main()
