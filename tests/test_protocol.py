import unittest

from devloop.errors import ProtocolError
from devloop.protocol import extract_command_block, parse_protocol_response


SAMPLE_RESPONSE_V2 = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: COLLECT_CONTEXT
SUMMARY_HUMAN: Collecting the minimum context.
NEXT_STEP_HUMAN: Paste the new prompt into ChatGPT.
TASK_SUMMARY_EN: Fix the compile issue.
CURRENT_GOAL_EN: Inspect the parser implementation.
PROMPT_GOAL: Diagnose the compile failure.
*** BEGIN QUERY ***
TYPE: read_snippet
FILE: src/main/scala/com/acme/Parser.scala
START_LINE: 10
END_LINE: 30
*** END QUERY ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()


class ProtocolTests(unittest.TestCase):
    def test_extracts_single_command_block(self) -> None:
        block = extract_command_block(SAMPLE_RESPONSE_V2)
        self.assertIn("COMMAND: COLLECT_CONTEXT", block)

    def test_accepts_duplicate_identical_blocks(self) -> None:
        duplicated = SAMPLE_RESPONSE_V2 + "\n\n" + SAMPLE_RESPONSE_V2
        block = extract_command_block(duplicated)
        self.assertEqual(block, extract_command_block(SAMPLE_RESPONSE_V2))

    def test_parses_v2_protocol_envelope(self) -> None:
        envelope = parse_protocol_response(SAMPLE_RESPONSE_V2)
        self.assertEqual(envelope.command.command, "COLLECT_CONTEXT")
        self.assertEqual(envelope.command.task_summary_en, "Fix the compile issue.")
        self.assertEqual(envelope.command.summary_human, "Collecting the minimum context.")
        self.assertEqual(envelope.command.payload["queries"][0]["type"], "read_snippet")

    def test_extracts_command_block_markers(self) -> None:
        block = extract_command_block(SAMPLE_RESPONSE_V2)
        self.assertIn("VERSION: 1", block)

    def test_parses_v2_collect_context_envelope(self) -> None:
        envelope = parse_protocol_response(SAMPLE_RESPONSE_V2)
        self.assertEqual(envelope.command.command, "COLLECT_CONTEXT")
        self.assertEqual(envelope.command.summary_human, "Collecting the minimum context.")
        self.assertEqual(envelope.command.payload["queries"][0]["type"], "read_snippet")

    def test_parses_project_tree_query_with_path(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: COLLECT_CONTEXT
SUMMARY_HUMAN: Need the subtree.
NEXT_STEP_HUMAN: Paste the next prompt.
TASK_SUMMARY_EN: Inspect repository layout.
CURRENT_GOAL_EN: Read one source subtree.
*** BEGIN QUERY ***
TYPE: project_tree
PATH: core/src
*** END QUERY ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.payload["queries"][0]["type"], "project_tree")
        self.assertEqual(envelope.command.payload["queries"][0]["path"], "core/src")

    def test_rejects_multiple_distinct_blocks(self) -> None:
        other = SAMPLE_RESPONSE_V2.replace("COMMAND: COLLECT_CONTEXT", "COMMAND: DONE", 1)
        invalid = SAMPLE_RESPONSE_V2 + "\n" + other
        with self.assertRaises(ProtocolError):
            extract_command_block(invalid)

    def test_parses_v2_with_mixed_case_keys(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
Version: 1
Command: ASK_HUMAN
Summary_Human: Ask the human to rerun compile.
Next_Step_Human: Run the compile command and return the first error.
Task_Summary_En: Validate parser robustness.
Current_Goal_En: Accept mixed-case V2 keys.
*** BEGIN REQUESTED_RUN ***
Kind: sbt
Purpose: Re-run compile
Command_Example: sbt compile
*** END REQUESTED_RUN ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.command, "ASK_HUMAN")
        self.assertEqual(envelope.command.payload["requested_runs"][0]["kind"], "sbt")

    def test_parses_done_command(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: DONE
SUMMARY_HUMAN: Done.
NEXT_STEP_HUMAN: Review the result.
TASK_SUMMARY_EN: Finish the task.
CURRENT_GOAL_EN: Stop the workflow.
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.command.command, "DONE")
        self.assertEqual(envelope.command.payload, {})

    def test_strips_human_text_outside_command_block(self) -> None:
        response = "Short explanation for the human.\n\n" + SAMPLE_RESPONSE_V2
        envelope = parse_protocol_response(response)
        self.assertEqual(envelope.human_text, "Short explanation for the human.")

    def test_rejects_unknown_collect_context_query_type(self) -> None:
        response = """
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: COLLECT_CONTEXT
SUMMARY_HUMAN: Collecting context.
NEXT_STEP_HUMAN: Paste the new prompt into ChatGPT.
TASK_SUMMARY_EN: Validate parser safety.
CURRENT_GOAL_EN: Reject unsupported query types.
*** BEGIN QUERY ***
TYPE: unsupported_query
FILE: src/main/scala/com/acme/Parser.scala
*** END QUERY ***
<<<DEVLOOP_COMMAND_END>>>
""".strip()
        with self.assertRaises(ProtocolError) as context:
            parse_protocol_response(response)
        self.assertIn("Unsupported query type", str(context.exception))

    def test_rejects_yaml_protocol_block(self) -> None:
        response = """
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
        with self.assertRaises(ProtocolError) as context:
            parse_protocol_response(response)
        self.assertIn("DEVLOOP_COMMAND_V2", str(context.exception))


if __name__ == "__main__":
    unittest.main()
