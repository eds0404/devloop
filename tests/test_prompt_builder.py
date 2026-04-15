import re
import unittest

from devloop.prompt_builder import (
    PromptSection,
    build_bootstrap_prompt,
    build_context_prompt,
    load_protocol_reference_text,
)
from devloop.protocol import (
    SUPPORTED_COMMANDS,
    SUPPORTED_PATCH_FORMAT_V2,
    SUPPORTED_QUERY_TYPES,
    parse_protocol_response,
)


class PromptBuilderTests(unittest.TestCase):
    def test_bootstrap_prompt_includes_current_protocol_contract(self) -> None:
        prompt = build_bootstrap_prompt("repo-name", "Russian")
        self.assertIn("DEVLOOP_COMMAND_V2", prompt)
        self.assertIn("SEARCH_REPLACE_BLOCKS_V1", prompt)
        self.assertIn("Do not use YAML.", prompt)
        self.assertIn("Do not emit prose outside the command block.", prompt)

    def test_includes_protocol_rules_in_context_prompt(self) -> None:
        result = build_context_prompt(
            task_summary="Task",
            current_goal="Goal",
            source_label="unit test",
            human_language_name="Russian",
            sections=[PromptSection("Important", "Body", required=True)],
            max_chars=4000,
        )
        self.assertIn("Machine protocol is mandatory for every reply.", result.text)
        self.assertIn("<<<DEVLOOP_COMMAND_START>>>", result.text)
        self.assertIn("DEVLOOP_COMMAND_V2", result.text)
        self.assertIn("Do not use YAML.", result.text)
        self.assertIn("read_around_match", result.text)
        self.assertIn("Do not emit prose outside the command block.", result.text)

    def test_can_replace_full_protocol_reference_with_short_reminder(self) -> None:
        result = build_context_prompt(
            task_summary="Task",
            current_goal="Goal",
            source_label="unit test",
            human_language_name="Russian",
            sections=[PromptSection("Important", "Body", required=True)],
            max_chars=4000,
            include_protocol_reference=False,
        )
        self.assertIn("The full protocol reference is intentionally omitted", result.text)
        self.assertNotIn("read_around_match", result.text)
        self.assertIn("exactly one DEVLOOP_COMMAND_V2 block", result.text)
        self.assertIn("Do not use YAML.", result.text)
        self.assertIn("Do not add prose outside the block.", result.text)
        self.assertIn("PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1", result.text)

    def test_protocol_reference_examples_parse_with_runtime_parser(self) -> None:
        reference = load_protocol_reference_text("Russian")
        blocks = re.findall(
            r"<<<DEVLOOP_COMMAND_START>>>\n.*?\n<<<DEVLOOP_COMMAND_END>>>",
            reference,
            flags=re.DOTALL,
        )
        example_blocks = [block for block in blocks if "\nDEVLOOP_COMMAND_V2\n" in block]
        self.assertGreaterEqual(len(example_blocks), 4)

        parsed_commands = [parse_protocol_response(block).command.command for block in example_blocks]
        self.assertEqual(parsed_commands[:4], ["ASK_HUMAN", "DONE", "COLLECT_CONTEXT", "APPLY_PATCH"])

    def test_protocol_reference_lists_current_runtime_capabilities(self) -> None:
        reference = load_protocol_reference_text("Russian")
        for command in SUPPORTED_COMMANDS:
            self.assertIn(f"`{command}`", reference)
        for query_type in SUPPORTED_QUERY_TYPES:
            self.assertIn(f"`{query_type}`", reference)
        self.assertIn(SUPPORTED_PATCH_FORMAT_V2, reference)

    def test_reports_truncation_at_section_boundaries(self) -> None:
        sections = [
            PromptSection("Important", "A" * 40, required=True),
            PromptSection("Optional One", "\n".join(["line"] * 30), compact_body="compact one"),
            PromptSection("Optional Two", "\n".join(["line"] * 30), compact_body="compact two"),
        ]
        result = build_context_prompt(
            task_summary="Task",
            current_goal="Goal",
            source_label="unit test",
            human_language_name="Russian",
            sections=sections,
            max_chars=260,
        )
        self.assertTrue(result.truncated)
        self.assertIn("Truncation report", result.text)
        self.assertTrue(result.omitted_titles or result.shortened_titles)


if __name__ == "__main__":
    unittest.main()
