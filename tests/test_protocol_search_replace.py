import unittest

from devloop.errors import ProtocolError
from devloop.protocol import parse_protocol_response


class ProtocolSearchReplaceTests(unittest.TestCase):
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
